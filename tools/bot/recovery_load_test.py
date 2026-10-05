"""Finite Linux Release recovery matrix with isolated MySQL fixtures.

Baseline scales, real non-reading client, original-ACK reconnects, an InnoDB
row-lock delay and private Redis network faults. Existing volumes survive.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from room_recovery_windows_test import Acceptance
from room_recovery_integration_test import action, connect, read_checkpoint, stable_snapshot
from protocol import make_message, negotiate_protocol, read_message, send_message
from tools.bot.load_test import latency_summary, run_load
from tools.bot.redis_fault_proxy import RedisFaultProxy
from tools.ranking.rebuild_leaderboard import RedisClient
from tools.replay.replay_battle import load_cards, load_replay, reconstruct

CHECKPOINT_FIELDS = (
    "recovery_checkpoint_attempts", "recovery_checkpoint_successes", "recovery_checkpoint_failures",
    "recovery_checkpoint_bytes_total", "recovery_checkpoint_bytes_max",
    "recovery_checkpoint_serialize_us_total", "recovery_checkpoint_write_us_total", "recovery_checkpoint_write_us_max",
    "recovery_checkpoint_lock_wait_us_total", "recovery_checkpoint_connection_us_total",
    "recovery_checkpoint_sql_us_total", "recovery_checkpoint_commit_us_total",
    "recovery_checkpoint_queue_wait_us_total", "recovery_checkpoint_batches", "recovery_checkpoint_batch_items_max",
)


def completed_matches(report):
    requested = report["clients"] * report["rounds_per_client"]
    if (requested <= 0 or report["completed_rounds"] != requested or len(report["results"]) != report["clients"]
            or any(item.get("errors") for item in report["results"])):
        raise AssertionError("requested player rounds did not all complete")
    if requested % 2:
        raise AssertionError("player-round count must be even")
    return requested // 2


def process_sample(pid):
    # /proc/<pid>/stat totals include every server thread, excluding the Python driver.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"at": time.monotonic(), "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE")}


def resource_summary(samples):
    rates = [(b["cpu_seconds"] - a["cpu_seconds"]) / (b["at"] - a["at"]) * 100
             for a, b in zip(samples, samples[1:]) if b["at"] > a["at"]]
    return {"source": "/proc server process", "sample_count": len(samples),
            "peak_cpu_percent": round(max(rates), 3) if rates else None,
            "peak_rss_bytes": max((sample["rss_bytes"] for sample in samples), default=None)}


class Matrix(Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.matrix = []
        self.active_names = []

    async def metrics(self, protocol="text_v1"):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.args.port)
        try:
            if protocol == "proto_v1":
                await negotiate_protocol(reader, writer, protocol)
            await send_message(writer, "AdminRoomsReq", {}, protocol=protocol)
            response = await asyncio.wait_for(read_message(reader, protocol=protocol), 10)
            if response["type"] != "AdminRoomsResp":
                raise AssertionError("invalid Admin response")
            return {key: int(value) for key, value in response["payload"].items() if key != "request_id"}
        finally:
            writer.close()
            await writer.wait_closed()

    async def measure_action(self, pair, kind, request, latencies):
        current = pair[int(pair[0].snapshot["turn"])]
        started = time.perf_counter()
        await current.send(kind, request)
        ack = await current.receive("ActionAck")
        latencies.append((time.perf_counter() - started) * 1000)
        updates = await asyncio.gather(*(player.receive(("BattleSnapshot", "MatchResult")) for player in pair))
        if updates[0]["type"] != updates[1]["type"] or (updates[0]["type"] == "MatchResult" and
                updates[0]["payload"] != updates[1]["payload"]):
            raise AssertionError("players disagree on outcome")
        return ack, updates

    async def finish_pair(self, pair, latencies):
        for _ in range(41):
            current = pair[int(pair[0].snapshot["turn"])]
            _, updates = await self.measure_action(pair, "EndTurnReq", {
                "match_id": current.snapshot["match_id"], "turn_id": int(current.snapshot["turn_id"]),
                "action_id": int(current.snapshot["last_action_id"]) + 1}, latencies)
            if updates[0]["type"] == "MatchResult":
                return
        raise AssertionError("controlled pair did not finish")

    async def controlled(self, scenario, count, protocol):
        args = self.client_args(protocol, self.args.port)
        # Deterministic pair assignment while keeping all rooms simultaneously active.
        pairs = []
        match_started = []
        for number in range(count // 2):
            pair, names = await self.new_match(args, f"{scenario}{number}")
            self.active_names.extend(names)
            pairs.append(pair)
            match_started.append(time.perf_counter())
        latencies = []
        requests = [{"match_id": pair[0].snapshot["match_id"], "turn_id": 1, "action_id": 1,
                     "card": 1, "request_id": f"{scenario}-{index}"} for index, pair in enumerate(pairs)]
        blocker = None
        if scenario == "mysql-delay":
            blocker = await self.hold_row(f"delay-{count}-{protocol}",
                "SELECT match_id FROM room_checkpoints WHERE match_id='" + requests[0]["match_id"] + "' FOR UPDATE")
        pending = asyncio.gather(*(self.measure_action(pair, "PlayCardReq", request, latencies)
                                   for pair, request in zip(pairs, requests)))
        if blocker:
            try:
                await self.blocked_query("UPDATE room_checkpoints")
                delay_started = time.perf_counter()
                await asyncio.sleep(self.args.delay_ms / 1000)
                delay_ms = (time.perf_counter() - delay_started) * 1000
            finally:
                self.release_row(blocker)
        outcomes = await pending
        first_latencies = list(latencies)
        extra = {"first_action_ack": latency_summary(first_latencies)}
        if scenario == "mysql-delay":
            if max(first_latencies) < self.args.delay_ms * 0.8:
                raise AssertionError("injected SQL lock delay did not affect checkpoint confirmation")
            extra.update(lock_hold_ms=round(delay_ms, 3), blocked_query_verified=True)
        if scenario == "reconnect":
            originals = [await read_checkpoint(args, request["match_id"]) for request in requests]
            snapshots = [stable_snapshot(pair[0].snapshot) for pair in pairs]
            await asyncio.gather(*(pair[0].close() for pair in pairs))
            async def resume_pair(index):
                pair = pairs[index]
                started = time.perf_counter()
                resumed = await connect(args, token=pair[0].token)
                self.connections.append(resumed)
                await resumed.receive("BattleSnapshot")
                await pair[1].receive("BattleSnapshot")
                if stable_snapshot(resumed.snapshot) != snapshots[index]:
                    raise AssertionError("reconnect changed authoritative state")
                pair[0] = resumed
                if await read_checkpoint(args, requests[index]["match_id"]) != originals[index]:
                    raise AssertionError("reconnect changed RNG/replay/receipts/turn deadline")
                await resumed.send("PlayCardReq", requests[index])
                if (await resumed.receive("ActionAck"))["payload"] != outcomes[index][0]["payload"]:
                    raise AssertionError("reconnect lost original ACK")
                await self.assert_no_delivery(pair, ("BattleEvent", "BattleSnapshot"))
                return (time.perf_counter() - started) * 1000
            reconnect_latencies = await asyncio.gather(*(resume_pair(index) for index in range(len(pairs))))
            extra.update(reconnected_players=len(pairs), original_ack_verified=len(pairs),
                         reconnect_latency=latency_summary(reconnect_latencies))
        async def finish(index):
            await self.finish_pair(pairs[index], latencies)
            return (time.perf_counter() - match_started[index]) * 1000
        match_latencies = await asyncio.gather(*(finish(index) for index in range(len(pairs))))
        for pair in pairs:
            await asyncio.gather(*(member.close() for member in pair))
        return {"clients": count, "completed_rounds": count, "rounds_per_client": 1,
                "success_rate": 1.0, "latency": {"action_ack": latency_summary(latencies),
                                                 "completed_match": latency_summary(match_latencies)},
                "match_latency_scope": "MatchFound/snapshot received to MatchResult",
                "action_latency_ms": [round(value, 3) for value in latencies], **extra}

    async def slow_reader(self, report_task):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(sock, ("127.0.0.1", self.args.port))
        before = await self.metrics()
        sent = 0
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    await asyncio.wait_for(loop.sock_sendall(sock, make_message("AdminRoomsReq") * 32), 0.5)
                    sent += 32
                except (OSError, asyncio.TimeoutError):
                    break
                await asyncio.sleep(0.005)
                if sent % 256 == 0:
                    current = await self.metrics()
                    if current["send_frames_dropped"] > before["send_frames_dropped"]:
                        break
            after = await self.metrics()
            if after["send_frames_dropped"] <= before["send_frames_dropped"]:
                raise AssertionError("real non-reading connection did not trigger bounded backpressure")
            report = await report_task
            report.update(nonreading_requests=sent, receive_buffer_bytes=sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                          dropped_frames=after["send_frames_dropped"] - before["send_frames_dropped"],
                          send_queue_high_watermark_bytes=after["send_queue_high_watermark_bytes"])
            return report
        finally:
            sock.close()
            if not report_task.done():
                report_task.cancel()
                await asyncio.gather(report_task, return_exceptions=True)

    async def baseline(self, count, protocol, slow=False):
        prefix = "p6_" + self.suffix + "_"
        names = [prefix + str(number + 1) for number in range(count)]
        self.names.extend(names)
        self.active_names.extend(names)
        args = SimpleNamespace(host="127.0.0.1", port=self.args.port, count=count, timeout=self.args.timeout,
                               rounds=1, protocol=protocol, name_prefix=prefix)
        task = asyncio.create_task(run_load(args))
        report = await self.slow_reader(task) if slow else await task
        return report

    async def redis_fault(self, scenario, count, protocol, proxy):
        before = await self.metrics()
        fault_started = time.monotonic()
        await proxy.set_mode({"redis-outage": "refuse", "redis-partition": "blackhole",
                              "redis-reply-loss": "drop-reply"}[scenario],
                             **({"command": "EVAL"} if scenario == "redis-reply-loss" else {}))
        workload = asyncio.create_task(self.baseline(count, protocol))
        try:
            if scenario == "redis-outage":
                # Every player receives MatchResult while this Arena cannot use Redis.
                report = await workload
                rows = await asyncio.to_thread(self.sql,
                    "SELECT COUNT(*) FROM matches WHERE status='finished'; "
                    "SELECT COUNT(*) FROM match_results; "
                    "SELECT COUNT(*) FROM settlement_outbox WHERE status='pending'; "
                    "SELECT COUNT(*) FROM settlement_outbox WHERE attempts>0;")
                expected = str(count // 2)
                if rows.splitlines() != [expected] * 4:
                    raise AssertionError("Redis outage blocked/duplicated settlement or lost pending Outbox: " + rows)
                redis = RedisClient(self.args.redis_host, self.args.redis_port)
                try:
                    if any(redis.command("ZSCORE", "arena:leaderboard:rating", name) is not None
                           for name in self.active_names):
                        raise AssertionError("private outage unexpectedly updated Redis")
                finally:
                    redis.close()
                fault_metrics = await self.metrics()
                if fault_metrics["redis_apply_failures"] <= before["redis_apply_failures"]:
                    raise AssertionError("Redis outage missing apply failure metrics")
                report.update(results_delivered_during_outage=count, pending_during_outage=count // 2,
                              recorded_failures_during_outage=count // 2, cache_absent_during_outage=True)
            elif scenario == "redis-partition":
                # No reply/FIN is delivered, unlike refused connections. MySQL still commits.
                await self.wait_until(lambda: proxy.stats()["blocked_bytes"] > 0,
                                      "partition did not block a Redis command", timeout=self.args.timeout)
                blocked_at = time.monotonic()
                await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM match_results") == str(count // 2),
                                      "Redis partition blocked authoritative MySQL settlement",
                                      timeout=self.args.timeout)
                await asyncio.sleep(max(0, self.args.partition_ms / 1000 - (time.monotonic() - blocked_at)))
                report = {"mysql_committed_during_partition": count // 2,
                          "blackhole_hold_ms": round((time.monotonic() - blocked_at) * 1000, 3),
                          "results_completed_before_restore": (workload.done() and not workload.cancelled()
                                                               and workload.exception() is None)}
            else:
                report = await workload
                stats = proxy.stats()
                dropped = [event for event in stats["events"] if event["event"] == "reply-dropped"]
                if (stats["lost_replies"] != 1 or len(dropped) != 1 or
                        dropped[0].get("command") != "EVAL" or dropped[0].get("reply_type") != ":"):
                    raise AssertionError("no successful integer EVAL reply was lost after Redis execution")
                report.update(lost_apply_replies=1)
            await proxy.set_mode("pass")
            restored_at = time.monotonic()
            if scenario == "redis-partition":
                report = {**await workload, **report}
            report.update(fault_duration_ms=round((restored_at - fault_started) * 1000, 3),
                          fault_metrics=await self.metrics(), redis_proxy=proxy.stats())
            return report
        finally:
            await proxy.set_mode("pass")
            if not workload.done():
                workload.cancel()
            await asyncio.gather(workload, return_exceptions=True)

    def verify_database(self, expected):
        raw = self.sql("SELECT m.match_id,m.player_a,m.player_b,m.winner_id,o.status "
                       "FROM matches m JOIN match_results r ON r.match_id=m.match_id "
                       "JOIN settlement_outbox o ON o.match_id=m.match_id WHERE m.status='finished';")
        rows = [line.split("\t") for line in raw.splitlines() if line]
        if len(rows) != expected or any(row[4] != "applied" for row in rows):
            return None
        if self.sql("SELECT COUNT(*) FROM matches WHERE status='running'") != "0":
            raise AssertionError("unfinished room remains")
        redis = RedisClient(self.args.redis_host, self.args.redis_port)
        try:
            ratings = self.sql("SELECT player_id,rating,wins,losses FROM players ORDER BY player_id")
            if len(ratings.splitlines()) != expected * 2:
                raise AssertionError("unexpected player count")
            for line in ratings.splitlines():
                name, rating, wins, losses = line.split("\t")
                if (int(rating), int(wins), int(losses)) not in ((1010, 1, 0), (990, 0, 1)):
                    raise AssertionError("settlement counts/ratings duplicated: " + line)
                if int(float(redis.command("ZSCORE", "arena:leaderboard:rating", name))) != int(rating):
                    raise AssertionError("Redis differs from MySQL")
        finally:
            redis.close()
        return rows

    async def run_case(self, scenario, count, protocol, recovery):
        proxy = None
        if scenario.startswith("redis-"):
            proxy = RedisFaultProxy(self.args.redis_host, self.args.redis_port)
            await proxy.start()
        try:
            await self._run_case(scenario, count, protocol, recovery, proxy)
        finally:
            if proxy:
                await proxy.close()

    async def _run_case(self, scenario, count, protocol, recovery, proxy):
        label = f"{scenario}-{count}-{protocol}-r{int(recovery)}"
        self.environment["ARENA_ROOM_RECOVERY"] = str(int(recovery))
        if proxy:
            self.environment.update(ARENA_REDIS_HOST="127.0.0.1", ARENA_REDIS_PORT=str(proxy.port))
        else:
            self.environment.update(ARENA_REDIS_HOST=self.args.redis_host, ARENA_REDIS_PORT=str(self.args.redis_port))
        os.environ.update(self.environment)
        server = self.spawn(label, self.args.port)
        await self.ready(server, self.args.port)
        self.active_names = []
        samples = []
        stop = asyncio.Event()
        async def sample():
            while not stop.is_set():
                samples.append(process_sample(server.process.pid))
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.05)
        sampler = asyncio.create_task(sample())
        started = time.perf_counter()
        try:
            if scenario in ("baseline", "slow-reader"):
                report = await self.baseline(count, protocol, slow=scenario == "slow-reader")
            elif proxy:
                report = await self.redis_fault(scenario, count, protocol, proxy)
            else:
                report = await self.controlled(scenario, count, protocol)
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            await sampler
        report.update(scenario=scenario, protocol=protocol, recovery=recovery,
                      checkpoint_batch_size=self.args.checkpoint_batch_size,
                      resources=resource_summary(samples), duration_ms=round(elapsed * 1000, 3))
        if scenario in ("baseline", "slow-reader") or proxy:
            try:
                completed_matches(report)
            except AssertionError as error:
                match_ids = self.sql("SELECT match_id FROM matches").splitlines()
                self.matches.extend(match_ids)
                report.update(passed=False, error=str(error), metrics=await self.metrics())
                raw_path = self.evidence / (label + ".json")
                raw_path.write_text(json.dumps({"report": report, "resource_samples": samples}, separators=(",", ":")) + "\n")
                self.matrix.append({key: value for key, value in report.items() if key != "results"})
                print(json.dumps(self.matrix[-1], separators=(",", ":")), flush=True)
                raise
        rows = await self.wait_until(lambda: self.verify_database(count // 2), "MySQL/Redis/Outbox settlement incomplete", timeout=30)
        match_ids = [row[0] for row in rows]
        self.matches.extend(match_ids)
        await self.wait_until(lambda: all((self.evidence / "replays" / (match_id + ".replay")).exists()
                                       for match_id in match_ids), "replay files did not persist")
        cards = load_cards(ROOT / "server/config/cards.csv")
        for match_id, first, second, winner, _ in rows:
            loaded_id, _, _, events = load_replay(self.evidence / "replays" / (match_id + ".replay"))
            state = reconstruct(events, cards, loaded_id)
            if loaded_id != match_id or state["winner"] != (0 if winner == first else 1):
                raise AssertionError("offline replay winner differs from MySQL")
        metrics = await self.metrics()
        proto_metrics = await self.metrics("proto_v1")
        for name in CHECKPOINT_FIELDS:
            if not 0 <= metrics[name] <= 2**64 - 1 or proto_metrics[name] != metrics[name]:
                raise AssertionError("checkpoint metrics missing or differ across protocols")
        if recovery and (metrics["recovery_checkpoint_successes"] == 0 or metrics["recovery_checkpoint_failures"]):
            raise AssertionError("recovery checkpoint writes missing or failed")
        if proxy and not metrics["redis_apply_failures"]:
            raise AssertionError("Redis fault did not report an apply failure")
        if not recovery and any(metrics[name] for name in CHECKPOINT_FIELDS):
            raise AssertionError("disabled recovery reports checkpoint writes")
        attempts = metrics["recovery_checkpoint_attempts"]
        report.update(scenario=scenario, protocol=protocol, recovery=recovery,
                      completed_matches=len(rows), duration_ms=round(elapsed * 1000, 3),
                      throughput_matches_per_second=round(len(rows) / elapsed, 3),
                      resources=resource_summary(samples), metrics=metrics, replays_verified=len(rows),
                      checkpoint_mean_write_ms=round(metrics["recovery_checkpoint_write_us_total"] / attempts / 1000, 3) if attempts else None,
                      checkpoint_mean_bytes=round(metrics["recovery_checkpoint_bytes_total"] / attempts) if attempts else None,
                      passed=True)
        if proxy:
            report["redis_proxy"] = proxy.stats()
        raw_path = self.evidence / (label + ".json")
        raw_path.write_text(json.dumps({"report": report, "resource_samples": samples}, separators=(",", ":")) + "\n")
        summary = {key: value for key, value in report.items() if key not in ("results", "action_latency_ms")}
        summary.update(raw_file=raw_path.name, raw_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
        self.matrix.append(summary)
        print(json.dumps(summary, separators=(",", ":")), flush=True)
        self.kill(server)
        redis = RedisClient(self.args.redis_host, self.args.redis_port)
        try:
            redis.command("ZREM", "arena:leaderboard:rating", *self.active_names)
            redis.command("DEL", *("arena:match:rank:" + match_id for match_id in match_ids))
        finally:
            redis.close()
        self.clear_matches()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19300)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--delay-ms", type=float, default=250)
    parser.add_argument("--partition-ms", type=float, default=1000)
    parser.add_argument("--scales", default="20,100,200")
    parser.add_argument("--scenario-count", type=int, default=20)
    parser.add_argument("--scenario-scales", help="comma-separated fault client counts; overrides scenario-count")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    parser.add_argument("--scenarios", nargs="+", choices=("baseline", "slow-reader", "reconnect", "mysql-delay",
                        "redis-outage", "redis-reply-loss", "redis-partition"),
                        default=("baseline", "slow-reader", "reconnect", "mysql-delay",
                                 "redis-outage", "redis-reply-loss", "redis-partition"))
    parser.add_argument("--compare-disabled", action="store_true")
    parser.add_argument("--checkpoint-batch-size", type=int, default=16)
    parser.add_argument("--evidence", default="build-p6-recovery")
    args = parser.parse_args()
    scales = [int(value) for value in args.scales.split(",")]
    scenario_scales = [int(value) for value in args.scenario_scales.split(",")] if args.scenario_scales else [args.scenario_count]
    if not sys.platform.startswith("linux") or any(value <= 0 or value % 2 for value in scales + scenario_scales):
        parser.error("Linux and positive even client counts are required")
    if not 0 < args.delay_ms < 5000 or not 0 < args.partition_ms < 5000 or args.timeout <= 0:
        parser.error("delay-ms and partition-ms must be in (0,5000), timeout positive")
    if not 1 <= args.checkpoint_batch_size <= 32:
        parser.error("checkpoint-batch-size must be in [1,32]")
    matrix = Matrix(args)
    matrix.environment["ARENA_CHECKPOINT_BATCH_SIZE"] = str(args.checkpoint_batch_size)
    original = os.environ.copy()
    summary = {"passed": False, "matrix": matrix.matrix, "source_commit": os.getenv("ARENA_SOURCE_COMMIT", "unknown"),
               "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "server_path": args.server, "started_at": datetime.now(timezone.utc).isoformat(),
               "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest()}
    async def run():
        for scenario in args.scenarios:
            for count in (scales if scenario == "baseline" else scenario_scales):
                if args.compare_disabled and scenario == "baseline":
                    await matrix.run_case(scenario, count, args.protocol, recovery=False)
                await matrix.run_case(scenario, count, args.protocol, recovery=True)
    try:
        matrix.setup()
        asyncio.run(run())
        summary["passed"] = True
        return 0
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        try:
            # Include matches from a failed workload before the isolated schema is removed.
            with contextlib.suppress(Exception):
                matrix.matches.extend(matrix.sql("SELECT match_id FROM matches").splitlines())
            matrix.cleanup()
            summary["fixtures_cleaned"] = True
        except Exception as error:
            summary.update(passed=False, fixtures_cleaned=False, cleanup_error=str(error))
            if not primary_error:
                raise
        finally:
            os.environ.clear()
            os.environ.update(original)
            summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            (matrix.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print("Evidence: " + str(matrix.evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
