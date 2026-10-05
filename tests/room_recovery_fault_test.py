"""Isolated MySQL checkpoint write failure and owner fencing acceptance.

Uses a random schema/user in the existing MySQL container and a local Release
server. Existing Arena/Redis containers and named volumes remain running.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from room_recovery_integration_test import Server, create_match, read_checkpoint


def sql(args, query, database=""):
    command = ["docker", "exec", "-i", "-e", "MYSQL_PWD=" +
               os.getenv("ARENA_MYSQL_ROOT_PASSWORD", "root_dev_password"),
               args.mysql_container, "mysql", "--batch", "--skip-column-names", "-uroot"]
    if database:
        command.append(database)
    result = subprocess.run(command, input=query, capture_output=True, text=True, check=True, timeout=15)
    return result.stdout.strip()


async def run(args, user):
    server = Server(args)
    connections = []
    try:
        await server.start()
        pair = await create_match(args, ("fault_a", "fault_b"), connections)
        match_id = pair[0].snapshot["match_id"]
        before = await read_checkpoint(args, match_id)
        database = os.environ["ARENA_MYSQL_DATABASE"]
        await asyncio.to_thread(sql, args,
            f"REVOKE UPDATE ON `{database}`.room_checkpoints FROM '{user}'@'%'; FLUSH PRIVILEGES;")
        await pair[0].send("PlayCardReq", {"match_id": match_id, "turn_id": 1,
                            "action_id": 1, "card": 1})
        try:
            unexpected = await pair[0].receive(("ActionAck", "BattleEvent", "BattleSnapshot"), timeout=1.3)
        except asyncio.TimeoutError:
            pass
        else:
            raise AssertionError(f"checkpoint write failure leaked battle update or ACK: {unexpected}")
        if await read_checkpoint(args, match_id) != before:
            raise AssertionError("failed write changed durable checkpoint")
        await pair[1].send("EndTurnReq", {"match_id": match_id, "turn_id": 2, "action_id": 1})
        message = await pair[1].receive("Error")
        if message["payload"].get("code") != "recovery_pending":
            raise AssertionError("pending checkpoint did not freeze subsequent battle commands")
        await asyncio.to_thread(sql, args,
            f"GRANT UPDATE ON `{database}`.room_checkpoints TO '{user}'@'%'; FLUSH PRIVILEGES;")
        await pair[0].receive("ActionAck")
        await asyncio.gather(*(player.receive("BattleSnapshot") for player in pair))
        after = await read_checkpoint(args, match_id)
        if after["revision"] != 2 or after["turn_id"] != 2 or after["players"][0]["state"][3] != 1:
            raise AssertionError("checkpoint retry did not persist exactly one candidate action")
        print("room recovery write failure passed: no ACK/update before durability; later commands frozen; retry applied once")
    finally:
        for player in connections:
            await player.close()
        server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--owner-test", required=True)
    parser.add_argument("--mysql-container", default="arena-cards-mysql")
    parser.add_argument("--port", type=int, default=19174)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    args = parser.parse_args()
    args.mysql_client = "mysql"
    args.docker_server = ""
    suffix = uuid.uuid4().hex[:10]
    database, user, password = "recovery_fault_" + suffix, "rf_" + suffix, uuid.uuid4().hex
    old_environment = os.environ.copy()
    try:
        sql(args, f"CREATE DATABASE `{database}`; CREATE USER '{user}'@'%' IDENTIFIED BY '{password}';")
        sql(args, (ROOT / "deploy" / "schema.sql").read_text(encoding="utf-8"), database)
        sql(args, f"GRANT ALL ON `{database}`.* TO '{user}'@'%'; FLUSH PRIVILEGES;")
        os.environ.update(ARENA_MYSQL_HOST="127.0.0.1", ARENA_MYSQL_PORT="3307",
            ARENA_MYSQL_USER=user, ARENA_MYSQL_PASSWORD=password, ARENA_MYSQL_DATABASE=database,
            ARENA_MYSQL_LOCK_NAME="recovery_fault_" + suffix, ARENA_REDIS_ENABLED="0")
        subprocess.run([str(Path(args.owner_test).resolve())], env=os.environ.copy(), check=True, timeout=15)
        sql(args, "DELETE FROM room_checkpoints; DELETE FROM settlement_outbox; DELETE FROM match_results; "
            "DELETE FROM matches; DELETE FROM players;", database)
        # Table-specific grants allow revoking checkpoint UPDATE alone while
        # preserving normal match/settlement permissions.
        sql(args, f"REVOKE ALL ON `{database}`.* FROM '{user}'@'%'; "
            f"GRANT CREATE,ALTER,REFERENCES ON `{database}`.* TO '{user}'@'%'; " +
            " ".join(f"GRANT SELECT,INSERT,UPDATE,DELETE ON `{database}`.{table} TO '{user}'@'%';"
                     for table in ("players", "matches", "match_results", "settlement_outbox",
                                   "room_checkpoints", "room_recovery_tail")) +
            " FLUSH PRIVILEGES;")
        asyncio.run(run(args, user))
        return 0
    finally:
        os.environ.clear()
        os.environ.update(old_environment)
        sql(args, f"DROP DATABASE IF EXISTS `{database}`; DROP USER IF EXISTS '{user}'@'%'; FLUSH PRIVILEGES;")


if __name__ == "__main__":
    raise SystemExit(main())
