"""Linux graceful-stop/restart evidence with an isolated real MySQL/Redis fixture.

Signals are delivered to the actual Arena process. A real InnoDB lock verifies
the bounded failure branch and that an uncommitted action never gets an ACK.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from room_recovery_windows_test import Acceptance, expected_response
from room_recovery_integration_test import action, read_checkpoint, stable_snapshot


class GracefulRecovery(Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.environment.update(ARENA_MYSQL_POOL_SIZE="2", ARENA_CHECKPOINT_BATCH_SIZE="16",
                                ARENA_HEARTBEAT_TIMEOUT_MS="30000", ARENA_SHUTDOWN_TIMEOUT_MS="2000")

    async def stop(self, server, signum=signal.SIGTERM, expected=0, bound=3):
        started = time.monotonic()
        server.process.send_signal(signum)
        code = await asyncio.wait_for(asyncio.to_thread(server.process.wait, bound), bound + 0.1)
        elapsed = time.monotonic() - started
        if code != expected or elapsed > bound:
            raise AssertionError(f"{signum.name} exit {code} after {elapsed:.3f}s; expected {expected} within {bound}s\n" +
                                 server.log_path.read_text(errors="replace"))
        return {"signal": signum.name, "exit_code": code, "elapsed_ms": round(elapsed * 1000, 3)}

    async def no_false_ack(self, player):
        messages = []
        try:
            while True:
                message = await player.receive(("ActionAck", "BattleEvent", "BattleSnapshot", "MatchResult", "Error"), timeout=0.5)
                messages.append(message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except asyncio.TimeoutError:
            raise AssertionError("shutdown left the blocked action transport open")
        if any(item["type"] == "ActionAck" for item in messages):
            raise AssertionError("uncommitted action received a false ACK: " + str(messages))
        return [item["type"] for item in messages]

    async def live_and_terminal(self, protocol):
        args = self.client_args(protocol, self.args.port)
        server = self.spawn(protocol + "-live", args.port)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "grace_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1,
                   "request_id": "graceful-original-action"}
        original_ack, _ = await action(pair, "PlayCardReq", request)
        checkpoint = await read_checkpoint(args, match_id)
        snapshots = [stable_snapshot(player.snapshot) for player in pair]
        stopped = await self.stop(server, signal.SIGTERM)
        if await read_checkpoint(args, match_id) != checkpoint:
            raise AssertionError("graceful live stop changed checkpoint/RNG/ACK/deadlines")
        if self.sql("SELECT status FROM matches WHERE match_id='" + match_id + "'; "
                    "SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "'") != "running\n0":
            raise AssertionError("graceful recovery stop forced an unfinished winner/result")
        restarted = self.spawn(protocol + "-live-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [player for player, _ in await self.resume(args, pair)]
        if [stable_snapshot(player.snapshot) for player in resumed] != snapshots:
            raise AssertionError("restart changed battle/replay state")
        restored = await read_checkpoint(args, match_id)
        if restored != checkpoint:
            raise AssertionError("restart changed original token/128 ACK/RNG/absolute deadline")
        remaining = int(resumed[0].snapshot["remaining_ms"])
        expected_remaining = checkpoint["turn_deadline"] - int(time.time() * 1000)
        if abs(remaining - expected_remaining) > 1200:
            raise AssertionError("graceful restart reset the absolute turn deadline")
        await resumed[0].send("PlayCardReq", request)
        replayed_ack = await resumed[0].receive("ActionAck")
        if replayed_ack["payload"] != original_ack["payload"] or await read_checkpoint(args, match_id) != checkpoint:
            raise AssertionError("original action retry did not preserve its exact durable ACK")
        result = await self.finish(resumed)
        await self.settled(match_id, names)
        terminal_stop = await self.stop(restarted, signal.SIGINT)
        terminal_restart = self.spawn(protocol + "-terminal-restart", args.port)
        await self.ready(terminal_restart, args.port)
        terminal = await self.resume(args, resumed, terminal=True)
        if any(response["payload"] != result for _, response in terminal):
            raise AssertionError("terminal result changed across graceful stop/restart")
        await self.settled(match_id, names)
        final_stop = await self.stop(terminal_restart)
        await asyncio.gather(*(player.close() for player in pair + resumed + [player for player, _ in terminal]))
        self.results.append({"protocol": protocol, "scenario": "live_and_terminal_graceful_restart", "passed": True,
                             "live_stop": stopped, "terminal_stop": terminal_stop, "final_stop": final_stop,
                             "checkpoint_identical": True, "original_ack_identical": True,
                             "absolute_deadline_preserved": True, "terminal_idempotency": True})

    async def locked_checkpoint_timeout(self, protocol):
        args = self.client_args(protocol, self.args.port)
        self.environment["ARENA_SHUTDOWN_TIMEOUT_MS"] = "200"
        server = self.spawn(protocol + "-locked", args.port)
        self.environment["ARENA_SHUTDOWN_TIMEOUT_MS"] = "2000"
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "blocked_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        checkpoint = await read_checkpoint(args, match_id)
        blocker = await self.hold_row(protocol + "-lock", "SELECT match_id FROM room_checkpoints WHERE match_id='" + match_id + "' FOR UPDATE")
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1,
                   "request_id": "shutdown-uncommitted-action"}
        try:
            await pair[0].send("PlayCardReq", request)
            await self.blocked_query("UPDATE room_checkpoints")
            stopped = await self.stop(server, expected=4, bound=1.5)
            received = await self.no_false_ack(pair[0])
            if await read_checkpoint(args, match_id) != checkpoint:
                raise AssertionError("shutdown timeout persisted a locked/uncommitted action")
            if self.sql("SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "'") != "0":
                raise AssertionError("failed graceful shutdown fabricated settlement")
        finally:
            self.release_row(blocker)
        await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM information_schema.innodb_trx t "
            "JOIN information_schema.PROCESSLIST p ON p.ID=t.trx_mysql_thread_id "
            f"WHERE p.USER='{self.user}'") == "0", "timed-out server transaction did not roll back")
        restarted = self.spawn(protocol + "-locked-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [player for player, _ in await self.resume(args, pair)]
        if await read_checkpoint(args, match_id) != checkpoint:
            raise AssertionError("restart lost last acknowledged checkpoint after stop timeout")
        await action(resumed, "PlayCardReq", request)
        applied = await read_checkpoint(args, match_id)
        if applied["revision"] != checkpoint["revision"] + 1 or applied["players"][1]["state"][0] != 22:
            raise AssertionError("undurable action retry applied incorrectly")
        await self.finish(resumed)
        await self.settled(match_id, names)
        final_stop = await self.stop(restarted)
        await asyncio.gather(*(player.close() for player in pair + resumed))
        self.results.append({"protocol": protocol, "scenario": "locked_checkpoint_bounded_shutdown_failure", "passed": True,
                             "stop": stopped, "final_stop": final_stop, "inflight_sql_verified": True,
                             "messages_before_eof": received, "false_ack": False,
                             "last_acknowledged_checkpoint_preserved": True, "retry_once": True})

    async def run(self):
        for protocol in ("text_v1", "proto_v1") if self.args.protocol == "both" else (self.args.protocol,):
            for name, function in (("live", self.live_and_terminal), ("locked", self.locked_checkpoint_timeout)):
                if name in self.args.scenarios:
                    await function(protocol)
                    print(json.dumps(self.results[-1]), flush=True)
                    self.clear_matches()
        for name, function in (("nonrecovery", self.nonrecovery_abort), ("inflight", self.inflight_drain)):
            if name in self.args.scenarios:
                await function()
                print(json.dumps(self.results[-1]), flush=True)
                self.clear_matches()

    async def inflight_drain(self):
        args = self.client_args("text_v1", self.args.port)
        server = self.spawn("inflight-drain", args.port)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "drain")
        match_id = pair[0].snapshot["match_id"]
        checkpoint = await read_checkpoint(args, match_id)
        blocker = await self.hold_row("drain-lock", "SELECT match_id FROM room_checkpoints WHERE match_id='" + match_id + "' FOR UPDATE")
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1,
                   "request_id": "shutdown-inflight-durable-action"}
        stop_task = None
        released = False
        try:
            action_started_ms = int(time.time() * 1000)
            await pair[0].send("PlayCardReq", request)
            await self.blocked_query("UPDATE room_checkpoints")
            stop_task = asyncio.create_task(self.stop(server))
            await asyncio.sleep(0.25)
            await asyncio.to_thread(self.release_row, blocker)
            released = True
            stopped = await stop_task
        finally:
            if not released:
                self.release_row(blocker)
            if stop_task and not stop_task.done():
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
        drained = await read_checkpoint(args, match_id)
        comparison = {"before": checkpoint, "after": drained,
                      "changed_fields": [key for key in checkpoint if checkpoint[key] != drained[key]]}
        (self.evidence / "inflight-checkpoints.json").write_text(json.dumps(comparison, indent=2) + "\n")
        if drained["revision"] != checkpoint["revision"] + 1 or drained["players"][1]["state"][0] != 22:
            raise AssertionError("successful graceful stop did not drain the in-flight SQL action")
        # Strike uses the starting energy and normally advances to turn 2.
        # Its new turn deadline belongs to the action, before SIGTERM; restart
        # must subsequently preserve that drained deadline exactly.
        if (drained["turn_id"] != checkpoint["turn_id"] + 1 or
                abs(drained["turn_deadline"] - (action_started_ms + 30000)) > 500 or
                [player["disconnect_deadline"] for player in drained["players"]] != [0, 0]):
            raise AssertionError("in-flight graceful drain reset live deadline/disconnect state: " + json.dumps({
                "turn_before": checkpoint["turn_deadline"], "turn_after": drained["turn_deadline"],
                "action_started_ms": action_started_ms,
                "disconnect_before": [player["disconnect_deadline"] for player in checkpoint["players"]],
                "disconnect_after": [player["disconnect_deadline"] for player in drained["players"]]}))
        restarted = self.spawn("inflight-drain-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [player for player, _ in await self.resume(args, pair)]
        await resumed[0].send("PlayCardReq", request)
        ack = await resumed[0].receive("ActionAck")
        durable_ack = expected_response("ActionAck", drained["players"][0]["receipts"][0][2], "text_v1")
        if ack["payload"] != durable_ack or await read_checkpoint(args, match_id) != drained:
            raise AssertionError("retry after in-flight graceful drain changed its original durable ACK/state")
        await self.finish(resumed)
        await self.settled(match_id, names)
        final_stop = await self.stop(restarted)
        await asyncio.gather(*(player.close() for player in pair + resumed))
        self.results.append({"protocol": "text_v1", "scenario": "inflight_checkpoint_drains_within_budget", "passed": True,
                             "stop": stopped, "final_stop": final_stop, "inflight_sql_verified": True,
                             "lock_release_after_stop_ms": 250, "durable_original_ack_on_retry": True,
                             "normal_action_turn_advance": True,
                             "unchanged_absolute_deadline_after_restart": True, "terminal_idempotency": True})

    async def nonrecovery_abort(self):
        self.environment["ARENA_ROOM_RECOVERY"] = "0"
        server = self.spawn("nonrecovery-stop", self.args.port)
        pair = []
        try:
            await self.ready(server, self.args.port)
            pair, names = await self.new_match(self.client_args("text_v1", self.args.port), "no_recovery")
            match_id = pair[0].snapshot["match_id"]
            stopped = await self.stop(server)
            rows = self.sql("SELECT status,result_reason FROM matches WHERE match_id='" + match_id + "'; "
                            "SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "'; "
                            "SELECT COUNT(*) FROM settlement_outbox WHERE match_id='" + match_id + "'; "
                            "SELECT rating,wins,losses FROM players WHERE player_id IN ('" + names[0] + "','" + names[1] + "');")
            if rows.splitlines() != ["aborted\tserver_shutdown", "0", "0", "1000\t0\t0", "1000\t0\t0"]:
                raise AssertionError("non-recovery graceful stop scored/settled instead of aborting: " + rows)
            self.results.append({"protocol": "text_v1", "scenario": "nonrecovery_stop_aborts_without_scoring",
                                 "passed": True, "stop": stopped, "status": "aborted", "reason": "server_shutdown",
                                 "match_results": 0, "outbox_rows": 0, "ratings": [1000, 1000]})
        finally:
            await asyncio.gather(*(player.close() for player in pair))
            self.kill(server)
            self.environment["ARENA_ROOM_RECOVERY"] = "1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19390)
    parser.add_argument("--evidence", default="build-graceful-recovery")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1", "both"), default="both")
    parser.add_argument("--scenarios", nargs="+", choices=("live", "locked", "nonrecovery", "inflight"),
                        default=("live", "locked", "nonrecovery", "inflight"))
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Linux is required for actual SIGTERM/SIGINT graceful acceptance")
    acceptance = GracefulRecovery(args)
    original = os.environ.copy()
    summary = {"passed": False, "results": acceptance.results,
               "started_at": datetime.now(timezone.utc).isoformat(),
               "server": str(Path(args.server).resolve()),
               "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest(),
               "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "configuration": {key: acceptance.environment[key] for key in (
                   "ARENA_ROOM_RECOVERY", "ARENA_MYSQL_CLEANUP_RUNNING", "ARENA_REDIS_FAIL_APPLY_COUNT",
                   "ARENA_HEARTBEAT_TIMEOUT_MS", "ARENA_SHUTDOWN_TIMEOUT_MS")}}
    try:
        acceptance.setup()
        os.environ.update(acceptance.environment)
        asyncio.run(acceptance.run())
        reports = [str(path) for pattern in ("asan.*", "ubsan.*")
                   for path in acceptance.evidence.glob(pattern) if path.stat().st_size]
        summary["sanitizer_reports"] = reports
        if reports:
            raise AssertionError("product sanitizer reports: " + str(reports))
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
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            (acceptance.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print("Evidence: " + str(acceptance.evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
