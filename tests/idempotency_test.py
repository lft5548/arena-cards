"""Verify duplicate actions replay an acknowledgement without reapplying state."""
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


async def next_type(reader, expected, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        message = await asyncio.wait_for(read_message(reader), deadline - asyncio.get_running_loop().time())
        if message["type"] == expected:
            return message
    raise AssertionError(f"did not receive {expected}")


async def login(name, port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    await next_type(reader, "LoginResp")
    await send_message(writer, "MatchJoinReq", "")
    return reader, writer


async def run(server, port):
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writers = []
    try:
        await wait_for_server(process, port)
        reader_a, writer_a = await login("idem-a", port); writers.append(writer_a)
        reader_b, writer_b = await login("idem-b", port); writers.append(writer_b)
        match = (await next_type(reader_a, "MatchFound"))["payload"]["match_id"]
        await next_type(reader_b, "MatchFound")
        initial = await next_type(reader_a, "BattleSnapshot")
        await next_type(reader_b, "BattleSnapshot")
        first = initial["payload"]
        request = {"match_id": match, "turn_id": int(first["turn_id"]), "action_id": 1, "card": 1}
        await send_message(writer_a, "PlayCardReq", request)
        ack = await next_type(reader_a, "ActionAck")
        if ack["payload"].get("status") != "applied":
            raise AssertionError(f"action was not applied: {ack}")
        await next_type(reader_a, "BattleEvent")
        after = await next_type(reader_a, "BattleSnapshot")
        hp_after = int(after["payload"]["p1_hp"])
        if hp_after != int(first["p1_hp"]) - 8:
            raise AssertionError(f"unexpected first hit: before={first['p1_hp']} after={hp_after}")

        await send_message(writer_a, "PlayCardReq", request)
        replay = await next_type(reader_a, "ActionAck")
        if replay != ack:
            raise AssertionError(f"retry returned a different receipt: {replay} != {ack}")
        try:
            extra = await asyncio.wait_for(read_message(reader_a), 0.2)
        except asyncio.TimeoutError:
            extra = None
        if extra is not None:
            raise AssertionError(f"duplicate action changed the stream: {extra}")

        conflict = dict(request, card=2)
        await send_message(writer_a, "PlayCardReq", conflict)
        error = await next_type(reader_a, "Error")
        if error["payload"].get("code") != "action_id_conflict":
            raise AssertionError(f"expected action_id_conflict, got {error}")

        await next_type(reader_b, "BattleEvent")
        opponent_turn = await next_type(reader_b, "BattleSnapshot")
        end_request = {"match_id": match, "turn_id": int(opponent_turn["payload"]["turn_id"]), "action_id": 1}
        await send_message(writer_b, "EndTurnReq", end_request)
        end_ack = await next_type(reader_b, "ActionAck")
        await next_type(reader_b, "BattleEvent")
        await next_type(reader_b, "BattleSnapshot")
        await send_message(writer_b, "EndTurnReq", end_request)
        if await next_type(reader_b, "ActionAck") != end_ack:
            raise AssertionError("duplicate end-turn request returned a different acknowledgement")
        print("idempotency test passed", {"match_id": match, "hp_after": hp_after})
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


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--server", required=True); parser.add_argument("--port", type=int, default=19103)
    args = parser.parse_args(); asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
