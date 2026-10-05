"""Verify TextV1 capability discovery and ProtoV1 ID isolation."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import make_proto_message, read_message, send_message


async def run(server: str, port: int) -> None:
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT,
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

        await send_message(writer, "ProtocolHelloReq", "")
        response = await asyncio.wait_for(read_message(reader), 2)
        if response["type"] != "ProtocolHelloResp":
            raise AssertionError(f"unexpected capability response: {response}")
        fields = response["payload"]
        if fields.get("text_v1") != "1" or fields.get("proto_v1") != "1":
            raise AssertionError(f"missing protocol capabilities: {fields}")
        if fields.get("proto_v1_runtime") != os.getenv("ARENA_EXPECT_PROTOBUF", "1") or fields.get("selected") != "text_v1":
            raise AssertionError(f"unexpected default negotiation: {fields}")

        # The helper must never reuse TextV1 IDs for binary payloads.
        binary_frame = make_proto_message("LoginReq", b"\x08\x03")
        writer.write(binary_frame)
        await writer.drain()
        await asyncio.sleep(0.05)
        print("protocol hello test passed")
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
    parser.add_argument("--port", type=int, default=19110)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
