"""Focused real-MySQL/Redis result delivery and lost-Lua-reply acceptance.

Runs an isolated Arena process, schema/user and loopback fault proxy. Existing
services and volumes remain running. Normal Release and sanitizer binaries are
both supported; explicit crash-window build hooks are not required.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from room_recovery_windows_test import Acceptance
from room_recovery_integration_test import action
from protocol import negotiate_protocol, read_message, send_message
from tools.bot.redis_fault_proxy import RedisFaultProxy
from tools.ranking.rebuild_leaderboard import RedisClient
from tools.replay.replay_battle import load_cards, load_replay, reconstruct

FAILURE_FIELDS = ("redis_connection_failures", "redis_apply_failures", "settlement_outbox_failures")


class RedisReliability(Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.environment.update(
            ARENA_MYSQL_POOL_SIZE="2", ARENA_CHECKPOINT_BATCH_SIZE="16",
            ARENA_REDIS_CONNECT_TIMEOUT_MS=str(args.redis_timeout_ms),
            ARENA_REDIS_IO_TIMEOUT_MS=str(args.redis_timeout_ms))

    async def metrics(self, protocol):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.args.port)
        try:
            if protocol == "proto_v1":
                await negotiate_protocol(reader, writer, protocol)
            request_id = "redis-reliability-" + protocol
            await send_message(writer, "AdminRoomsReq", {"request_id": request_id}, protocol=protocol)
            response = await asyncio.wait_for(read_message(reader, protocol=protocol), 2)
            if response["type"] != "AdminRoomsResp":
                raise AssertionError("invalid Admin response")
            if protocol == "proto_v1" and response["payload"].get("request_id") != request_id:
                raise AssertionError("ProtoV1 Admin response lost request correlation")
            return {key: int(value) for key, value in response["payload"].items() if key != "request_id"}
        finally:
            writer.close()
            await writer.wait_closed()

    async def fastapi_metrics(self):
        import httpx
        from tools.admin_api import app as admin
        original = admin.HOST, admin.PORT
        admin.HOST, admin.PORT = "127.0.0.1", self.args.port
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin.app),
                                         base_url="http://arena-acceptance") as client:
                response = await client.get("/metrics")
                response.raise_for_status()
                health = await client.get("/health")
                health.raise_for_status()
                if not health.json().get("ready"):
                    raise AssertionError("FastAPI health did not report ready during Redis blackhole")
                return response.json()
        finally:
            admin.HOST, admin.PORT = original

    async def prepare_terminal(self, protocol, label, strike=True):
        pair, names = await self.new_match(self.client_args(protocol, self.args.port), label)
        match_id = pair[0].snapshot["match_id"]
        # One Strike establishes a deterministic winner without ending early.
        if strike:
            await action(pair, "PlayCardReq", {"match_id": match_id, "turn_id": 1,
                                                "action_id": 1, "card": 1})
        while int(pair[0].snapshot["turn_id"]) < 40:
            current = pair[int(pair[0].snapshot["turn"])]
            await action(pair, "EndTurnReq", {"match_id": match_id,
                "turn_id": int(current.snapshot["turn_id"]),
                "action_id": int(current.snapshot["last_action_id"]) + 1})
        return pair, names, match_id

    async def terminal_result(self, pair, match_id, expected_winner=0):
        current = pair[int(pair[0].snapshot["turn"])]
        started = time.monotonic()
        await current.send("EndTurnReq", {"match_id": match_id, "turn_id": 40,
                                          "action_id": int(current.snapshot["last_action_id"]) + 1})
        await current.receive("ActionAck", timeout=self.args.result_bound_seconds)
        async def receive(player):
            message = await player.receive("MatchResult", timeout=self.args.result_bound_seconds)
            return message["payload"], time.monotonic()
        responses = await asyncio.gather(*(receive(player) for player in pair))
        if responses[0][0] != responses[1][0] or responses[0][0].get("winner") != str(expected_winner):
            raise AssertionError("players received inconsistent terminal results")
        elapsed = max(received for _, received in responses) - started
        if elapsed > self.args.result_bound_seconds:
            raise AssertionError("MatchResult exceeded terminal-action delivery bound")
        return {"terminal_action_to_both_results_ms": round(elapsed * 1000, 3),
                "last_received_at": max(received for _, received in responses),
                "payload": responses[0][0]}

    def outbox(self, match_id):
        raw = self.sql("SELECT status,attempts,CONCAT('error:',last_error) FROM settlement_outbox WHERE match_id='" +
                       match_id + "'")
        if not raw:
            return None
        status, attempts, error = raw.split("\t", 2)
        return {"status": status, "attempts": int(attempts), "last_error": error.removeprefix("error:")}

    def verify_authority(self, match_id, names):
        raw = self.sql("SELECT m.status,m.winner_id,r.winner_id,r.loser_id FROM matches m "
                       "JOIN match_results r ON r.match_id=m.match_id WHERE m.match_id='" + match_id + "'; "
                       "SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "'; "
                       "SELECT rating,wins,losses FROM players WHERE player_id='" + names[0] + "'; "
                       "SELECT rating,wins,losses FROM players WHERE player_id='" + names[1] + "'; "
                       "SELECT COUNT(*) FROM settlement_outbox WHERE match_id='" + match_id + "';")
        expected = [f"finished\t{names[0]}\t{names[0]}\t{names[1]}", "1", "1010\t1\t0", "990\t0\t1", "1"]
        if raw.splitlines() != expected:
            raise AssertionError("authoritative settlement lost/duplicated: " + raw)

    def cache_values(self, match_id, names):
        redis = RedisClient(self.args.redis_host, self.args.redis_port)
        try:
            return [redis.command("ZSCORE", "arena:leaderboard:rating", name) for name in names], \
                redis.command("EXISTS", "arena:match:rank:" + match_id)
        finally:
            redis.close()

    async def verify_replay(self, match_id, result, expected_winner=0):
        path = self.evidence / "replays" / (match_id + ".replay")
        await self.wait_until(path.exists, "terminal replay was not persisted")
        loaded_id, seed, digest, events = load_replay(path)
        state = reconstruct(events, load_cards(ROOT / "server/config/cards.csv"), match_id)
        if (loaded_id != match_id or state["winner"] != expected_winner or
                str(seed) != result["replay_seed"] or str(digest) != result["replay_digest"] or
                len(events) != int(result["replay_revision"]) or result.get("replay_valid") != "1"):
            raise AssertionError("terminal replay differs from delivered result")

    async def wait_applied(self, match_id, names, result):
        def applied():
            row = self.outbox(match_id)
            return row if row and row["status"] == "applied" else None
        row = await self.wait_until(applied, "Outbox did not automatically apply after Redis recovery", timeout=10)
        self.verify_authority(match_id, names)
        scores, key = await asyncio.to_thread(self.cache_values, match_id, names)
        if [int(float(score)) for score in scores] != [1010, 990] or key != 1:
            raise AssertionError("Redis lost or duplicated committed rating changes")
        await self.verify_replay(match_id, result)
        return row

    async def blackhole(self, protocol, proxy):
        prepared = [await self.prepare_terminal(protocol, f"blackhole_{protocol}_{number}") for number in range(2)]
        before = await self.metrics(protocol)
        await proxy.set_mode("blackhole")
        fault_started = time.monotonic()
        tasks = []
        try:
            tasks.append(asyncio.create_task(self.terminal_result(prepared[0][0], prepared[0][2])))
            # Start the second settlement after the worker is demonstrably waiting
            # on a silent connection, exercising contention from another room.
            deadline = time.monotonic() + 3
            while not proxy.stats()["blocked_bytes"]:
                if time.monotonic() >= deadline:
                    raise AssertionError("Outbox worker did not send into the Redis blackhole")
                await asyncio.sleep(0.002)
            tasks.append(asyncio.create_task(self.terminal_result(prepared[1][0], prepared[1][2])))
            ids = "','".join(match_id for _, _, match_id in prepared)
            await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM match_results WHERE match_id IN ('" + ids + "')") == "2",
                                  "Redis blackhole blocked authoritative MySQL commits")
            observed_commit_at = time.monotonic()
            delivered = await asyncio.wait_for(asyncio.gather(*tasks), self.args.result_bound_seconds)
            commit_to_results_ms = max(0, max(item["last_received_at"] for item in delivered) - observed_commit_at) * 1000
            if commit_to_results_ms > self.args.result_bound_seconds * 1000 or proxy.mode != "blackhole":
                raise AssertionError("results were not delivered within bound while blackhole remained active")
            for _, names, match_id in prepared:
                self.verify_authority(match_id, names)
                scores, key = await asyncio.to_thread(self.cache_values, match_id, names)
                if scores != [None, None] or key != 0:
                    raise AssertionError("blackhole unexpectedly reached the shared Redis")
            def recorded():
                rows = [self.outbox(match_id) for _, _, match_id in prepared]
                return rows if (all(row and row["status"] == "pending" for row in rows) and
                                any(row["attempts"] > 0 and "timed out" in row["last_error"] for row in rows)) else None
            pending_rows = await self.wait_until(recorded, "Redis timeout did not retain pending Outbox/error details")
            deadline = time.monotonic() + 3
            while (await self.metrics(protocol))["settlement_outbox_pending"] < 2:
                if time.monotonic() >= deadline:
                    raise AssertionError("pending Outbox gauge did not converge during blackhole")
                await asyncio.sleep(0.05)
            metrics = {kind: await self.metrics(kind) for kind in ("text_v1", "proto_v1")}
            api = await self.fastapi_metrics()
            for source, fields in {**metrics, "fastapi": api}.items():
                if (any(fields[field] <= before[field] for field in FAILURE_FIELDS[1:]) or
                        fields["redis_connection_failures"] < before["redis_connection_failures"]):
                    raise AssertionError("missing Redis/Outbox failures in " + source)
                if fields["settlement_outbox_pending"] < 2:
                    raise AssertionError("pending Outbox gauge missing in " + source)
            if proxy.mode != "blackhole":
                raise AssertionError("proxy recovered before fault acceptance completed")
            fault_stats = proxy.stats()
            restored_at = time.monotonic()
            await proxy.set_mode("pass")
            recovered = [await self.wait_applied(match_id, names, outcome["payload"])
                         for (_, names, match_id), outcome in zip(prepared, delivered)]
            self.results.append({"protocol": protocol, "scenario": "blackhole", "passed": True,
                "simultaneous_rooms": 2, "results_delivered_before_restore": 4,
                "observed_commit_to_both_rooms_results_ms": round(commit_to_results_ms, 3),
                "terminal_action_to_both_results_ms": [item["terminal_action_to_both_results_ms"] for item in delivered],
                "result_bound_ms": self.args.result_bound_seconds * 1000,
                "fault_duration_ms": round((restored_at - fault_started) * 1000, 3),
                "outbox_during_fault": pending_rows, "outbox_after_restore": recovered, "fault_metrics": metrics,
                "fastapi_metrics": api, "proxy_during_fault": fault_stats, "replays_verified": 2})
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await proxy.set_mode("pass")
            for pair, _, _ in prepared:
                await asyncio.gather(*(player.close() for player in pair))

    async def lost_reply(self, protocol, proxy):
        pair, names, match_id = await self.prepare_terminal(protocol, "lostreply_" + protocol)
        before = await self.metrics(protocol)
        event_count = len(proxy.stats()["events"])
        await proxy.set_mode("drop-reply", command="EVAL")
        try:
            delivered = await self.terminal_result(pair, match_id)
            row = await self.wait_applied(match_id, names, delivered["payload"])
            events = proxy.stats()["events"][event_count:]
            dropped = [event for event in events if event["event"] == "reply-dropped"]
            replies = [event["reply_integer"] for event in events if event["event"] == "eval-reply"]
            if (len(dropped) != 1 or dropped[0].get("command") != "EVAL" or
                    dropped[0].get("reply_integer") != 1 or replies != [1, 0]):
                raise AssertionError("expected successful Lua reply loss followed by one idempotent retry: " + str(events))
            after = await self.metrics(protocol)
            if row["attempts"] < 1 or any(after[field] <= before[field] for field in FAILURE_FIELDS[1:]):
                raise AssertionError("lost apply reply did not record failed Outbox attempt and metrics")
            # A further poll must leave SQL and Redis at exactly one settlement.
            await asyncio.sleep(1.1)
            self.verify_authority(match_id, names)
            scores, key = await asyncio.to_thread(self.cache_values, match_id, names)
            if [int(float(score)) for score in scores] != [1010, 990] or key != 1:
                raise AssertionError("lost reply retry duplicated rating changes")
            self.results.append({"protocol": protocol, "scenario": "successful_lua_reply_loss", "passed": True,
                "terminal_action_to_both_results_ms": delivered["terminal_action_to_both_results_ms"],
                "eval_integer_replies": replies, "outbox": row, "redis_ratings": [1010, 990],
                "failure_metrics": {field: after[field] - before[field] for field in FAILURE_FIELDS[1:]},
                "replays_verified": 1})
        finally:
            await proxy.set_mode("pass")
            await asyncio.gather(*(player.close() for player in pair))

    async def disabled_draw(self):
        first, second = "backlog_a_" + self.suffix, "backlog_b_" + self.suffix
        ids = [f"rated_{number:03}_{self.suffix}" for number in range(256)]
        self.names.extend((first, second))
        self.matches.extend(ids)
        players = f"('{first}','{first}'),('{second}','{second}')"
        matches = ",".join(f"('{match_id}','{first}','{second}','{first}','finished')" for match_id in ids)
        results = ",".join(f"('{match_id}','{first}','{second}',10,-10)" for match_id in ids)
        outbox = ",".join(f"('{match_id}','{first}','{second}',10,-10,'2000-01-01 00:00:00')" for match_id in ids)
        self.sql("INSERT INTO players(player_id,nickname) VALUES " + players + "; "
                 "INSERT INTO matches(match_id,player_a,player_b,winner_id,status) VALUES " + matches + "; "
                 "INSERT INTO match_results(match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta) VALUES " + results + "; "
                 "INSERT INTO settlement_outbox(match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta,created_at) VALUES " + outbox + ";")
        self.environment["ARENA_REDIS_ENABLED"] = "0"
        server = self.spawn("redis-disabled-draw", self.args.port)
        pair = []
        try:
            await self.ready(server, self.args.port)
            pair, names, match_id = await self.prepare_terminal("text_v1", "disabled_draw", strike=False)
            delivered = await self.terminal_result(pair, match_id, expected_winner=-1)
            await self.wait_until(lambda: self.outbox(match_id) and self.outbox(match_id)["status"] == "applied",
                                  "cache-free draw starved behind 256 rated pending rows", timeout=5)
            raw = self.sql("SELECT r.winner_id,r.loser_id,r.winner_rating_delta,r.loser_rating_delta,o.status "
                           "FROM match_results r JOIN settlement_outbox o ON o.match_id=r.match_id "
                           "WHERE r.match_id='" + match_id + "'; "
                           "SELECT rating,wins,losses FROM players WHERE player_id IN ('" + names[0] + "','" + names[1] + "'); "
                           "SELECT COUNT(*) FROM settlement_outbox WHERE match_id LIKE 'rated_%' AND "
                           f"winner_id='{first}' AND loser_id='{second}' AND winner_rating_delta=10 AND loser_rating_delta=-10 AND status='pending' AND attempts=0 AND last_error='';")
            if raw.splitlines() != ["NULL\tNULL\t0\t0\tapplied", "1000\t0\t0", "1000\t0\t0", "256"]:
                raise AssertionError("disabled Redis draw/backlog semantics changed: " + raw)
            await self.verify_replay(match_id, delivered["payload"], expected_winner=-1)
            self.results.append({"protocol": "text_v1", "scenario": "redis_disabled_draw_behind_rated_backlog",
                "passed": True, "rated_pending_rows_preserved": 256, "draw_outbox": "applied",
                "draw_ratings": [1000, 1000], "replays_verified": 1,
                "terminal_action_to_both_results_ms": delivered["terminal_action_to_both_results_ms"]})
        finally:
            await asyncio.gather(*(player.close() for player in pair))
            self.kill(server)
            self.environment["ARENA_REDIS_ENABLED"] = "1"

    async def run(self):
        protocols = ("text_v1", "proto_v1") if self.args.protocol == "both" else (self.args.protocol,)
        for protocol in protocols:
            proxy = await RedisFaultProxy(self.args.redis_host, self.args.redis_port).start()
            self.environment.update(ARENA_REDIS_HOST="127.0.0.1", ARENA_REDIS_PORT=str(proxy.port))
            server = self.spawn(protocol, self.args.port)
            try:
                await self.ready(server, self.args.port)
                await self.blackhole(protocol, proxy)
                print(json.dumps(self.results[-1]), flush=True)
                await self.lost_reply(protocol, proxy)
                print(json.dumps(self.results[-1]), flush=True)
            finally:
                self.kill(server)
                await proxy.close()
        await self.disabled_draw()
        print(json.dumps(self.results[-1]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19380)
    parser.add_argument("--evidence", default="build-redis-reliability")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1", "both"), default="both")
    parser.add_argument("--redis-timeout-ms", type=int, default=100)
    parser.add_argument("--result-bound-seconds", type=float, default=2)
    args = parser.parse_args()
    if not 1 <= args.redis_timeout_ms <= 30000 or args.result_bound_seconds <= 0:
        parser.error("Redis timeout must be 1-30000 ms and the result bound must be positive")
    acceptance = RedisReliability(args)
    original = os.environ.copy()
    summary = {"passed": False, "results": acceptance.results,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "server_path": args.server,
               "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest(),
               "configuration": {key: acceptance.environment[key] for key in (
                   "ARENA_ROOM_RECOVERY", "ARENA_MYSQL_CLEANUP_RUNNING", "ARENA_MYSQL_POOL_SIZE",
                   "ARENA_CHECKPOINT_BATCH_SIZE", "ARENA_REDIS_FAIL_APPLY_COUNT",
                   "ARENA_REDIS_CONNECT_TIMEOUT_MS", "ARENA_REDIS_IO_TIMEOUT_MS")}}
    try:
        acceptance.setup()
        os.environ.update(acceptance.environment)
        asyncio.run(acceptance.run())
        findings = [str(path) for pattern in ("asan.*", "ubsan.*")
                    for path in acceptance.evidence.glob(pattern) if path.stat().st_size]
        summary["sanitizer_reports"] = findings
        if findings:
            raise AssertionError("product sanitizer reports were emitted: " + str(findings))
        summary["passed"] = True
        return 0
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        try:
            acceptance.cleanup()
            summary["fixtures_cleaned"] = True
        except Exception as error:
            summary.update(passed=False, fixtures_cleaned=False, cleanup_error=str(error))
            raise
        finally:
            os.environ.clear()
            os.environ.update(original)
            summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            (acceptance.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print("Evidence: " + str(acceptance.evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
