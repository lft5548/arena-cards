"""Verify the unauthenticated admin room-count query over the game protocol."""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message

FAILURE_COUNTERS = (
    "settlement_outbox_load_failures", "settlement_outbox_mark_failures",
    "settlement_outbox_record_failures", "mysql_connection_attempts",
    "mysql_connection_successes", "mysql_connection_failures", "mysql_connection_losses",
    "redis_connection_failures", "redis_apply_failures",
)


async def query_payload(port: int) -> dict[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_message(writer, "AdminRoomsReq", {})
        message = await asyncio.wait_for(read_message(reader), 2)
        if message["type"] != "AdminRoomsResp":
            raise AssertionError(f"unexpected response: {message}")
        return message["payload"]
    finally:
        writer.close()
        await writer.wait_closed()


async def login_and_join(name: str, port: int):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    login = await asyncio.wait_for(read_message(reader), 2)
    if login["type"] != "LoginResp":
        raise AssertionError(f"login failed: {login}")
    await send_message(writer, "MatchJoinReq", "")
    return reader, writer


async def run(server: str, port: int) -> None:
    redis_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    redis_socket.bind(("127.0.0.1", 0))
    environment = os.environ.copy()
    environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0",
                       ARENA_REDIS_ENABLED="1", ARENA_REDIS_REQUIRED="0",
                       ARENA_REDIS_HOST="127.0.0.1", ARENA_REDIS_PORT=str(redis_socket.getsockname()[1]))
    process = subprocess.Popen([server, str(port)], cwd=ROOT, env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writers = []
    try:
        for attempt in range(100):
            try:
                initial = await query_payload(port)
                break
            except OSError:
                if process.poll() is not None or attempt == 99:
                    raise
                await asyncio.sleep(0.05)
        if int(initial.get("active_rooms", "-1")) != 0:
            raise AssertionError("expected no active rooms before matchmaking")
        for name in FAILURE_COUNTERS:
            expected = 1 if name == "redis_connection_failures" else 0
            if int(initial.get(name, "-1")) != expected:
                raise AssertionError(f"startup failure metric {name}: expected {expected}, got {initial}")
        _, writer_a = await login_and_join("admin-room-a", port)
        _, writer_b = await login_and_join("admin-room-b", port)
        writers.extend((writer_a, writer_b))
        await asyncio.sleep(0.1)
        metrics = await query_payload(port)
        if int(metrics.get("active_rooms", "-1")) != 1:
            raise AssertionError("expected one active room after matchmaking")
        for name in (
            "active_rooms", "active_sessions",
            "room_commands_enqueued", "room_commands_processed",
            "room_command_queue_high_watermark", "send_frames_enqueued",
            "send_frames_dropped", "send_queue_high_watermark_bytes",
            "settlements_started", "settlements_succeeded", "settlements_failed",
            "settlement_retries", "settlement_latency_ms_total", "settlement_latency_ms_max",
            "settlement_outbox_applied", "settlement_outbox_failures", "settlement_outbox_pending",
            "settlement_outbox_load_failures", "settlement_outbox_mark_failures",
            "settlement_outbox_record_failures", "mysql_connection_attempts",
            "mysql_connection_successes", "mysql_connection_failures", "mysql_connection_losses",
            "redis_connection_failures", "redis_apply_failures",
            "replays_saved", "replay_save_failures",
            "recovery_checkpoint_attempts", "recovery_checkpoint_successes", "recovery_checkpoint_failures",
            "recovery_checkpoint_bytes_total", "recovery_checkpoint_bytes_max",
            "recovery_checkpoint_serialize_us_total", "recovery_checkpoint_write_us_total",
            "recovery_checkpoint_write_us_max",
            "recovery_checkpoint_lock_wait_us_total", "recovery_checkpoint_connection_us_total",
            "recovery_checkpoint_sql_us_total", "recovery_checkpoint_commit_us_total",
            "recovery_checkpoint_queue_wait_us_total", "recovery_checkpoint_batches", "recovery_checkpoint_batch_items_max",
        ):
            if name not in metrics or not 0 <= int(metrics[name]) <= 2**64 - 1:
                raise AssertionError(f"missing or invalid metric {name}: {metrics}")
        if int(metrics["room_commands_processed"]) > int(metrics["room_commands_enqueued"]):
            raise AssertionError(f"processed commands exceed enqueued: {metrics}")
        print("admin rooms test passed")
    finally:
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        redis_socket.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19106)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
