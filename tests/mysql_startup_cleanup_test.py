"""Verify that a restart archives in-memory matches left in ``running`` state."""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def wait_for_port(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited before listening (exit={process.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("server did not start listening within 10 seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19107)
    parser.add_argument("--mysql-client", default=os.getenv("ARENA_MYSQL_CLIENT", "mysql"))
    parser.add_argument("--mysql-container", default=os.getenv("ARENA_MYSQL_CONTAINER", ""))
    args = parser.parse_args()
    required = ("ARENA_MYSQL_USER", "ARENA_MYSQL_DATABASE")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit("Set these variables for a dedicated test database: " + ", ".join(missing))

    suffix = uuid.uuid4().hex[:12]
    player_a, player_b = "cleanup_a_" + suffix, "cleanup_b_" + suffix
    match_id = "cleanup-" + suffix
    finished_a, finished_b = "finished_a_" + suffix, "finished_b_" + suffix
    finished_id = "finished-" + suffix
    escaped_match = match_id.replace("'", "''")
    escaped_a, escaped_b = player_a.replace("'", "''"), player_b.replace("'", "''")
    escaped_finished = finished_id.replace("'", "''")
    escaped_fa, escaped_fb = finished_a.replace("'", "''"), finished_b.replace("'", "''")
    process = None
    second_process = None
    try:
        mysql_query(args.mysql_client, "INSERT INTO players(player_id,nickname) VALUES ('" + escaped_a + "','" + escaped_a + "'),('" + escaped_b + "','" + escaped_b + "'),('" + escaped_fa + "','" + escaped_fa + "'),('" + escaped_fb + "','" + escaped_fb + "')", args.mysql_container)
        mysql_query(args.mysql_client, "INSERT INTO matches(match_id,player_a,player_b,status,started_at) VALUES ('" + escaped_match + "','" + escaped_a + "','" + escaped_b + "','running',NOW())", args.mysql_container)
        mysql_query(args.mysql_client, "INSERT INTO matches(match_id,player_a,player_b,status,result_reason,started_at,ended_at) VALUES ('" + escaped_finished + "','" + escaped_fa + "','" + escaped_fb + "','finished','normal',NOW(),NOW())", args.mysql_container)
        environment = os.environ.copy()
        environment["ARENA_MYSQL_ENABLED"] = "1"
        environment["ARENA_MYSQL_REQUIRED"] = "1"
        environment["ARENA_MYSQL_CLEANUP_RUNNING"] = "1"
        process = subprocess.Popen([os.path.abspath(args.server), str(args.port)], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for_port(args.port, process)
        second_process = subprocess.Popen([os.path.abspath(args.server), str(args.port + 1)], cwd=ROOT, env=environment,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while second_process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if second_process.poll() != 3:
            raise AssertionError(f"second instance should fail ownership lock, exit={second_process.poll()}")
        row = mysql_query(args.mysql_client, "SELECT status,result_reason,ended_at IS NOT NULL FROM matches WHERE match_id='" + escaped_match + "'", args.mysql_container)
        if row != "aborted\tserver_restart\t1":
            raise AssertionError(f"unexpected startup cleanup result: {row!r}")
        finished_row = mysql_query(args.mysql_client, "SELECT status,result_reason FROM matches WHERE match_id='" + escaped_finished + "'", args.mysql_container)
        if finished_row != "finished\tnormal":
            raise AssertionError(f"startup cleanup changed finished match: {finished_row!r}")
        players = mysql_query(args.mysql_client, "SELECT player_id,rating,wins,losses FROM players WHERE player_id IN ('" + escaped_a + "','" + escaped_b + "') ORDER BY player_id", args.mysql_container)
        if players != f"{player_a}\t1000\t0\t0\n{player_b}\t1000\t0\t0":
            raise AssertionError(f"startup cleanup changed player stats: {players!r}")
        # A second startup cleanup has no rows left to mutate.
        remaining_running = mysql_query(args.mysql_client, "SELECT COUNT(*) FROM matches WHERE match_id='" + escaped_match + "' AND status='running'", args.mysql_container)
        if remaining_running != "0":
            raise AssertionError(f"startup cleanup was not idempotent: {remaining_running!r}")
        result_count = mysql_query(args.mysql_client, "SELECT COUNT(*) FROM match_results WHERE match_id='" + escaped_match + "'", args.mysql_container)
        if result_count != "0":
            raise AssertionError(f"startup cleanup must not settle a match: {result_count!r}")
        print("mysql startup cleanup test passed", {"match_id": match_id})
        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if second_process is not None and second_process.poll() is None:
            second_process.terminate()
            try:
                second_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                second_process.kill()
                second_process.wait()
        try:
            mysql_query(args.mysql_client, "DELETE FROM match_results WHERE match_id='" + escaped_match + "'", args.mysql_container)
            mysql_query(args.mysql_client, "DELETE FROM matches WHERE match_id='" + escaped_match + "'", args.mysql_container)
            mysql_query(args.mysql_client, "DELETE FROM matches WHERE match_id='" + escaped_finished + "'", args.mysql_container)
            mysql_query(args.mysql_client, "DELETE FROM players WHERE player_id IN ('" + escaped_a + "','" + escaped_b + "','" + escaped_fa + "','" + escaped_fb + "')", args.mysql_container)
        except RuntimeError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
