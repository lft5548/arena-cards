"""Verify a player can resume the same room within the reconnect window."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message


async def login_and_match(name: str, port: int):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    login = await asyncio.wait_for(read_message(reader), 2)
    token = login["payload"].get("token", "")
    if not token:
        raise AssertionError(f"missing token: {login}")
    await send_message(writer, "MatchJoinReq", "")
    return reader, writer, token


async def wait_for_server(process: subprocess.Popen, port: int) -> None:
    """Wait until this test's server is listening before opening game sessions."""
    deadline = asyncio.get_running_loop().time() + 3
    while asyncio.get_running_loop().time() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"server exited before listen: code={returncode}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            if process.poll() is not None:
                raise RuntimeError(f"server exited while becoming ready: code={process.returncode}")
            return
        except OSError:
            await asyncio.sleep(0.02)
    raise TimeoutError(f"server did not listen on port {port}")


async def wait_for(reader, message_type: str, timeout: float = 3):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await asyncio.wait_for(read_message(reader), max(0.1, deadline - asyncio.get_running_loop().time()))
        if message["type"] == message_type:
            return message
    raise AssertionError(f"did not receive {message_type}")


async def run(server: str, port: int):
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_for_server(process, port)
        reader_a, writer_a, token_a = await login_and_match("reconnect-a", port)
        reader_b, writer_b, _ = await login_and_match("reconnect-b", port)
        found_a = await wait_for(reader_a, "MatchFound")
        await wait_for(reader_b, "MatchFound")
        match_id = found_a["payload"]["match_id"]
        await wait_for(reader_a, "BattleSnapshot")
        writer_a.close()
        await writer_a.wait_closed()
        await asyncio.sleep(0.2)

        reader_resume, writer_resume = await asyncio.open_connection("127.0.0.1", port)
        await send_message(writer_resume, "ReconnectReq", token_a)
        response = None
        early_snapshot = None
        for _ in range(3):
            message = await asyncio.wait_for(read_message(reader_resume), 2)
            if message["type"] == "ReconnectResp":
                response = message
                break
            if message["type"] == "BattleSnapshot":
                early_snapshot = message
        if response is None:
            raise AssertionError("did not receive ReconnectResp")
        if response["payload"].get("ok") != "1" or response["payload"].get("match_id") != match_id:
            raise AssertionError(f"unexpected reconnect response: {response}")
        snapshot = early_snapshot or await wait_for(reader_resume, "BattleSnapshot")
        if snapshot["payload"].get("match_id") != match_id:
            raise AssertionError(f"snapshot resumed wrong match: {snapshot}")
        print("reconnect test passed", {"match_id": match_id})
        writer_resume.close()
        writer_b.close()
        await writer_resume.wait_closed()
        await writer_b.wait_closed()
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
    parser.add_argument("--port", type=int, default=19102)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
