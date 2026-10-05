"""Leaderboard protocol acceptance; real SQL fixtures are opt-in and cleaned up."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import negotiate_protocol, read_message, send_message
from mysql_settlement_test import mysql_query, wait_for_server


def sql_name(value: str) -> str:
    return "CONVERT(0x" + value.encode("utf-8").hex() + " USING utf8mb4)"


def expected_rows(args, limit: int) -> list[dict[str, str]]:
    result = mysql_query(args.mysql_client,
        "SELECT HEX(player_id),rating,wins,losses FROM players "
        "ORDER BY rating DESC,player_id ASC LIMIT " + str(limit), args.mysql_container)
    rows = []
    for line in result.splitlines():
        player, rating, wins, losses = line.split("\t")
        rows.append({"user": bytes.fromhex(player).decode("utf-8"), "rating": rating,
                     "wins": wins, "losses": losses})
    return rows


async def probe(args, protocol: str, names: list[str]) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", args.port)
    try:
        await negotiate_protocol(reader, writer, protocol)

        async def request(values, expected_code="", expected_limit=10):
            await send_message(writer, "LeaderboardReq", values, protocol=protocol)
            message = await asyncio.wait_for(read_message(reader, protocol=protocol), 5)
            if message["type"] != "LeaderboardResp":
                raise AssertionError(f"unexpected {protocol} leaderboard response: {message}")
            payload = message["payload"]
            correlation = values.get("request_id", "") if isinstance(values, dict) else "invalid-text"
            if payload.get("request_id", "") != correlation:
                raise AssertionError(f"leaderboard lost {protocol} request correlation: {payload}")
            if expected_code:
                if payload.get("ok") != "0" or payload.get("code") != expected_code or payload.get("count") != "0":
                    raise AssertionError(f"expected {expected_code}, received {payload}")
                if any(key.startswith("entry_") for key in payload):
                    raise AssertionError("failed query returned stale leaderboard rows")
                return
            if payload.get("ok") != "1" or payload.get("source") != "mysql":
                raise AssertionError(f"leaderboard did not report authoritative MySQL source: {payload}")
            expected = expected_rows(args, expected_limit)
            if int(payload.get("count", "-1")) != len(expected):
                raise AssertionError(f"leaderboard count differs from SQL: {payload}")
            for index, row in enumerate(expected):
                if payload.get(f"entry_{index}_rank") != str(index + 1):
                    raise AssertionError(f"incorrect sequential rank: {payload}")
                for key, value in row.items():
                    if payload.get(f"entry_{index}_{key}") != value:
                        raise AssertionError(f"{protocol} leaderboard {key} differs from SQL: {payload}")
            if expected_limit == 20 and not set(names).issubset({row["user"] for row in expected}):
                raise AssertionError("fixture rows were not present in the tested leaderboard")

        await request({"request_id": "before-login"}, "login_required")
        await send_message(writer, "LoginReq", "a" * 65, protocol=protocol)
        invalid_login = await asyncio.wait_for(read_message(reader, protocol=protocol), 5)
        if invalid_login["type"] != "Error" or invalid_login["payload"].get("code") != "invalid_username":
            raise AssertionError("65-byte username was accepted")
        await send_message(writer, "LoginReq", names[2] if names else "leaderboard-reader", protocol=protocol)
        login = await asyncio.wait_for(read_message(reader, protocol=protocol), 5)
        if login["type"] != "LoginResp" or login["payload"].get("ok") != "1":
            raise AssertionError(f"leaderboard login failed: {login}")
        if names and login["payload"].get("user") != names[2]:
            raise AssertionError("64-byte UTF-8 username changed during login")
        invalid_limits = (0, 21, 4294967295)
        for limit in invalid_limits:
            await request({"limit": limit, "request_id": f"invalid-{limit}"}, "invalid_limit")
        if protocol == "text_v1":
            for text in ("-1", "word", "1junk", "4294967296", "10;limit=1"):
                await request("limit=" + text + ";request_id=invalid-text", "invalid_limit")
        missing = "" if args.mysql else "leaderboard_unavailable"
        await request({"request_id": "default-limit"}, missing)
        await request({"request_id": "rank%3B%25"}, missing)
        for limit in (1, 2, 10, 20):
            await request({"limit": limit, "request_id": f"valid-{limit}"}, missing, limit)
        await send_message(writer, "Heartbeat", {"request_id": "after-ranking"}, protocol=protocol)
        pong = await asyncio.wait_for(read_message(reader, protocol=protocol), 5)
        if pong["type"] != "Pong":
            raise AssertionError("leaderboard errors broke the existing connection")
        print("leaderboard endpoint passed", protocol, "mysql" if args.mysql else "unavailable")
    finally:
        writer.close()
        await writer.wait_closed()


async def run(args, process, names) -> None:
    await wait_for_server(args.port, process)
    for protocol in ("text_v1",) if args.text_only else ("text_v1", "proto_v1"):
        await probe(args, protocol, names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="")
    parser.add_argument("--existing-server", action="store_true")
    parser.add_argument("--port", type=int, default=19162)
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--mysql", action="store_true", help="read real SQL and add isolated temporary fixture players")
    parser.add_argument("--mysql-client", default=os.getenv("ARENA_MYSQL_CLIENT", "mysql"))
    parser.add_argument("--mysql-container", default=os.getenv("ARENA_MYSQL_CONTAINER", ""))
    args = parser.parse_args()
    if not args.existing_server and not args.server:
        parser.error("--server is required unless --existing-server is set")
    if args.mysql and any(not os.getenv(key) for key in ("ARENA_MYSQL_USER", "ARENA_MYSQL_DATABASE")):
        parser.error("real SQL mode requires ARENA_MYSQL_USER and ARENA_MYSQL_DATABASE")
    process = None
    names = []
    try:
        if args.mysql:
            suffix = uuid.uuid4().hex[:12]
            names = ["lb_" + suffix + "_a", "lb_" + suffix + "_b",
                     "\u699c" * 16 + "lb_" + suffix + "_", "lb_" + suffix + ";=%3B%25escaped"]
            assert len(names[2].encode("utf-8")) == 64
            tuples = ["(" + sql_name(name) + "," + sql_name(name) + "," +
                      str(2147483647 - index // 2) + ",2147483647,2)" for index, name in enumerate(names)]
            mysql_query(args.mysql_client,
                        "INSERT INTO players(player_id,nickname,rating,wins,losses) VALUES " + ",".join(tuples),
                        args.mysql_container)
        if not args.existing_server:
            environment = os.environ.copy()
            environment.update(ARENA_MYSQL_ENABLED="1" if args.mysql else "0",
                               ARENA_MYSQL_REQUIRED="1" if args.mysql else "0",
                               ARENA_REDIS_ENABLED="0", ARENA_REDIS_REQUIRED="0", ARENA_ROOM_RECOVERY="0")
            process = subprocess.Popen([os.path.abspath(args.server), str(args.port)], cwd=ROOT, env=environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        asyncio.run(run(args, process, names))
        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if names:
            mysql_query(args.mysql_client, "DELETE FROM players WHERE player_id IN (" +
                        ",".join(sql_name(name) for name in names) + ")", args.mysql_container)


if __name__ == "__main__":
    raise SystemExit(main())
