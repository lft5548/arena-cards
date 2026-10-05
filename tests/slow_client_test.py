"""Verify that an over-capacity outbound frame closes a slow session."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import send_message


async def run(server: str, port: int) -> None:
    env = os.environ.copy()
    env["ARENA_SEND_QUEUE_MAX_BYTES"] = "1"
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writer = None
    try:
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server exited before listening: {process.returncode}")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            raise AssertionError("server did not listen")

        await send_message(writer, "LoginReq", "slow-client")
        data = await asyncio.wait_for(reader.read(1), 2)
        if data != b"":
            raise AssertionError("slow session unexpectedly received data under a one-byte queue cap")
        print("slow client backpressure test passed")
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19109)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
