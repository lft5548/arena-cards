"""Linux real-MySQL crash windows and process ownership acceptance.

Requires ARENA_ENABLE_TEST_FAULTS=ON. Uses a random schema/user and preserves
existing services and named volumes. Server logs and sanitizer reports survive.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from room_recovery_integration_test import action, connect, create_match, read_checkpoint
from protocol import MESSAGE_TYPES, parse_message
from tools.ranking.rebuild_leaderboard import RedisClient


def expected_response(kind, raw, protocol):
    payload = parse_message(MESSAGE_TYPES[kind].to_bytes(2, "big") + raw.encode())["payload"]
    if protocol == "text_v1":
        return payload
    context = {key: payload.get(key, "0" if key != "request_id" else "")
               for key in ("match_id", "turn_id", "action_id", "revision", "request_id")}
    if kind == "ActionAck":
        context.update(status=payload["status"], applied="1")
    else:
        context["revision"] = payload["replay_revision"]
        context.update({key: payload[key] for key in
                        ("winner", "reason", "replay_seed", "replay_revision", "replay_digest", "replay_valid")})
    return context


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.suffix = uuid.uuid4().hex[:12]
        self.database, self.user = "recovery_windows_" + self.suffix, "rw_" + self.suffix
        self.password = uuid.uuid4().hex
        self.lock_name = self.database
        self.evidence = Path(args.evidence).resolve() / self.suffix
        self.evidence.mkdir(parents=True)
        self.processes, self.connections, self.names, self.matches = [], [], [], []
        self.results = []
        self.environment = os.environ.copy()
        self.environment.update(
            ARENA_MYSQL_ENABLED="1", ARENA_MYSQL_REQUIRED="1", ARENA_ROOM_RECOVERY="1",
            ARENA_MYSQL_CLEANUP_RUNNING="0", ARENA_MYSQL_HOST=args.mysql_host,
            ARENA_MYSQL_PORT=str(args.mysql_port), ARENA_MYSQL_DATABASE=self.database,
            ARENA_MYSQL_USER=self.user, ARENA_MYSQL_PASSWORD=self.password,
            ARENA_MYSQL_LOCK_NAME=self.lock_name, ARENA_MYSQL_POOL_SIZE="1",
            ARENA_REDIS_ENABLED="1", ARENA_REDIS_HOST=args.redis_host,
            ARENA_REDIS_PORT=str(args.redis_port), ARENA_REDIS_FAIL_APPLY_COUNT="0",
            ARENA_OUTBOX_POLL_SECONDS="1", ARENA_REPLAY_DIR=str(self.evidence / "replays"),
            ASAN_OPTIONS="halt_on_error=1:detect_leaks=1:log_path=" + str(self.evidence / "asan"),
            UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1:log_path=" + str(self.evidence / "ubsan"))
        self.environment.pop("ARENA_TEST_FAULT_POINT", None)
        self.environment.pop("ARENA_TEST_FAULT_DIRECTORY", None)

    def sql(self, query, database=True):
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = os.getenv("ARENA_MYSQL_ROOT_PASSWORD", "root_dev_password")
        command = [self.args.mysql_client, "--batch", "--skip-column-names",
                   "--host=" + self.args.mysql_host, "--port=" + str(self.args.mysql_port), "-uroot"]
        if database:
            command.append(self.database)
        return subprocess.run(command, input=query, env=environment, capture_output=True,
                              text=True, timeout=15, check=True).stdout.strip()

    def setup(self):
        self.sql(f"CREATE DATABASE `{self.database}`; CREATE USER '{self.user}'@'%' "
                 f"IDENTIFIED BY '{self.password}'; GRANT ALL ON `{self.database}`.* "
                 f"TO '{self.user}'@'%';", database=False)
        self.sql((ROOT / "deploy/schema.sql").read_text(encoding="utf-8"))

    async def hold_row(self, label, query):
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = os.getenv("ARENA_MYSQL_ROOT_PASSWORD", "root_dev_password")
        log_path = self.evidence / (label + "-sql-lock.log")
        command = [self.args.mysql_client, "--batch", "--skip-column-names", "--unbuffered",
                   "--host=" + self.args.mysql_host, "--port=" + str(self.args.mysql_port), "-uroot", self.database]
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log, env=environment)
        self.processes.append(process)
        process.stdin.write(("START TRANSACTION; " + query + "; SELECT CONCAT(CONNECTION_ID(),':locked'); "
                             "SELECT SLEEP(60); ROLLBACK;\n").encode())
        process.stdin.close()
        def locked():
            for line in log_path.read_text(errors="replace").splitlines():
                if line.endswith(":locked"):
                    return int(line.split(":")[0])
            if process.poll() is not None:
                raise AssertionError("SQL lock holder exited: " + log_path.read_text(errors="replace"))
            return None
        connection = await self.wait_until(locked, "SQL row lock was not acquired")
        return SimpleNamespace(process=process, connection=connection)

    def release_row(self, blocker):
        self.sql("KILL CONNECTION " + str(blocker.connection))
        if blocker.process.poll() is None:
            blocker.process.kill()
        blocker.process.wait(timeout=5)

    async def blocked_query(self, prefix):
        query = ("SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                 "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                 "JOIN information_schema.PROCESSLIST p ON p.ID=t.PROCESSLIST_ID "
                 f"WHERE p.USER='{self.user}' AND p.DB='{self.database}' AND p.INFO LIKE '{prefix}%'")
        await self.wait_until(lambda: int(self.sql(query)) > 0,
                              "expected in-flight SQL did not block on an InnoDB row lock: " + prefix)

    def spawn(self, label, port, point=""):
        environment = self.environment.copy()
        barrier = self.evidence / label
        barrier.mkdir()
        if point:
            environment.update(ARENA_TEST_FAULT_POINT=point, ARENA_TEST_FAULT_DIRECTORY=str(barrier))
        log_path = self.evidence / (label + ".log")
        with log_path.open("wb") as log:
            process = subprocess.Popen([str(Path(self.args.server).resolve()), str(port)],
                                       cwd=ROOT, env=environment, stdout=log, stderr=log)
        self.processes.append(process)
        return SimpleNamespace(process=process, barrier=barrier, log_path=log_path)

    async def ready(self, server, port):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.process.poll() is not None:
                raise AssertionError(server.log_path.read_text(errors="replace"))
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.02)
            else:
                writer.close()
                await writer.wait_closed()
                return
        raise AssertionError("server did not listen")

    async def wait_until(self, function, description, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = await asyncio.to_thread(function)
            if result:
                return result
            await asyncio.sleep(0.05)
        raise AssertionError(description)

    async def barrier(self, server, point, match_id):
        await self.wait_until(lambda: (server.barrier / "ready").exists(), "fault barrier was not reached")
        if (server.barrier / "ready").read_text().splitlines() != [point, match_id]:
            raise AssertionError("wrong crash window or match at test barrier")
        if server.process.poll() is not None:
            raise AssertionError("server exited before deliberate crash")

    def kill(self, server):
        if server.process.poll() is None:
            server.process.kill()
            server.process.wait(timeout=5)

    def client_args(self, protocol, port):
        return SimpleNamespace(protocol=protocol, port=port, mysql_client=self.args.mysql_client,
                               mysql_container="")

    async def new_match(self, args, label):
        names = (label + "_a_" + self.suffix, label + "_b_" + self.suffix)
        self.names.extend(names)
        pair = await create_match(args, names, self.connections)
        self.matches.append(pair[0].snapshot["match_id"])
        return pair, names

    async def resume(self, args, original, terminal=False):
        pair = []
        for previous in original:
            player = await connect(args, token=previous.token)
            self.connections.append(player)
            response = await player.receive("MatchResult" if terminal else "BattleSnapshot")
            pair.append((player, response))
        return pair

    async def assert_no_delivery(self, pair, kinds):
        for player in pair:
            try:
                message = await player.receive(kinds, timeout=0.15)
            except asyncio.TimeoutError:
                continue
            raise AssertionError("message escaped before deliberate crash: " + str(message))

    async def finish(self, pair):
        for _ in range(41):
            player = pair[int(pair[0].snapshot["turn"])]
            request = {"match_id": player.snapshot["match_id"],
                       "turn_id": int(player.snapshot["turn_id"]),
                       "action_id": int(player.snapshot["last_action_id"]) + 1}
            _, updates = await action(pair, "EndTurnReq", request)
            if updates[0]["type"] == "MatchResult":
                return updates[0]["payload"]
        raise AssertionError("resumed room did not finish")

    async def settled(self, match_id, names, winner=0):
        await self.wait_until(lambda: self.sql("SELECT status FROM settlement_outbox WHERE match_id='" +
                             match_id + "'") == "applied", "Outbox did not become applied")
        rows = self.sql("SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "';"
                        "SELECT rating,wins,losses FROM players WHERE player_id='" + names[winner] + "';"
                        "SELECT rating,wins,losses FROM players WHERE player_id='" + names[1-winner] + "';"
                        "SELECT COUNT(*) FROM settlement_outbox WHERE match_id='" + match_id + "';")
        if rows.splitlines() != ["1", "1010\t1\t0", "990\t0\t1", "1"]:
            raise AssertionError("settlement duplicated or incorrect: " + rows)
        redis = RedisClient(self.args.redis_host, self.args.redis_port)
        try:
            if ([int(float(redis.command("ZSCORE", "arena:leaderboard:rating", names[i])))
                 for i in (winner, 1-winner)] != [1010, 990]):
                raise AssertionError("Redis differs from authoritative settlement")
        finally:
            redis.close()
        return rows

    async def checkpoint_window(self, protocol):
        args = self.client_args(protocol, self.args.port)
        server = self.spawn(protocol + "-checkpoint", args.port, "checkpoint_before_ack")
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "checkpoint_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1,
                   "card": 1, "request_id": "unacknowledged-durable-action"}
        await pair[0].send("PlayCardReq", request)
        await self.barrier(server, "checkpoint_before_ack", match_id)
        durable = await read_checkpoint(args, match_id)
        if durable["revision"] != 2 or durable["players"][1]["state"][0] != 22:
            raise AssertionError("barrier is not after the durable action write")
        await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        self.kill(server)
        restarted = self.spawn(protocol + "-checkpoint-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [player for player, _ in await self.resume(args, pair)]
        if await read_checkpoint(args, match_id) != durable:
            raise AssertionError("checkpoint/RNG/receipts/deadline changed across COMMIT-before-ACK crash")
        await resumed[0].send("PlayCardReq", request)
        receipt = await resumed[0].receive("ActionAck")
        original = durable["players"][0]["receipts"][0][2]
        payload = expected_response("ActionAck", original, protocol)
        if receipt["payload"] != payload or await read_checkpoint(args, match_id) != durable:
            raise AssertionError("retry did not return original durable ACK exactly once")
        await self.assert_no_delivery(resumed, ("BattleEvent", "BattleSnapshot"))
        await self.finish(resumed)
        await self.settled(match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "checkpoint_COMMIT_before_ACK", "passed": True})

    async def settlement_window(self, protocol, before_redis=False):
        args = self.client_args(protocol, self.args.port)
        label = "redis" if before_redis else "settlement"
        point = "redis_before_apply" if before_redis else "settlement_before_result"
        server = self.spawn(protocol + "-" + label, args.port, point)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, label + "_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        await action(pair, "PlayCardReq", {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1})
        for _ in range(41):
            current = pair[int(pair[0].snapshot["turn"])]
            turn = int(current.snapshot["turn_id"])
            request = {"match_id": match_id, "turn_id": turn,
                       "action_id": int(current.snapshot["last_action_id"]) + 1}
            if turn == 40:
                await current.send("EndTurnReq", request)
                await current.receive("ActionAck")
                break
            await action(pair, "EndTurnReq", request)
        await self.barrier(server, point, match_id)
        durable = await read_checkpoint(args, match_id)
        if not durable["pending_result"] or self.sql("SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "'") != "1":
            raise AssertionError("settlement barrier was not after COMMIT")
        if before_redis:
            expected = expected_response("MatchResult", durable["pending_result"], protocol)
            delivered = await asyncio.gather(*(player.receive("MatchResult", timeout=2) for player in pair))
            if any(response["payload"] != expected for response in delivered):
                raise AssertionError("Redis pre-apply barrier delayed or changed committed result")
            if self.sql("SELECT status FROM settlement_outbox WHERE match_id='" + match_id + "'") != "pending":
                raise AssertionError("Outbox was applied before the Redis crash window")
            redis = RedisClient(self.args.redis_host, self.args.redis_port)
            try:
                if redis.command("EXISTS", "arena:match:rank:" + match_id) == 1 or any(
                        redis.command("ZSCORE", "arena:leaderboard:rating", name) is not None for name in names):
                    raise AssertionError("a Redis writer escaped the shared pre-apply barrier")
            finally:
                redis.close()
            before = None
        else:
            await self.assert_no_delivery(pair, "MatchResult")
            before = await self.settled(match_id, names)
        self.kill(server)
        restarted = self.spawn(protocol + "-" + label + "-restart", args.port)
        await self.ready(restarted, args.port)
        responses = await self.resume(args, pair, terminal=True)
        expected = expected_response("MatchResult", durable["pending_result"], protocol)
        if any(response["payload"] != expected for _, response in responses):
            raise AssertionError("recovered settlement result differs from original pending result")
        after = await self.settled(match_id, names)
        if before is not None and after != before:
            raise AssertionError("recovery applied settlement twice")
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "settlement_COMMIT_before_Redis" if before_redis
                             else "settlement_COMMIT_before_result", "passed": True})

    async def redis_window(self, protocol):
        await self.settlement_window(protocol, before_redis=True)

    async def batch_precommit(self, protocol):
        args = self.client_args(protocol, self.args.port)
        server = self.spawn(protocol + "-batch-precommit", args.port, "checkpoint_batch_before_commit")
        await self.ready(server, args.port)
        first, names_a = await self.new_match(args, "batch_a_" + protocol)
        second, names_b = await self.new_match(args, "batch_b_" + protocol)
        pairs = [first, second]
        ids = [pair[0].snapshot["match_id"] for pair in pairs]
        before = [await read_checkpoint(args, match_id) for match_id in ids]
        requests = [{"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1,
                     "request_id": "batch-precommit-" + str(index)} for index, match_id in enumerate(ids)]
        await asyncio.gather(*(pair[0].send("PlayCardReq", request) for pair, request in zip(pairs, requests)))
        await self.wait_until(lambda: (server.barrier / "ready").exists(), "batch precommit barrier missing")
        if (server.barrier / "ready").read_text().splitlines()[0] != "checkpoint_batch_before_commit":
            raise AssertionError("wrong batch barrier")
        # data_locks proves both UPDATEs occurred in the same uncommitted transaction.
        lock_query = ("SELECT COUNT(DISTINCT l.LOCK_DATA) FROM performance_schema.data_locks l "
                      "JOIN performance_schema.threads t ON t.THREAD_ID=l.THREAD_ID "
                      "JOIN information_schema.PROCESSLIST p ON p.ID=t.PROCESSLIST_ID "
                      f"WHERE p.USER='{self.user}' AND l.OBJECT_SCHEMA='{self.database}' "
                      "AND l.OBJECT_NAME='room_checkpoints' AND l.LOCK_TYPE='RECORD' AND l.LOCK_MODE LIKE 'X%'")
        if int(self.sql(lock_query)) < 2:
            raise AssertionError("two room checkpoints did not enter the same transaction")
        for pair in pairs:
            await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        if [await read_checkpoint(args, match_id) for match_id in ids] != before:
            raise AssertionError("uncommitted batch leaked durable state")
        self.kill(server)
        await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                             f"WHERE USER='{self.user}'") == "0", "crashed batch session still running")
        restarted = self.spawn(protocol + "-batch-precommit-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = []
        for pair in pairs:
            resumed.append([player for player, _ in await self.resume(args, pair)])
        if [await read_checkpoint(args, match_id) for match_id in ids] != before:
            raise AssertionError("crashed batch did not roll back every member")
        await asyncio.gather(*(action(pair, "PlayCardReq", request) for pair, request in zip(resumed, requests)))
        for pair, request in zip(resumed, requests):
            await pair[0].send("PlayCardReq", request)
            ack = await pair[0].receive("ActionAck")
            document = await read_checkpoint(args, request["match_id"])
            if document["revision"] != 2 or ack["payload"] != expected_response(
                    "ActionAck", document["players"][0]["receipts"][0][2], protocol):
                raise AssertionError("batch rollback retry duplicated or changed original ACK")
            await self.assert_no_delivery(pair, ("BattleEvent", "BattleSnapshot"))
        await asyncio.gather(*(self.finish(pair) for pair in resumed))
        for match_id, names in zip(ids, (names_a, names_b)):
            await self.settled(match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "two_room_batch_before_COMMIT", "passed": True})

    async def checkpoint_inflight(self, protocol):
        args = self.client_args(protocol, self.args.port)
        server = self.spawn(protocol + "-checkpoint-inflight", args.port)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "inflight_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        before = await read_checkpoint(args, match_id)
        blocker = await self.hold_row(protocol + "-checkpoint", "SELECT match_id FROM room_checkpoints "
                                      "WHERE match_id='" + match_id + "' FOR UPDATE")
        request = {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1,
                   "request_id": "inflight-action"}
        await pair[0].send("PlayCardReq", request)
        await self.blocked_query("UPDATE room_checkpoints")
        await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        self.kill(server)
        self.release_row(blocker)
        await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                             f"WHERE USER='{self.user}'") == "0", "killed checkpoint session did not close")
        durable = await read_checkpoint(args, match_id)
        # A killed TCP client can leave a blocked autocommit query executing.
        # The unacknowledged command may commit; either complete document is valid.
        committed = durable["revision"] == before["revision"] + 1
        if not committed and durable != before:
            raise AssertionError("in-flight checkpoint produced a partial document")
        if committed and (durable["turn_id"] != 2 or durable["players"][1]["state"][0] != 22 or
                          durable["players"][0]["state"][3] != 1 or
                          len(durable["players"][0]["receipts"]) != 1):
            raise AssertionError("in-flight committed checkpoint is inconsistent")
        restarted = self.spawn(protocol + "-checkpoint-inflight-restart", args.port)
        await self.ready(restarted, args.port)
        resumed = [player for player, _ in await self.resume(args, pair)]
        if await read_checkpoint(args, match_id) != durable:
            raise AssertionError("restart changed the atomic durable checkpoint")
        if committed:
            await resumed[0].send("PlayCardReq", request)
            ack = await resumed[0].receive("ActionAck")
            if ack["payload"] != expected_response("ActionAck", durable["players"][0]["receipts"][0][2], protocol):
                raise AssertionError("ambiguous committed write lost its original ACK")
        else:
            ack, _ = await action(resumed, "PlayCardReq", request)
        after = await read_checkpoint(args, match_id)
        if after["revision"] != before["revision"] + 1 or after["players"][1]["state"][0] != 22:
            raise AssertionError("unconfirmed action retry did not apply once")
        await resumed[0].send("PlayCardReq", request)
        if (await resumed[0].receive("ActionAck"))["payload"] != ack["payload"]:
            raise AssertionError("in-flight crash retry did not preserve its ACK")
        await self.assert_no_delivery(resumed, ("BattleEvent", "BattleSnapshot"))
        await self.finish(resumed)
        await self.settled(match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "checkpoint_SQL_inflight",
                             "unacknowledged_write_committed": committed, "passed": True})

    async def settlement_precommit(self, protocol):
        args = self.client_args(protocol, self.args.port)
        server = self.spawn(protocol + "-precommit", args.port)
        await self.ready(server, args.port)
        pair, names = await self.new_match(args, "precommit_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        await action(pair, "PlayCardReq", {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1})
        for _ in range(39):
            current = pair[int(pair[0].snapshot["turn"])]
            turn = int(current.snapshot["turn_id"])
            if turn == 40:
                break
            await action(pair, "EndTurnReq", {"match_id": match_id, "turn_id": turn,
                         "action_id": int(current.snapshot["last_action_id"]) + 1})
        blocker = await self.hold_row(protocol + "-precommit", "SELECT match_id FROM match_results "
                                      "WHERE match_id='" + match_id + "' FOR UPDATE")
        current = pair[int(pair[0].snapshot["turn"])]
        await current.send("EndTurnReq", {"match_id": match_id, "turn_id": 40,
                           "action_id": int(current.snapshot["last_action_id"]) + 1})
        await current.receive("ActionAck")
        await self.blocked_query("INSERT INTO match_results")
        durable = await read_checkpoint(args, match_id)
        if not durable["pending_result"]:
            raise AssertionError("terminal checkpoint was not durable before settlement")
        rows = self.sql("SELECT COUNT(*) FROM match_results; SELECT rating FROM players ORDER BY player_id;")
        if rows.splitlines() != ["0", "1000", "1000"]:
            raise AssertionError("uncommitted settlement leaked authoritative updates")
        await self.assert_no_delivery(pair, "MatchResult")
        self.kill(server)
        self.release_row(blocker)
        await self.wait_until(lambda: self.sql("SELECT COUNT(*) FROM information_schema.innodb_trx t "
                             "JOIN information_schema.PROCESSLIST p ON p.ID=t.trx_mysql_thread_id "
                             f"WHERE p.USER='{self.user}'") == "0", "killed settlement did not roll back")
        if self.sql("SELECT COUNT(*) FROM match_results; SELECT rating FROM players ORDER BY player_id;") != rows:
            raise AssertionError("pre-COMMIT crash did not roll back transaction")
        restarted = self.spawn(protocol + "-precommit-restart", args.port)
        await self.ready(restarted, args.port)
        responses = await self.resume(args, pair, terminal=True)
        expected = expected_response("MatchResult", durable["pending_result"], protocol)
        if any(response["payload"] != expected for _, response in responses):
            raise AssertionError("pending terminal result changed after rollback/restart")
        await self.settled(match_id, names)
        self.kill(restarted)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "settlement_transaction_before_COMMIT", "passed": True})

    async def process_competition(self, protocol):
        args = self.client_args(protocol, self.args.port)
        old = self.spawn(protocol + "-old-owner", args.port)
        await self.ready(old, args.port)
        pair, names = await self.new_match(args, "competition_" + protocol)
        match_id = pair[0].snapshot["match_id"]
        durable = await read_checkpoint(args, match_id)
        owner = self.sql("SELECT owner_id FROM room_checkpoints WHERE match_id='" + match_id + "'")
        denied = self.spawn(protocol + "-denied-owner", args.port + 1)
        code = await asyncio.to_thread(denied.process.wait, 5)
        if code != 3 or "instance lock is already held" not in denied.log_path.read_text():
            raise AssertionError("second real recovery process did not reject startup")
        os.kill(old.process.pid, signal.SIGSTOP)
        connection = self.sql("SELECT IS_USED_LOCK('" + self.lock_name + "')")
        if not connection.isdecimal():
            raise AssertionError("old owner has no real MySQL lock session")
        self.sql("KILL CONNECTION " + connection)
        successor_args = self.client_args(protocol, args.port + 1)
        successor = self.spawn(protocol + "-new-owner", successor_args.port)
        await self.ready(successor, successor_args.port)
        new_owner = self.sql("SELECT owner_id FROM room_checkpoints WHERE match_id='" + match_id + "'")
        await self.resume(successor_args, pair)
        claimed = await read_checkpoint(args, match_id)
        if new_owner == owner or claimed != durable:
            (self.evidence / (protocol + "-claim-difference.json")).write_text(json.dumps(
                {"owner_changed": new_owner != owner, "before": durable, "claimed": claimed}, indent=2))
            raise AssertionError("successor claim differs: owner_changed=" + str(new_owner != owner) +
                                 "; checkpoint_fields=" + str([key for key in durable if durable[key] != claimed[key]]))
        os.kill(old.process.pid, signal.SIGCONT)
        await pair[0].send("PlayCardReq", {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1})
        await self.wait_until(lambda: "instance lock is already held" in old.log_path.read_text(),
                              "old process did not attempt reconnect while successor owned lock")
        await self.assert_no_delivery(pair, ("ActionAck", "BattleEvent", "BattleSnapshot"))
        self.kill(successor)
        await self.wait_until(lambda: "room_checkpoint_owner_changed" in old.log_path.read_text(),
                              "old process was not fenced after reacquiring the database lock")
        if await read_checkpoint(args, match_id) != durable or self.sql("SELECT COUNT(*) FROM match_results") != "0":
            raise AssertionError("old owner modified successor's checkpoint or settled stale state")
        self.kill(old)
        final = self.spawn(protocol + "-final-owner", successor_args.port)
        await self.ready(final, successor_args.port)
        resumed = [player for player, _ in await self.resume(successor_args, pair)]
        await action(resumed, "PlayCardReq", {"match_id": match_id, "turn_id": 1, "action_id": 1, "card": 1})
        await self.finish(resumed)
        await self.settled(match_id, names)
        self.kill(final)
        self.clear_matches()
        self.results.append({"protocol": protocol, "window": "real_process_lock_loss_reconnect_owner_fence", "passed": True})

    def clear_matches(self):
        self.sql("DELETE FROM room_checkpoints; DELETE FROM settlement_outbox; DELETE FROM match_results; "
                 "DELETE FROM matches; DELETE FROM players;")

    async def run(self):
        tests = {"checkpoint": self.checkpoint_window, "settlement": self.settlement_window,
                 "competition": self.process_competition, "checkpoint-inflight": self.checkpoint_inflight,
                 "settlement-precommit": self.settlement_precommit, "redis": self.redis_window,
                 "batch": self.batch_precommit}
        protocols = ("text_v1", "proto_v1") if self.args.protocol == "both" else (self.args.protocol,)
        for protocol in protocols:
            for name in self.args.scenarios:
                test = tests[name]
                await test(protocol)
                print(json.dumps(self.results[-1]), flush=True)

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        redis = None
        try:
            redis = RedisClient(self.args.redis_host, self.args.redis_port)
            if self.names:
                redis.command("ZREM", "arena:leaderboard:rating", *self.names)
            if self.matches:
                redis.command("DEL", *("arena:match:rank:" + match_id for match_id in self.matches))
        finally:
            try:
                if redis is not None:
                    redis.close()
            finally:
                self.sql(f"DROP DATABASE IF EXISTS `{self.database}`; DROP USER IF EXISTS '{self.user}'@'%';",
                         database=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19200)
    parser.add_argument("--evidence", default="build-sanitizers/recovery-windows")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1", "both"), default="both")
    parser.add_argument("--scenarios", nargs="+", choices=("checkpoint", "settlement", "competition",
                        "checkpoint-inflight", "settlement-precommit", "redis", "batch"),
                        default=("checkpoint", "settlement", "competition", "checkpoint-inflight",
                                 "settlement-precommit", "redis", "batch"))
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Linux is required for real process SIGSTOP/SIGCONT fault sequencing")
    acceptance = Acceptance(args)
    original = os.environ.copy()
    summary = {"passed": False, "results": acceptance.results}
    try:
        acceptance.setup()
        os.environ.update(acceptance.environment)
        asyncio.run(acceptance.run())
        findings = [str(path) for pattern in ("asan.*", "ubsan.*")
                    for path in acceptance.evidence.glob(pattern) if path.stat().st_size]
        summary["sanitizer_reports"] = findings
        if findings:
            raise AssertionError("sanitizer reports were emitted: " + str(findings))
        summary["passed"] = True
        return 0
    except (AssertionError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        summary["error"] = str(error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        cleanup_error = None
        try:
            acceptance.cleanup()
            summary["fixtures_cleaned"] = True
        except (OSError, subprocess.SubprocessError) as error:
            summary["passed"] = False
            summary["fixtures_cleaned"] = False
            summary["cleanup_error"] = str(error)
            cleanup_error = error
        finally:
            os.environ.clear()
            os.environ.update(original)
            (acceptance.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print("Evidence: " + str(acceptance.evidence), flush=True)
        if cleanup_error is not None and not primary_error:
            raise cleanup_error


if __name__ == "__main__":
    raise SystemExit(main())
