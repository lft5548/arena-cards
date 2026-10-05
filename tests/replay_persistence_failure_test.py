"""Replay disk failures must not block or alter the authoritative result."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message


async def next_message(reader, wanted):
    while True:
        message = await asyncio.wait_for(read_message(reader), 5)
        if message["type"] == wanted:
            return message
        if message["type"] == "Error":
            raise AssertionError(f"unexpected game error: {message}")


async def player(name: str, port: int) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_message(writer, "LoginReq", name)
        await next_message(reader, "LoginResp")
        await send_message(writer, "MatchJoinReq", "")
        action_id = 0
        while True:
            message = await asyncio.wait_for(read_message(reader), 8)
            if message["type"] == "BattleSnapshot":
                payload = message["payload"]
                if int(payload["turn"]) == int(payload["player_index"]):
                    action_id += 1
                    await send_message(writer, "EndTurnReq", {
                        "match_id": payload["match_id"],
                        "turn_id": int(payload["turn_id"]),
                        "action_id": action_id,
                    })
            elif message["type"] == "MatchResult":
                return message["payload"]
            elif message["type"] == "Error":
                raise AssertionError(f"unexpected game error: {message}")
    finally:
        writer.close()
        await writer.wait_closed()


async def metrics(port: int) -> dict[str, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_message(writer, "AdminRoomsReq", {})
        response = await asyncio.wait_for(read_message(reader), 2)
        if response["type"] != "AdminRoomsResp":
            raise AssertionError(f"unexpected metrics response: {response}")
        return response["payload"]
    finally:
        writer.close()
        await writer.wait_closed()


async def run(server: str, port: int) -> None:
    with tempfile.TemporaryDirectory(prefix="arena-replay-failure-") as temp_dir:
        invalid_directory = Path(temp_dir) / "not-a-directory"
        invalid_directory.write_text("file blocks directory creation", encoding="utf-8")
        env = os.environ.copy()
        env["ARENA_REPLAY_DIR"] = str(invalid_directory)
        process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"server exited before listen: {process.returncode}")
                try:
                    _, probe = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.02)
                else:
                    probe.close()
                    await probe.wait_closed()
                    break
            else:
                raise TimeoutError(f"server did not listen on port {port}")

            results = await asyncio.wait_for(asyncio.gather(
                player("replay-fail-a", port), player("replay-fail-b", port)), 20)
            if results[0].get("winner") != "-1" or results[1].get("winner") != "-1":
                raise AssertionError(f"replay persistence failure changed match result: {results}")
            if results[0].get("match_id") != results[1].get("match_id"):
                raise AssertionError(f"players disagreed on match result: {results}")

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                values = await metrics(port)
                if int(values.get("replay_save_failures", "0")) >= 1:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"replay write failure was not counted: {values}")
            print("replay persistence failure test passed", {
                "match_id": results[0]["match_id"],
                "winner": results[0]["winner"],
                "replay_save_failures": values["replay_save_failures"],
            })
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19112)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
