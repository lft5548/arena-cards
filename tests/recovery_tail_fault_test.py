"""Only the new snapshot-tail persistence windows, using isolated real storage.

Requires a test-enabled C++ server. All online reconstruction is performed by
the restarted C++ server, never by the Python offline replay decoder.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from room_recovery_windows_test import Acceptance, expected_response
from room_recovery_integration_test import action, connect, stable_snapshot
from tools.bot.mysql_commit_proxy import MysqlCommitReplyProxy
from tools.bot.recovery_load_test import Matrix
from tools.replay.replay_battle import load_cards, load_replay, reconstruct


class TailAcceptance(Acceptance):
    def __init__(self, args):
        super().__init__(args)
        self.environment.update(ARENA_ROOM_RECOVERY_MODE="tail", ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL="16",
                                ARENA_CHECKPOINT_BATCH_SIZE="16", ARENA_TEST_REPLAY_SEED="424242")

    def durable(self, match_id):
        row = self.sql("SELECT recovery_format,snapshot_sequence,current_sequence,HEX(checkpoint) "
                       "FROM room_checkpoints WHERE match_id='" + match_id + "'")
        if not row:
            raise AssertionError("durable snapshot missing")
        kind, snapshot_sequence, current_sequence, snapshot = row.split("\t")
        tail = self.sql("SELECT sequence,HEX(payload) FROM room_recovery_tail WHERE match_id='" + match_id + "' ORDER BY sequence")
        rows = [line.split("\t") for line in tail.splitlines() if line]
        expected = list(range(int(snapshot_sequence) + 1, int(current_sequence) + 1))
        if kind != "tail" or [int(item[0]) for item in rows] != expected:
            raise AssertionError("tail is incomplete or not continuous")
        return {"format": kind, "snapshot_sequence": int(snapshot_sequence), "current_sequence": int(current_sequence),
                "snapshot_sha256": hashlib.sha256(bytes.fromhex(snapshot)).hexdigest(),
                "tail": [[int(sequence), hashlib.sha256(bytes.fromhex(payload)).hexdigest()] for sequence, payload in rows]}

    def disconnect_tail_deadline(self, match_id):
        """Inspect the metadata-only SQL record, without rebuilding battle state."""
        encoded = self.sql("SELECT HEX(payload) FROM room_recovery_tail WHERE match_id='" + match_id + "' AND sequence=2")
        if not encoded:
            return None
        data = bytes.fromhex(encoded)
        magic = b"ARENA_ROOM_TAIL_V1\n"
        if not data.startswith(magic):
            raise AssertionError("disconnect record has unexpected tail format")
        offset = len(magic)
        def integer():
            nonlocal offset
            value, = struct.unpack_from("<Q", data, offset)
            offset += 8
            return value
        def string():
            nonlocal offset
            size = integer()
            offset += size
            if offset > len(data) - 8:
                raise AssertionError("truncated disconnect tail")
        if (integer(), integer()) != (1, 2):
            raise AssertionError("disconnect did not follow the exact committed candidate")
        string()  # match ID
        string()  # rules fingerprint
        for _ in range(4):
            integer()  # version, base/current revision, seed
        if integer() != 1 << 46:
            raise AssertionError("deferred disconnect unexpectedly changed battle or candidate fields")
        deadline = integer()
        if integer() != 0 or offset != len(data) - 8:
            raise AssertionError("disconnect generated battle events")
        checksum = 14695981039346656037
        for byte in data[:-8]:
            checksum = ((checksum ^ byte) * 1099511628211) & ((1 << 64) - 1)
        if integer() != checksum:
            raise AssertionError("disconnect tail checksum differs")
        return deadline

    async def drain_snapshots(self, pair):
        for member in pair:
            while True:
                try:
                    await member.receive("BattleSnapshot", timeout=0.1)
                except asyncio.TimeoutError:
                    break

    async def finish_and_verify(self, pair, match_id, names):
        result = await self.finish(pair)
        await self.settled(match_id, names)
        replay = self.evidence / "replays" / (match_id + ".replay")
        await self.wait_until(replay.exists, "recovered replay did not persist")
        loaded_id, _, _, events = load_replay(replay)
        state = reconstruct(events, load_cards(ROOT / "server/config/cards.csv"), loaded_id)
        if loaded_id != match_id or state["winner"] != int(result["winner"]):
            raise AssertionError("recovered C++ result and complete offline replay disagree")

    @staticmethod
    def ack_payload(request, revision, protocol):
        request_id = request["request_id"] if protocol == "proto_v1" else ""
        raw = ("match_id=" + request["match_id"] + ";turn_id=" + str(request["turn_id"]) + ";action_id=" +
               str(request["action_id"]) + ";status=applied;revision=" + str(revision) + ";request_id=" + request_id)
        return expected_response("ActionAck", raw, protocol)

    async def replay_ack(self, pair, request, revision, protocol):
        before = self.durable(request["match_id"])
        await pair[0].send("PlayCardReq", request)
        reply = await pair[0].receive("ActionAck")
        expected = self.ack_payload(request, revision, protocol)
        if reply["payload"] != expected:
            raise AssertionError("online recovery did not retain the exact original ACK: " +
                                 json.dumps({"expected": expected, "actual": reply["payload"]}))
        await self.assert_no_delivery(pair, ("BattleEvent", "BattleSnapshot"))
        if self.durable(request["match_id"]) != before:
            raise AssertionError("retry duplicated a tail mutation")

    async def commit_before_ack(self, protocol):
        args = self.client_args(protocol, self.args.port)
        label = protocol + "-tail-commit-ack"
        self.environment["ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL"] = "16"
        server = self.spawn(label, args.port, "checkpoint_before_ack")
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "tail_ack_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1, "request_id": "tail-durable-no-ack"}
        await pair[0].send("PlayCardReq", request)
        await self.barrier(server, "checkpoint_before_ack", match_id)
        durable = self.durable(match_id)
        if durable["snapshot_sequence"] != 0 or durable["current_sequence"] != 1 or len(durable["tail"]) != 1:
            raise AssertionError("COMMIT/ACK barrier did not persist exactly one tail entry")
        await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        self.kill(server)
        restarted = self.spawn(label + "-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [member for member, _ in await self.resume(args, pair)]
        if resumed[0].snapshot["p1_hp"] != "22" or resumed[0].snapshot["last_action_id"] != "1":
            raise AssertionError("C++ tail reconstruction lost the committed action")
        await self.replay_ack(resumed, request, 2, protocol)
        await self.finish_and_verify(resumed, match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "tail_COMMIT_before_ACK", "durable": durable,
                             "original_ack_verified": True, "replay_verified": True, "passed": True})

    async def compact_window(self, protocol, point):
        args = self.client_args(protocol, self.args.port)
        label = protocol + "-" + point
        self.environment["ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL"] = "2"
        server = self.spawn(label, args.port, point)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "compact_" + point.replace("snapshot_", "") + "_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        strike = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1, "request_id": "precompact-ack"}
        await action(pair, "PlayCardReq", strike)
        original = [stable_snapshot(member.snapshot) for member in pair]
        durable = self.durable(match_id)
        if durable["current_sequence"] != 1 or durable["snapshot_sequence"] != 0:
            raise AssertionError("compact fixture does not have one existing tail")
        current = pair[int(pair[0].snapshot["turn"])]
        end = {"match_id": match_id, "turn_id": int(current.snapshot["turn_id"]),
               "action_id": int(current.snapshot["last_action_id"]) + 1, "request_id": "uncommitted-compact"}
        await current.send("EndTurnReq", end)
        await self.barrier(server, point, match_id)
        if self.durable(match_id) != durable:
            raise AssertionError("another MySQL connection observed a partially published snapshot")
        await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        self.kill(server)
        restarted = self.spawn(label + "-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [member for member, _ in await self.resume(args, pair)]
        if [stable_snapshot(member.snapshot) for member in resumed] != original:
            raise AssertionError("snapshot/tail atomic rollback changed the last confirmed state")
        await self.replay_ack(resumed, strike, 2, protocol)
        await action(resumed, "EndTurnReq", end)
        await self.finish_and_verify(resumed, match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": point, "old_snapshot_and_tail_visible": True,
                             "rollback_verified_online": True, "replay_verified": True, "passed": True})

    async def commit_reply_lost(self, protocol):
        args = self.client_args(protocol, self.args.port)
        label = protocol + "-tail-commit-reply-loss"
        self.environment["ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL"] = "16"
        # MySQL 8's first caching_sha2_password authentication requires TLS.
        # Warm only this random user's fast-auth cache through a direct TLS
        # connection, then inspect its private plaintext proxy connection.
        auth_environment = os.environ.copy()
        auth_environment["MYSQL_PWD"] = self.password
        authenticated = await asyncio.to_thread(subprocess.run,
            [self.args.mysql_client, "--batch", "--skip-column-names", "--ssl-mode=REQUIRED",
             "--host=" + self.args.mysql_host, "--port=" + str(self.args.mysql_port),
             "--user=" + self.user, self.database], input="SELECT 1;", env=auth_environment,
            capture_output=True, text=True, timeout=15, check=True)
        if authenticated.stdout.strip() != "1":
            raise AssertionError("private MySQL user did not authenticate through TLS")
        proxy = MysqlCommitReplyProxy(self.args.mysql_host, self.args.mysql_port)
        await proxy.start()
        previous_host, previous_port = self.environment["ARENA_MYSQL_HOST"], self.environment["ARENA_MYSQL_PORT"]
        self.environment.update(ARENA_MYSQL_HOST="127.0.0.1", ARENA_MYSQL_PORT=str(proxy.port))
        try:
            server = self.spawn(label, args.port)
            await self.ready(server, args.port)
            pair, names = await self.new_match(args, "commit_loss_" + protocol)
            match_id = pair[0].snapshot["match_id"]
            request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1, "request_id": "uncertain-commit-ack"}
            proxy.arm()
            await pair[0].send("PlayCardReq", request)
            await asyncio.wait_for(proxy.dropped.wait(), 10)
            if proxy.errors or len(proxy.observations) != 1 or not proxy.observations[0]["commit_ok"]:
                raise AssertionError("real MySQL COMMIT OK was not discarded: " + str(proxy.errors or proxy.observations))
            disconnected_at_ms = int(time.time() * 1000)
            opponent = pair[1]
            await opponent.close()
            durable = self.durable(match_id)
            if durable["current_sequence"] != 1 or len(durable["tail"]) != 1:
                raise AssertionError("discarded COMMIT reply was not independently durable")
            await self.assert_no_delivery([pair[0]], ("ActionAck", "BattleEvent", "BattleSnapshot"))
            ack = await pair[0].receive("ActionAck", timeout=15)
            update = await pair[0].receive("BattleSnapshot", timeout=15)
            if ack["payload"] != self.ack_payload(request, 2, protocol) or update["payload"]["p1_hp"] != "22":
                raise AssertionError("uncertain COMMIT retry changed its ACK or duplicated damage")
            deadline = await self.wait_until(lambda: self.disconnect_tail_deadline(match_id),
                "disconnect metadata was not committed after uncertain candidate retry", timeout=8)
            if not disconnected_at_ms + 14000 <= deadline <= disconnected_at_ms + 16000:
                raise AssertionError("deferred disconnect extended or lost its original absolute deadline")
            if self.durable(match_id)["current_sequence"] < 2:
                raise AssertionError("deferred disconnect reused the action candidate sequence")
            resumed_opponent = await connect(args, token=opponent.token)
            self.connections.append(resumed_opponent)
            await resumed_opponent.receive("BattleSnapshot", timeout=15)
            pair[1] = resumed_opponent
            await self.drain_snapshots(pair)
            retry_metrics = await Matrix.metrics(self)
            if not retry_metrics["recovery_checkpoint_failures"] or not retry_metrics["mysql_connection_losses"]:
                raise AssertionError("lost COMMIT response did not invalidate its MySQL connection or report failure")
            # Verify the same action after a fresh C++ online reconstruction, too.
            self.kill(server)
            restarted = self.spawn(label + "-restart", args.port)
            await self.ready(restarted, args.port)
            resumed = [member for member, _ in await self.resume(args, pair)]
            await self.replay_ack(resumed, request, 2, protocol)
            await self.finish_and_verify(resumed, match_id, names)
            metrics = await Matrix.metrics(self)
            self.kill(restarted)
            self.clear_matches()
            self.results.append({"protocol": protocol, "window": "successful_COMMIT_reply_lost", "proxy": proxy.observations,
                                 "tls_capability_removed": proxy.tls_capability_removed,
                                 "private_auth_cache_warmed_via_tls": True,
                                 "opponent_disconnected_during_uncertain_commit": True,
                                 "disconnect_absolute_deadline_ms": deadline,
                                 "original_disconnect_time_ms": disconnected_at_ms,
                                 "opponent_reconnected_with_original_token": True,
                                 "independent_sql_durability": durable, "original_ack_verified": True,
                                 "replay_verified": True, "retry_metrics": retry_metrics,
                                 "restart_metrics": metrics, "passed": True})
        finally:
            self.environment.update(ARENA_MYSQL_HOST=previous_host, ARENA_MYSQL_PORT=previous_port)
            await proxy.close()

    async def run(self):
        protocols = ("text_v1", "proto_v1") if self.args.protocol == "both" else (self.args.protocol,)
        for protocol in protocols:
            for scenario in self.args.scenarios:
                if scenario == "commit-ack":
                    await self.commit_before_ack(protocol)
                elif scenario == "compact":
                    for point in ("snapshot_before_tail_cleanup", "snapshot_after_tail_cleanup"):
                        await self.compact_window(protocol, point)
                        print(json.dumps(self.results[-1]), flush=True)
                    continue
                else:
                    await self.commit_reply_lost(protocol)
                print(json.dumps(self.results[-1]), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19500)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1", "both"), default="both")
    parser.add_argument("--scenarios", nargs="+", choices=("commit-ack", "compact", "commit-reply-loss"),
                        default=("commit-ack", "compact", "commit-reply-loss"))
    parser.add_argument("--evidence", default="build-recovery-tail-faults")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Linux required for exact real-process crash sequencing")
    acceptance = TailAcceptance(args)
    original = os.environ.copy()
    summary = {"passed": False, "results": acceptance.results,
        "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        acceptance.setup()
        os.environ.update(acceptance.environment)
        asyncio.run(acceptance.run())
        findings = [str(path) for pattern in ("asan.*", "ubsan.*") for path in acceptance.evidence.glob(pattern) if path.stat().st_size]
        summary["sanitizer_reports"] = findings
        if findings:
            raise AssertionError("sanitizer reports emitted: " + str(findings))
        summary["passed"] = True
        return 0
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        try:
            acceptance.cleanup()
            summary["fixtures_cleaned"] = True
        except Exception as error:
            summary.update(passed=False, fixtures_cleaned=False, cleanup_error=str(error))
            if not primary_error:
                raise
        finally:
            os.environ.clear()
            os.environ.update(original)
            (acceptance.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print("Evidence: " + str(acceptance.evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
