"""Verify that concurrent same-turn commands are serialized by the room actor."""
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
from reconnect_test import wait_for_server


async def next_message(reader, types, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await asyncio.wait_for(read_message(reader), max(0.1, deadline - asyncio.get_running_loop().time()))
        if message["type"] in types:
            return message
    raise AssertionError(f"did not receive one of {types}")


async def login(name, port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    await next_message(reader, {"LoginResp"})
    await send_message(writer, "MatchJoinReq", "")
    return reader, writer


async def run(server, port):
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writers = []
    try:
        await wait_for_server(process, port)
        reader_a, writer_a = await login("concurrent-a", port)
        reader_b, writer_b = await login("concurrent-b", port)
        writers.extend((writer_a, writer_b))
        found = await next_message(reader_a, {"MatchFound"})
        await next_message(reader_b, {"MatchFound"})
        snapshot_a = await next_message(reader_a, {"BattleSnapshot"})
        snapshot_b = await next_message(reader_b, {"BattleSnapshot"})
        first = snapshot_a["payload"] if snapshot_a["payload"].get("turn") == snapshot_a["payload"].get("player_index") else snapshot_b["payload"]
        current_writer = writer_a if snapshot_a["payload"].get("player_index") == first.get("player_index") else writer_b
        other_writer = writer_b if current_writer is writer_a else writer_a
        request = {"match_id": found["payload"]["match_id"], "turn_id": int(first["turn_id"]), "action_id": 1, "card": 1}
        stale_request = dict(request, action_id=1)
        await asyncio.gather(
            send_message(current_writer, "PlayCardReq", request),
            send_message(other_writer, "PlayCardReq", stale_request),
        )
        responses = []
        for reader in (reader_a, reader_b):
            for _ in range(4):
                message = await next_message(reader, {"ActionAck", "Error", "BattleEvent", "BattleSnapshot"})
                if message["type"] in {"ActionAck", "Error"}:
                    responses.append(message)
                    break
        acks = [message for message in responses if message["type"] == "ActionAck" and message["payload"].get("status") == "applied"]
        errors = [message for message in responses if message["type"] == "Error"]
        if len(acks) != 1 or not errors:
            raise AssertionError(f"expected one applied command and one rejection: {responses}")
        print("concurrent action test passed", {"acks": len(acks), "errors": len(errors)})
    finally:
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--server", required=True); parser.add_argument("--port", type=int, default=19108)
    args = parser.parse_args(); asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
