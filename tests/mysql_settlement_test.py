"""Optional end-to-end MySQL settlement test for a dedicated test database."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message, negotiate_protocol
from tools.ranking.rebuild_leaderboard import RedisClient


def mysql_query(client: str, query: str, container: str = "") -> str:
    env = os.environ.copy()
    env["MYSQL_PWD"] = env.get("ARENA_MYSQL_PASSWORD", "")
    if container:
        command = [
            "docker", "exec", "-e", "MYSQL_PWD=" + env["MYSQL_PWD"], container, "mysql",
            "--batch", "--skip-column-names", "--host=127.0.0.1", "--port=3306",
            "--user=" + env["ARENA_MYSQL_USER"], env["ARENA_MYSQL_DATABASE"], "-e", query,
        ]
    else:
        command = [
            client, "--batch", "--skip-column-names",
            "--host=" + env.get("ARENA_MYSQL_HOST", "127.0.0.1"),
            "--port=" + env.get("ARENA_MYSQL_PORT", "3306"),
            "--user=" + env["ARENA_MYSQL_USER"], env["ARENA_MYSQL_DATABASE"], "-e", query,
        ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "mysql client failed")
    return result.stdout.strip()


async def receive(reader, wanted, protocol="text_v1"):
    while True:
        message = await asyncio.wait_for(read_message(reader, protocol=protocol), 10)
        if message["type"] == wanted:
            return message
        if message["type"] == "Error":
            raise RuntimeError(str(message["payload"]))


async def verify_metrics(port: int, min_outbox_attempts: int) -> None:
    names = (
        "mysql_connection_attempts", "mysql_connection_successes", "mysql_connection_failures",
        "mysql_connection_losses", "redis_connection_failures", "redis_apply_failures",
        "settlement_outbox_load_failures", "settlement_outbox_mark_failures",
        "settlement_outbox_record_failures", "settlement_outbox_applied",
        "settlement_outbox_failures", "settlement_outbox_pending",
    )
    for protocol in ("text_v1", "proto_v1"):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            if protocol == "proto_v1":
                await negotiate_protocol(reader, writer, protocol)
            request_id = "settlement-metrics-" + protocol
            await send_message(writer, "AdminRoomsReq", {"request_id": request_id}, protocol=protocol)
            response = await receive(reader, "AdminRoomsResp", protocol)
            values = {name: int(response["payload"].get(name, "-1")) for name in names}
            if any(not 0 <= value <= 2**64 - 1 for value in values.values()):
                raise AssertionError(f"missing or invalid {protocol} persistence metrics: {values}")
            if protocol == "proto_v1" and response["payload"].get("request_id") != request_id:
                raise AssertionError("ProtoV1 persistence metrics lost request correlation")
            if values["mysql_connection_attempts"] < 1 or values["mysql_connection_successes"] < 1:
                raise AssertionError(f"MySQL connection metrics were not recorded: {values}")
            if min(values["redis_apply_failures"], values["settlement_outbox_failures"]) < min_outbox_attempts:
                raise AssertionError(f"Outbox retry failure metrics were too low: {values}")
            print("persistence metrics passed", protocol, values)
        finally:
            writer.close()
            await writer.wait_closed()


async def wait_for_server(port: int, process: subprocess.Popen | None) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"server exited before listening (exit={process.returncode})")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.1)
        else:
            writer.close()
            await writer.wait_closed()
            return
    raise TimeoutError("server did not start listening within 10 seconds")


async def run_match(port: int, names: tuple[str, str], process: subprocess.Popen | None,
                    protocol: str = "text_v1", allow_settlement_pending: bool = False) -> str:
    await wait_for_server(port, process)
    players = []
    try:
        for name in names:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            players.append((reader, writer))
            if protocol == "proto_v1":
                await negotiate_protocol(reader, writer, protocol)
            await send_message(writer, "LoginReq", name, protocol=protocol)
            login = await receive(reader, "LoginResp", protocol)
            if login["payload"].get("ok") != "1":
                raise RuntimeError("login failed")
            await send_message(writer, "MatchJoinReq", "", protocol=protocol)

        async def play(index: int) -> tuple[str, int, int]:
            reader, writer = players[index]
            match_id = ""
            action_id = 0
            first_action_id = None
            duplicate_acks = 0
            while True:
                message = await asyncio.wait_for(read_message(reader, protocol=protocol), 15)
                kind, payload = message["type"], message["payload"]
                if kind == "MatchFound":
                    match_id = payload["match_id"]
                elif kind == "BattleSnapshot":
                    match_id = payload["match_id"]
                    if int(payload.get("turn", -1)) != int(payload.get("player_index", -2)):
                        continue
                    action_id += 1
                    request = {"match_id": match_id, "turn_id": int(payload["turn_id"]), "action_id": action_id}
                    hand = [int(card) for card in payload.get("hand", "").split(",") if card]
                    if index == 0 and 1 in hand and int(payload["p0_energy"]) >= 2:
                        request["card"] = 1
                        await send_message(writer, "PlayCardReq", request, protocol=protocol)
                        if first_action_id is None:
                            first_action_id = action_id
                            await send_message(writer, "PlayCardReq", request, protocol=protocol)
                    else:
                        await send_message(writer, "EndTurnReq", request, protocol=protocol)
                elif kind == "ActionAck" and index == 0 and payload.get("action_id") == str(first_action_id):
                    duplicate_acks += 1
                elif kind == "MatchResult":
                    return match_id, int(payload["winner"]), duplicate_acks
                elif kind == "Error":
                    if allow_settlement_pending and payload.get("code") == "settlement_pending":
                        continue
                    raise RuntimeError(str(payload))

        results = await asyncio.wait_for(asyncio.gather(play(0), play(1)), 30)
        if results[0][0] != results[1][0] or not results[0][0]:
            raise AssertionError(f"players received different matches: {results}")
        if results[0][1] != 0 or results[1][1] != 0:
            raise AssertionError(f"expected player 0 to win: {results}")
        if results[0][2] != 2:
            raise AssertionError(f"duplicate action should return two receipts: {results}")
        return results[0][0]
    finally:
        for _, writer in players:
            writer.close()
            await writer.wait_closed()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="", help="local server executable; omit with --existing-server")
    parser.add_argument("--port", type=int, default=19104)
    parser.add_argument("--mysql-client", default=os.getenv("ARENA_MYSQL_CLIENT", "mysql"))
    parser.add_argument("--mysql-container", default=os.getenv("ARENA_MYSQL_CONTAINER", ""))
    parser.add_argument("--verify-redis", action="store_true", default=os.getenv("ARENA_REDIS_VERIFY", "") == "1")
    parser.add_argument("--verify-outbox", action="store_true", default=os.getenv("ARENA_OUTBOX_VERIFY", "") == "1")
    parser.add_argument("--verify-metrics", action="store_true", help="verify TextV1 and ProtoV1 persistence counters")
    parser.add_argument("--min-outbox-attempts", type=int, default=0)
    parser.add_argument("--redis-host", default=os.getenv("ARENA_REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.getenv("ARENA_REDIS_PORT", "6379")))
    parser.add_argument("--existing-server", action="store_true", help="use a server already listening on --port")
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    args = parser.parse_args()
    if not args.existing_server and not args.server:
        parser.error("--server is required unless --existing-server is set")
    required = ("ARENA_MYSQL_USER", "ARENA_MYSQL_DATABASE")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit("Set these variables for a dedicated test database: " + ", ".join(missing))

    suffix = uuid.uuid4().hex[:12]
    names = ("test_a_" + suffix, "test_b_" + suffix)
    process = None
    if not args.existing_server:
        server_env = os.environ.copy()
        server_env["ARENA_MYSQL_ENABLED"] = "1"
        server_env["ARENA_MYSQL_REQUIRED"] = "1"
        process = subprocess.Popen([os.path.abspath(args.server), str(args.port)], cwd=ROOT, env=server_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    match_id = ""
    try:
        match_id = asyncio.run(run_match(args.port, names, process, args.protocol))
        if not match_id:
            raise AssertionError("server did not provide match_id")
        escaped_id = match_id.replace("'", "''")
        result = mysql_query(args.mysql_client,
            "SELECT m.status,m.winner_id,r.winner_id,r.loser_id,r.winner_rating_delta,r.loser_rating_delta "
            "FROM matches m JOIN match_results r ON r.match_id=m.match_id WHERE m.match_id='" + escaped_id + "'",
            args.mysql_container)
        expected = "\t".join(("finished", names[0], names[0], names[1], "10", "-10"))
        if result != expected:
            raise AssertionError(f"unexpected committed result: {result!r}, expected {expected!r}")
        players = mysql_query(args.mysql_client,
            "SELECT player_id,rating,wins,losses FROM players WHERE player_id IN ('" +
            names[0] + "','" + names[1] + "') ORDER BY player_id",
            args.mysql_container)
        expected_players = "\n".join((f"{names[0]}\t1010\t1\t0", f"{names[1]}\t990\t0\t1"))
        if players != expected_players:
            raise AssertionError(f"unexpected ratings or counters: {players!r}")
        outbox = ""
        for _ in range(30):
            outbox = mysql_query(args.mysql_client,
                "SELECT status,winner_id,loser_id,winner_rating_delta,loser_rating_delta,attempts "
                "FROM settlement_outbox WHERE match_id='" + escaped_id + "'",
                args.mysql_container)
            if not args.verify_outbox or (outbox and outbox.split("\t", 1)[0] == "applied"):
                break
            time.sleep(0.2)
        expected_outbox = "\t".join(("applied" if args.verify_outbox else outbox.split("\t", 1)[0],
                                      names[0], names[1], "10", "-10", outbox.split("\t")[-1]))
        if not outbox or outbox.split("\t")[1:5] != expected_outbox.split("\t")[1:5]:
            raise AssertionError(f"unexpected settlement outbox row: {outbox!r}")
        if args.verify_outbox and outbox.split("\t")[0] != "applied":
            raise AssertionError(f"settlement outbox was not consumed: {outbox!r}")
        if int(outbox.split("\t")[5]) < args.min_outbox_attempts:
            raise AssertionError(f"settlement outbox retry count was too low: {outbox!r}")
        if args.verify_redis:
            redis = RedisClient(args.redis_host, args.redis_port)
            try:
                winner_score = redis.command("ZSCORE", "arena:leaderboard:rating", names[0])
                loser_score = redis.command("ZSCORE", "arena:leaderboard:rating", names[1])
                if winner_score != "1010" or loser_score != "990":
                    raise AssertionError(f"unexpected Redis scores: winner={winner_score!r}, loser={loser_score!r}")
            finally:
                redis.command("ZREM", "arena:leaderboard:rating", names[0], names[1])
                redis.command("DEL", "arena:match:rank:" + match_id)
                redis.close()
        if args.verify_metrics:
            asyncio.run(verify_metrics(args.port, args.min_outbox_attempts))
        print("mysql settlement test passed", {"match_id": match_id, "winner": names[0], "protocol": args.protocol})
        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        if match_id:
            try:
                safe_id = match_id.replace("'", "''")
                recovery_table = mysql_query(args.mysql_client,
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=DATABASE() "
                    "AND TABLE_NAME='room_checkpoints'", args.mysql_container)
                if recovery_table.strip() == "1":
                    mysql_query(args.mysql_client, "DELETE FROM room_checkpoints WHERE match_id='" + safe_id + "'",
                                args.mysql_container)
                mysql_query(args.mysql_client, "DELETE FROM settlement_outbox WHERE match_id='" + safe_id + "'", args.mysql_container)
                mysql_query(args.mysql_client, "DELETE FROM match_results WHERE match_id='" + safe_id + "'", args.mysql_container)
                mysql_query(args.mysql_client, "DELETE FROM matches WHERE match_id='" + safe_id + "'", args.mysql_container)
                for name in names:
                    safe_name = name.replace("'", "''")
                    mysql_query(args.mysql_client, "DELETE FROM players WHERE player_id='" + safe_name + "'", args.mysql_container)
            except RuntimeError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
