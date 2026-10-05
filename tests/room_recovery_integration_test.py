"""Optional real-MySQL crash/resume acceptance; uses only this run's random users.

Docker mode requires an existing server configured with ARENA_ROOM_RECOVERY=1,
ARENA_MYSQL_REQUIRED=1, and ARENA_MYSQL_CLEANUP_RUNNING=0. It kills and starts
that container without changing its configuration or deleting any data volume.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "client_pygame"))
from mysql_settlement_test import mysql_query
from protocol import negotiate_protocol, read_message, send_message


def command(*arguments: str) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout.strip()


class Server:
    def __init__(self, args):
        self.args = args
        self.process = None
        self.temporary = tempfile.TemporaryDirectory(prefix="arena-recovery-")
        self.log_path = Path(self.temporary.name) / "server.log"
        self.log = self.log_path.open("ab")
        self.redis_enabled = False
        self.env = os.environ.copy()
        self.env.update(ARENA_MYSQL_ENABLED="1", ARENA_MYSQL_REQUIRED="1",
                        ARENA_ROOM_RECOVERY="1", ARENA_MYSQL_CLEANUP_RUNNING="0",
                        ARENA_REPLAY_DIR=self.temporary.name)

    async def start(self):
        if self.args.docker_server:
            environment = json.loads(await asyncio.to_thread(command, "docker", "inspect", "--format",
                                                             "{{json .Config.Env}}", self.args.docker_server))
            settings = dict(item.split("=", 1) for item in environment)
            self.redis_enabled = settings.get("ARENA_REDIS_ENABLED", "").lower() in ("1", "true", "yes")
            expected = {"ARENA_ROOM_RECOVERY": "1", "ARENA_MYSQL_REQUIRED": "1",
                        "ARENA_MYSQL_CLEANUP_RUNNING": "0"}
            if any(settings.get(key) != value for key, value in expected.items()):
                raise RuntimeError(f"Docker server must already have recovery enabled: {expected}")
            running = await asyncio.to_thread(command, "docker", "inspect", "--format",
                                              "{{.State.Running}}", self.args.docker_server)
            if running != "true":
                await asyncio.to_thread(command, "docker", "start", self.args.docker_server)
        else:
            self.process = subprocess.Popen([str(Path(self.args.server).resolve()), str(self.args.port)],
                                            cwd=ROOT, env=self.env, stdout=self.log, stderr=self.log)
        await self.ready()

    async def ready(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("recovery server exited:\n" + self.log_path.read_text(errors="replace"))
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.args.port)
            except OSError:
                await asyncio.sleep(0.1)
            else:
                writer.close()
                await writer.wait_closed()
                return
        raise TimeoutError("recovery server did not listen within 20 seconds")

    async def crash_and_restart(self):
        if self.args.docker_server:
            await asyncio.to_thread(command, "docker", "kill", "--signal=KILL", self.args.docker_server)
        else:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, 5)
            self.process = None
        await asyncio.sleep(0.5)
        await self.start()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self.log.close()
        self.temporary.cleanup()


class Player:
    def __init__(self, reader, writer, protocol, token=""):
        self.reader, self.writer, self.protocol = reader, writer, protocol
        self.token = token
        self.snapshot = {}

    async def send(self, kind, payload):
        await send_message(self.writer, kind, payload, protocol=self.protocol)

    async def receive(self, kinds, timeout=10):
        if isinstance(kinds, str):
            kinds = (kinds,)
        deadline = time.monotonic() + timeout
        while True:
            message = await asyncio.wait_for(read_message(self.reader, protocol=self.protocol),
                                             max(0.01, deadline - time.monotonic()))
            if message["type"] == "Error" and "Error" not in kinds:
                raise AssertionError(f"unexpected server error: {message['payload']}")
            if message["type"] == "BattleSnapshot":
                self.snapshot = message["payload"]
            if message["type"] in kinds:
                return message

    async def close(self):
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


async def connect(args, token="", user=""):
    reader, writer = await asyncio.open_connection("127.0.0.1", args.port)
    player = Player(reader, writer, args.protocol, token)
    if args.protocol == "proto_v1":
        await negotiate_protocol(reader, writer, args.protocol)
    if token:
        await player.send("ReconnectReq", token)
        response = await player.receive("ReconnectResp")
        if response["payload"].get("ok") != "1":
            raise AssertionError(f"resume rejected: {response['payload']}")
    else:
        await player.send("LoginReq", user)
        response = await player.receive("LoginResp")
        player.token = response["payload"].get("token", "")
        if response["payload"].get("ok") != "1" or not player.token:
            raise AssertionError("login did not supply a resume token")
    return player


async def create_match(args, names, connections):
    pair = []
    for name in names:
        player = await connect(args, user=name)
        pair.append(player)
        connections.append(player)
        await player.send("MatchJoinReq", "")
    for player in pair:
        await player.receive("MatchFound")
        await player.receive("BattleSnapshot")
    if pair[0].snapshot["match_id"] != pair[1].snapshot["match_id"]:
        raise AssertionError("players joined different rooms")
    if [player.snapshot["player_index"] for player in pair] != ["0", "1"]:
        raise AssertionError("unexpected matchmaking player order")
    return pair


def checkpoint(args, match_id):
    encoded = mysql_query(args.mysql_client,
                          "SELECT HEX(checkpoint) FROM room_checkpoints WHERE match_id='" + match_id + "'",
                          args.mysql_container)
    if not encoded:
        raise AssertionError("durable room checkpoint is missing")
    data = bytes.fromhex(encoded)
    magic = b"ARENA_ROOM_RECOVERY_V1\n"
    if not data.startswith(magic):
        raise AssertionError("unexpected online checkpoint format")
    offset = len(magic)

    def integer():
        nonlocal offset
        value, = struct.unpack_from("<Q", data, offset)
        offset += 8
        return value

    def string():
        nonlocal offset
        length = integer()
        value = data[offset:offset + length]
        offset += length
        return value.decode("utf-8")

    def cards():
        return [integer() for _ in range(integer())]

    decoded = {"match_id": string(), "rules": string(), "version": integer(),
               "revision": integer(), "turn": integer(), "turn_id": integer(),
               "finished": integer(), "rng_state": integer(), "players": []}
    for _ in range(2):
        player = {"state": [integer() for _ in range(14)],
                  "cards": [cards() for _ in range(4)], "user": string(), "token": string(),
                  "disconnect_deadline": integer()}
        player["receipts"] = [(integer(), string(), string()) for _ in range(integer())]
        decoded["players"].append(player)
    decoded["turn_deadline"] = integer()
    decoded["pending_result"] = string()
    decoded["replay_valid"], decoded["terminal"] = integer(), integer()
    decoded["seed"] = integer()
    decoded["events"] = [(integer(), integer(), integer(), integer(), string(), string())
                         for _ in range(integer())]
    digest = 14695981039346656037
    for byte in data[:-8]:
        digest = ((digest ^ byte) * 1099511628211) & ((1 << 64) - 1)
    if offset != len(data) - 8 or integer() != digest:
        raise AssertionError("checkpoint binary boundaries or checksum differ")
    return decoded


async def read_checkpoint(args, match_id):
    return await asyncio.to_thread(checkpoint, args, match_id)


def stable_snapshot(payload):
    return {key: value for key, value in payload.items() if key not in ("remaining_ms", "request_id")}


async def action(pair, kind, request):
    player = pair[int(pair[0].snapshot["turn"])]
    await player.send(kind, request)
    ack = await player.receive("ActionAck")
    updates = await asyncio.gather(*(member.receive(("BattleSnapshot", "MatchResult")) for member in pair))
    if updates[0]["type"] != updates[1]["type"]:
        raise AssertionError("players observed different action outcomes")
    return ack, updates


def settlement_rows(args, match_id, names):
    return mysql_query(args.mysql_client,
        "SELECT status,winner_id,turn_count,result_reason FROM matches WHERE match_id='" + match_id + "';"
        "SELECT COUNT(*) FROM match_results WHERE match_id='" + match_id + "';"
        "SELECT player_id,rating,wins,losses FROM players WHERE player_id IN ('" +
        "','".join(names) + "') ORDER BY player_id;", args.mysql_container)


async def run(args, server, names):
    connections = []
    try:
        await server.start()
        pair = await create_match(args, names[:2], connections)
        match_id = pair[0].snapshot["match_id"]
        request = {"match_id": match_id, "turn_id": int(pair[0].snapshot["turn_id"]),
                   "action_id": 1, "card": 1, "request_id": "recovery-first-action"}
        original_ack, _ = await action(pair, "PlayCardReq", request)
        original_snapshots = [stable_snapshot(player.snapshot) for player in pair]
        original_checkpoint = await read_checkpoint(args, match_id)

        away = await create_match(args, names[2:], connections)
        away_match = away[0].snapshot["match_id"]
        await away[0].close()
        original_away = None
        for _ in range(20):
            original_away = await read_checkpoint(args, away_match)
            if original_away["players"][0]["disconnect_deadline"]:
                break
            await asyncio.sleep(0.1)
        if not original_away["players"][0]["disconnect_deadline"]:
            raise AssertionError("disconnect deadline was not persisted")

        await server.crash_and_restart()
        for player in pair + away:
            await player.close()
        resumed = []
        for player in pair:
            member = await connect(args, token=player.token)
            connections.append(member)
            await member.receive("BattleSnapshot")
            resumed.append(member)
        pair = resumed
        if [stable_snapshot(player.snapshot) for player in pair] != original_snapshots:
            raise AssertionError("private/public battle state or replay metadata changed across crash")
        restored = await read_checkpoint(args, match_id)
        if restored != original_checkpoint:
            raise AssertionError("RNG, decks, receipts, replay, or turn deadline changed across crash")
        remaining = int(pair[0].snapshot["remaining_ms"])
        expected = original_checkpoint["turn_deadline"] - int(time.time() * 1000)
        if not 0 <= remaining < 30000 or abs(remaining - expected) > 2000:
            raise AssertionError("restored turn timer reset or stopped during downtime")

        await pair[0].send("PlayCardReq", request)
        duplicate_ack = await pair[0].receive("ActionAck")
        if duplicate_ack["payload"] != original_ack["payload"]:
            raise AssertionError("crash resume did not preserve the exact original action ACK")
        if await read_checkpoint(args, match_id) != restored:
            raise AssertionError("duplicate action changed state or replay revision")

        survivor = await connect(args, token=away[1].token)
        connections.append(survivor)
        await survivor.receive("BattleSnapshot")
        restored_away = await read_checkpoint(args, away_match)
        if (restored_away["players"][0]["disconnect_deadline"] !=
                original_away["players"][0]["disconnect_deadline"]):
            raise AssertionError("already disconnected player received a fresh reconnect deadline")
        if restored_away["turn_deadline"] != original_away["turn_deadline"]:
            raise AssertionError("second room's turn deadline changed across crash")

        results = None
        for _ in range(41):
            current = pair[int(pair[0].snapshot["turn"])]
            next_action = int(current.snapshot["last_action_id"]) + 1
            _, updates = await action(pair, "EndTurnReq",
                {"match_id": match_id, "turn_id": int(current.snapshot["turn_id"]),
                 "action_id": next_action, "request_id": "recovery-end-" + str(next_action)})
            if updates[0]["type"] == "MatchResult":
                results = updates
                break
        if results is None or any(result["payload"].get("winner") != "0" for result in results):
            raise AssertionError("resumed room did not complete with the expected winner")
        before_settlement = await asyncio.to_thread(settlement_rows, args, match_id, names[:2])
        expected_ratings = (f"{names[0]}\t1010\t1\t0", f"{names[1]}\t990\t0\t1")
        if not all(row in before_settlement for row in expected_ratings):
            raise AssertionError("resumed settlement did not commit expected ratings exactly once")

        await server.crash_and_restart()
        for player in pair:
            member = await connect(args, token=player.token)
            connections.append(member)
            repeated_result = await member.receive("MatchResult")
            if repeated_result["payload"] != results[0]["payload"]:
                raise AssertionError("terminal result changed after another crash")
        after_settlement = await asyncio.to_thread(settlement_rows, args, match_id, names[:2])
        if after_settlement != before_settlement:
            raise AssertionError("terminal recovery duplicated settlement or ratings")
        survivor = await connect(args, token=away[1].token)
        connections.append(survivor)
        deadline_seconds = max(0, (original_away["players"][0]["disconnect_deadline"] - time.time() * 1000) / 1000)
        timeout_result = await survivor.receive("MatchResult", timeout=deadline_seconds + 3)
        if (timeout_result["payload"].get("reason") != "reconnect_timeout" or
                timeout_result["payload"].get("winner") != "1"):
            raise AssertionError("persisted disconnect deadline did not end the correct room")
        if time.time() * 1000 > original_away["players"][0]["disconnect_deadline"] + 3000:
            raise AssertionError("disconnect timeout was postponed by restart")
        print("room recovery integration passed", {"protocol": args.protocol,
              "crash_resume": True, "exact_ack": True, "private_state_rng": True,
              "turn_deadline": True, "disconnect_deadline": True,
              "complete_settlement": True, "terminal_idempotency": True})
    finally:
        for player in connections:
            await player.close()


def cleanup(args, names, redis_enabled):
    players = "('" + "','".join(names) + "')"
    only_this_run = "m.player_a IN " + players + " AND m.player_b IN " + players
    match_ids = mysql_query(args.mysql_client, "SELECT m.match_id FROM matches m WHERE " + only_this_run,
                            args.mysql_container).splitlines()
    mysql_query(args.mysql_client,
        "DELETE c FROM room_checkpoints c JOIN matches m ON m.match_id=c.match_id WHERE " + only_this_run + ";"
        "DELETE o FROM settlement_outbox o JOIN matches m ON m.match_id=o.match_id WHERE " + only_this_run + ";"
        "DELETE r FROM match_results r JOIN matches m ON m.match_id=r.match_id WHERE " + only_this_run + ";"
        "DELETE m FROM matches m WHERE " + only_this_run + ";"
        "DELETE FROM players WHERE player_id IN " + players + ";", args.mysql_container)
    if args.docker_server and redis_enabled:
        command("docker", "exec", args.redis_container, "redis-cli", "ZREM", "arena:leaderboard:rating", *names)
        if match_ids:
            command("docker", "exec", args.redis_container, "redis-cli", "DEL",
                    *("arena:match:rank:" + match_id for match_id in match_ids))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="")
    parser.add_argument("--port", type=int, default=19170)
    parser.add_argument("--mysql-client", default=os.getenv("ARENA_MYSQL_CLIENT", "mysql"))
    parser.add_argument("--mysql-container", default=os.getenv("ARENA_MYSQL_CONTAINER", ""))
    parser.add_argument("--redis-container", default="arena-cards-redis")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    parser.add_argument("--docker-server", nargs="?", const="arena-cards-server", default="")
    args = parser.parse_args()
    if bool(args.server) == bool(args.docker_server):
        parser.error("choose exactly one of --server or --docker-server")
    if args.docker_server and not args.mysql_container:
        parser.error("--docker-server requires --mysql-container")
    required = ("ARENA_MYSQL_USER", "ARENA_MYSQL_DATABASE")
    if any(not os.getenv(key) for key in required):
        parser.error("set ARENA_MYSQL_USER and ARENA_MYSQL_DATABASE for the test database")
    suffix = uuid.uuid4().hex[:12]
    names = tuple("rr_" + suffix + "_" + str(index) for index in range(4))
    server = Server(args)
    try:
        asyncio.run(run(args, server, names))
        return 0
    finally:
        server.close()
        cleanup(args, names, server.redis_enabled)


if __name__ == "__main__":
    raise SystemExit(main())
