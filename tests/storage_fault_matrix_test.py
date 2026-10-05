"""Run the opt-in same-version MySQL/Redis/Outbox fault matrix.

The test creates a temporary schema and user inside the existing MySQL container,
uses the current local Release server, and stops/starts only the Compose Arena and
Redis services. It never removes a named volume. This is intentionally separate
from the normal CTest suite because it needs Docker and database administrator
credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from mysql_settlement_test import ROOT, mysql_query, run_match
from protocol import read_message, send_message


MYSQL_CONTAINER = "arena-cards-mysql"
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.yml"
ROOT_PASSWORD = os.getenv("ARENA_MYSQL_ROOT_PASSWORD", "root_dev_password")


def command(args: list[str], *, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=ROOT, input=input_text, capture_output=True, text=True,
                            env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def compose(*args: str, fail_apply: int = 0, poll_seconds: int = 2) -> str:
    env = os.environ.copy()
    env.setdefault("ARENA_IMAGE_REGISTRY", "docker.m.daocloud.io/library")
    env.setdefault("ARENA_BASE_IMAGE", "docker.m.daocloud.io/library/ubuntu:24.04")
    env["ARENA_REDIS_FAIL_APPLY_COUNT"] = str(fail_apply)
    env["ARENA_OUTBOX_POLL_SECONDS"] = str(poll_seconds)
    return command(["docker", "compose", "-f", str(COMPOSE_FILE), *args], env=env)


def root_mysql(query: str, database: str = "") -> str:
    args = ["docker", "exec", "-i", MYSQL_CONTAINER, "mysql", "--batch", "--skip-column-names",
            "-uroot", "-p" + ROOT_PASSWORD]
    if database:
        args.append(database)
    args.extend(["-e", query])
    return command(args)


def load_schema(database: str) -> None:
    schema = (ROOT / "deploy" / "schema.sql").read_text(encoding="utf-8")
    args = ["docker", "exec", "-i", MYSQL_CONTAINER, "mysql", "-uroot", "-p" + ROOT_PASSWORD, database]
    command(args, input_text=schema)


def mysql_user_query(query: str) -> str:
    return mysql_query("mysql", query, MYSQL_CONTAINER)


def create_database(database: str, user: str, password: str) -> None:
    root_mysql(
        f"CREATE DATABASE `{database}`; DROP USER IF EXISTS '{user}'@'%'; "
        f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}';"
    )
    load_schema(database)
    root_mysql(
        f"GRANT CREATE,REFERENCES ON `{database}`.* TO '{user}'@'%'; "
        f"GRANT SELECT,INSERT,UPDATE ON `{database}`.players TO '{user}'@'%'; "
        f"GRANT SELECT,INSERT,UPDATE ON `{database}`.matches TO '{user}'@'%'; "
        f"GRANT SELECT,INSERT ON `{database}`.match_results TO '{user}'@'%'; "
        f"GRANT SELECT,INSERT,UPDATE ON `{database}`.settlement_outbox TO '{user}'@'%'; "
        "FLUSH PRIVILEGES;"
    )


def set_outbox_update(database: str, user: str, enabled: bool) -> None:
    if enabled:
        root_mysql(f"GRANT UPDATE ON `{database}`.settlement_outbox TO '{user}'@'%'; FLUSH PRIVILEGES;")
    else:
        root_mysql(f"REVOKE UPDATE ON `{database}`.settlement_outbox FROM '{user}'@'%'; FLUSH PRIVILEGES;")


def drop_database(database: str, user: str) -> None:
    root_mysql(f"DROP DATABASE IF EXISTS `{database}`; DROP USER IF EXISTS '{user}'@'%'; FLUSH PRIVILEGES;")


def start_local_server(binary: str, database: str, user: str, password: str, port: int,
                       *, fail_apply: int = 0, poll_seconds: int = 2) -> subprocess.Popen:
    environment = os.environ.copy()
    environment.update(
        ARENA_MYSQL_ENABLED="1", ARENA_MYSQL_REQUIRED="1", ARENA_MYSQL_HOST="127.0.0.1",
        ARENA_MYSQL_PORT="3307", ARENA_MYSQL_USER=user, ARENA_MYSQL_PASSWORD=password,
        ARENA_MYSQL_DATABASE=database, ARENA_MYSQL_CLEANUP_RUNNING="1",
        ARENA_MYSQL_LOCK_NAME="matrix_" + database, ARENA_REDIS_ENABLED="1",
        ARENA_REDIS_REQUIRED="0", ARENA_REDIS_HOST="127.0.0.1", ARENA_REDIS_PORT="6379",
        ARENA_REDIS_FAIL_APPLY_COUNT=str(fail_apply), ARENA_OUTBOX_POLL_SECONDS=str(poll_seconds),
    )
    log_path = Path(tempfile.gettempdir()) / f"arena-storage-matrix-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen([os.path.abspath(binary), str(port)], cwd=ROOT, env=environment,
                               stdout=log, stderr=subprocess.STDOUT)
    process._arena_matrix_log = log  # type: ignore[attr-defined]
    process._arena_matrix_log_path = log_path  # type: ignore[attr-defined]
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"server exited before listening (exit={process.returncode})")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise TimeoutError(f"server did not start listening on {port}")
    except BaseException:
        process.terminate()
        process.wait(timeout=3)
        log.close()
        details = log_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(f"local Arena failed to start on {port}: {details.strip()}")
    return process


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    log = getattr(process, "_arena_matrix_log", None)
    if log is not None:
        log.close()


def stop_compose_services() -> None:
    compose("stop", "arena-server", "redis")


def start_redis() -> None:
    compose("up", "-d", "--no-deps", "--wait", "redis")


def stop_redis() -> None:
    compose("stop", "redis")


def outbox_row(match_id: str) -> tuple[str, int]:
    row = mysql_user_query(
        "SELECT status,attempts FROM settlement_outbox WHERE match_id='" + match_id.replace("'", "''") + "'"
    )
    if not row:
        raise AssertionError(f"missing outbox row for {match_id}")
    status, attempts = row.split("\t")
    return status, int(attempts)


def wait_outbox(match_ids: list[str], status: str, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(outbox_row(match_id)[0] == status for match_id in match_ids):
            return
        time.sleep(0.25)
    raise AssertionError(f"outbox rows did not reach {status}: {[outbox_row(i) for i in match_ids]}")


async def admin_metrics(port: int) -> dict[str, int]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_message(writer, "AdminRoomsReq", {"request_id": "matrix-metrics"})
        while True:
            message = await asyncio.wait_for(read_message(reader), 10)
            if message["type"] == "AdminRoomsResp":
                return {key: int(value) for key, value in message["payload"].items()
                        if key != "request_id"}
            if message["type"] == "Error":
                raise RuntimeError(str(message["payload"]))
    finally:
        writer.close()
        await writer.wait_closed()


def metrics(port: int) -> dict[str, int]:
    return asyncio.run(admin_metrics(port))


async def wait_metrics_async(port: int, predicate, timeout: float = 20) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    latest: dict[str, int] = {}
    while time.monotonic() < deadline:
        latest = await admin_metrics(port)
        if predicate(latest):
            return latest
        await asyncio.sleep(0.25)
    raise AssertionError(f"metrics predicate timed out: {latest}")


def wait_metrics(port: int, predicate, timeout: float = 20) -> dict[str, int]:
    return asyncio.run(wait_metrics_async(port, predicate, timeout))


def processlist(database: str, user: str) -> list[tuple[int, str, str]]:
    raw = root_mysql(
        "SELECT ID,COALESCE(STATE,''),COALESCE(INFO,'') FROM information_schema.PROCESSLIST "
        f"WHERE USER='{user}' AND DB='{database}' AND COMMAND='Query'"
    )
    rows: list[tuple[int, str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def start_row_lock(database: str, player_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["docker", "exec", MYSQL_CONTAINER, "mysql", "-uroot", "-p" + ROOT_PASSWORD, database,
         "-e", f"START TRANSACTION; SELECT player_id FROM players WHERE player_id='{player_id}' FOR UPDATE; SELECT SLEEP(60);"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def interrupted_match(binary: str, database: str, user: str, password: str, port: int,
                            names: tuple[str, str]) -> tuple[str, dict[str, int]]:
    process = start_local_server(binary, database, user, password, port)
    players: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    lock_process: subprocess.Popen | None = None
    lock_connection_id: int | None = None
    try:
        async def receive_kind(reader: asyncio.StreamReader, wanted: str) -> dict[str, str]:
            while True:
                message = await asyncio.wait_for(read_message(reader), 15)
                if message["type"] == wanted:
                    return message["payload"]
                if message["type"] == "Error":
                    raise RuntimeError(str(message["payload"]))

        for name in names:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            players.append((reader, writer))
            await send_message(writer, "LoginReq", name)
            login = await receive_kind(reader, "LoginResp")
            if login.get("ok") != "1":
                raise RuntimeError(f"matrix login failed: {login}")
            await send_message(writer, "MatchJoinReq", "")
        initial = await asyncio.gather(*(receive_kind(reader, "BattleSnapshot") for reader, _ in players))
        # Both snapshots prove matchmaking and begin_match have completed. Lock
        # the loser row only after that point, so settlement is the interrupted
        # operation rather than room creation.
        lock_process = start_row_lock(database, names[1])
        lock_deadline = time.monotonic() + 10
        while time.monotonic() < lock_deadline:
            raw_lock = root_mysql(
                "SELECT ID FROM information_schema.PROCESSLIST "
                f"WHERE USER='root' AND DB='{database}' AND INFO LIKE '%SLEEP(60)%'"
            )
            if raw_lock.strip().isdigit():
                lock_connection_id = int(raw_lock.strip())
                break
            await asyncio.sleep(0.1)
        if lock_connection_id is None:
            raise AssertionError("row lock session did not start")

        async def play(index: int) -> str:
            reader, writer = players[index]
            pending = initial[index]
            action_id = 0
            match_id = pending.get("match_id", "")
            while True:
                payload = pending
                pending = None
                if payload is None:
                    message = await asyncio.wait_for(read_message(reader), 15)
                    kind, payload = message["type"], message["payload"]
                    if kind == "ActionAck":
                        continue
                    if kind == "MatchResult":
                        return payload.get("match_id", match_id)
                    if kind == "Error":
                        if payload.get("code") == "settlement_pending":
                            continue
                        raise RuntimeError(str(payload))
                    if kind != "BattleSnapshot":
                        continue
                match_id = payload.get("match_id", match_id)
                if int(payload.get("turn", -1)) != index:
                    continue
                action_id += 1
                request = {"match_id": match_id, "turn_id": int(payload["turn_id"]), "action_id": action_id}
                hand = [int(card) for card in payload.get("hand", "").split(",") if card]
                if index == 0 and 1 in hand and int(payload["p0_energy"]) >= 2:
                    request["card"] = 1
                    await send_message(writer, "PlayCardReq", request)
                else:
                    await send_message(writer, "EndTurnReq", request)

        task = asyncio.ensure_future(asyncio.gather(play(0), play(1)))
        killed = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not task.done():
            for connection_id, state, info in processlist(database, user):
                if "players" in info.lower() and ("insert" in info.lower() or "lock" in state.lower()):
                    root_mysql(f"KILL {connection_id}")
                    killed = True
                    break
            if killed:
                break
            await asyncio.sleep(0.1)
        if not killed:
            task.cancel()
            raise AssertionError(f"could not find blocked settlement connection: {processlist(database, user)}")
        root_mysql(f"KILL {lock_connection_id}")
        lock_process.terminate()
        lock_process.wait(timeout=3)
        match_results = await asyncio.wait_for(task, 35)
        match_id = match_results[0]
        return match_id, await wait_metrics_async(port, lambda values: values.get("mysql_connection_losses", 0) >= 1)
    finally:
        if lock_process is not None and lock_process.poll() is None:
            if lock_connection_id is not None:
                try:
                    root_mysql(f"KILL {lock_connection_id}")
                except RuntimeError:
                    pass
            lock_process.terminate()
            lock_process.wait(timeout=3)
        for _, writer in players:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        stop_process(process)


def cleanup_matches(match_ids: list[str], players: list[str]) -> None:
    for match_id in match_ids:
        safe = match_id.replace("'", "''")
        mysql_user_query(f"DELETE FROM settlement_outbox WHERE match_id='{safe}'")
        mysql_user_query(f"DELETE FROM match_results WHERE match_id='{safe}'")
        mysql_user_query(f"DELETE FROM matches WHERE match_id='{safe}'")
    for player in players:
        mysql_user_query("DELETE FROM players WHERE player_id='" + player.replace("'", "''") + "'")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=str(ROOT / "build-mysql-redis-release" / "arena_server.exe"))
    parser.add_argument("--port", type=int, default=19170)
    parser.add_argument("--mysql-container", default=MYSQL_CONTAINER)
    args = parser.parse_args()
    if args.mysql_container != MYSQL_CONTAINER:
        raise SystemExit("the matrix currently expects the Compose MySQL container named arena-cards-mysql")
    if not Path(args.server).exists():
        raise SystemExit(f"server executable does not exist: {args.server}")

    suffix = uuid.uuid4().hex[:12]
    database = "arena_matrix_" + suffix
    user = "matrix_" + suffix
    password = "matrix_password_" + suffix
    match_ids: list[str] = []
    players: list[str] = []
    process: subprocess.Popen | None = None
    try:
        os.environ.update(ARENA_MYSQL_USER=user, ARENA_MYSQL_PASSWORD=password, ARENA_MYSQL_DATABASE=database)
        stop_compose_services()
        create_database(database, user, password)
        # Redis outage: MySQL commits while the outbox remains pending, then the
        # same local binary reconnects and drains it after Redis returns.
        process = start_local_server(args.server, database, user, password, args.port)
        stop_redis()
        outage_names = ("matrix_outage_a_" + suffix, "matrix_outage_b_" + suffix)
        players.extend(outage_names)
        outage_id = asyncio.run(run_match(args.port, outage_names, process))
        match_ids.append(outage_id)
        require(outbox_row(outage_id)[0] == "pending", "Redis outage must leave a pending outbox row")
        start_redis()
        wait_outbox([outage_id], "applied")
        stop_process(process)
        process = None

        # Kill the blocked MySQL connection during an in-flight transaction.
        stop_redis()
        transaction_names = ("matrix_transaction_a_" + suffix, "matrix_transaction_b_" + suffix)
        players.extend(transaction_names)
        transaction_id, transaction_metrics = asyncio.run(
            interrupted_match(args.server, database, user, password, args.port + 1, transaction_names)
        )
        match_ids.append(transaction_id)
        require(outbox_row(transaction_id)[0] == "pending", "interrupted transaction must retry to an outbox row")
        require(transaction_metrics.get("mysql_connection_attempts", 0) >= 2,
                "interrupted transaction must reconnect")
        start_redis()
        process = start_local_server(args.server, database, user, password, args.port + 1, poll_seconds=1)
        wait_outbox([transaction_id], "applied")
        stop_process(process)
        process = None

        # Revoke only Outbox UPDATE. A pending row then exercises mark and
        # record failure paths; restoring the grant must allow normal recovery.
        stop_redis()
        process = start_local_server(args.server, database, user, password, args.port + 2, poll_seconds=2)
        mark_names = ("matrix_mark_a_" + suffix, "matrix_mark_b_" + suffix)
        players.extend(mark_names)
        mark_id = asyncio.run(run_match(args.port + 2, mark_names, process))
        match_ids.append(mark_id)
        require(outbox_row(mark_id)[0] == "pending", "mark scenario must start pending")
        stop_process(process)
        process = None
        set_outbox_update(database, user, False)
        start_redis()
        process = start_local_server(args.server, database, user, password, args.port + 2, poll_seconds=60)
        mark_metrics = wait_metrics(args.port + 2, lambda values: values.get("settlement_outbox_mark_failures", 0) >= 1)
        require(mark_metrics.get("settlement_outbox_record_failures", 0) >= 1,
                "mark failure must attempt to record its error")
        require(outbox_row(mark_id)[0] == "pending", "mark failure must preserve pending outbox")
        stop_process(process)
        process = None
        set_outbox_update(database, user, True)
        process = start_local_server(args.server, database, user, password, args.port + 2, poll_seconds=1)
        wait_outbox([mark_id], "applied")
        stop_process(process)
        process = None

        # Inject one Redis apply failure while Outbox UPDATE is revoked. This
        # reaches record_outbox_failure without first reaching mark_outbox_applied.
        stop_redis()
        process = start_local_server(args.server, database, user, password, args.port + 3, poll_seconds=2)
        record_names = ("matrix_record_a_" + suffix, "matrix_record_b_" + suffix)
        players.extend(record_names)
        record_id = asyncio.run(run_match(args.port + 3, record_names, process))
        match_ids.append(record_id)
        require(outbox_row(record_id)[0] == "pending", "record scenario must start pending")
        stop_process(process)
        process = None
        start_redis()
        set_outbox_update(database, user, False)
        process = start_local_server(args.server, database, user, password, args.port + 3,
                                     fail_apply=1, poll_seconds=60)
        record_metrics = wait_metrics(args.port + 3, lambda values: values.get("settlement_outbox_record_failures", 0) >= 1)
        require(record_metrics.get("redis_apply_failures", 0) >= 1,
                "record scenario must observe the injected Redis failure")
        require(outbox_row(record_id)[0] == "pending", "record failure must preserve pending outbox")
        stop_process(process)
        process = None
        set_outbox_update(database, user, True)
        process = start_local_server(args.server, database, user, password, args.port + 3, poll_seconds=1)
        wait_outbox([record_id], "applied")

        print("storage fault matrix passed", {
            "redis_outage_recovery": outage_id,
            "transaction_connection_kill": transaction_id,
            "outbox_mark_failure": mark_id,
            "outbox_record_failure": record_id,
            "transaction_metrics": transaction_metrics,
            "mark_metrics": mark_metrics,
            "record_metrics": record_metrics,
        })
        return 0
    finally:
        stop_process(process)
        try:
            set_outbox_update(database, user, True)
            cleanup_matches(match_ids, players)
        except Exception:
            pass
        try:
            drop_database(database, user)
        except Exception:
            pass
        try:
            start_redis()
            compose("up", "-d", "--no-deps", "--wait", "arena-server")
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
