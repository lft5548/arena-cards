"""Ensure the final accepted end-turn action appears in the terminal replay."""
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


async def login(name: str, port: int):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    login_message = await asyncio.wait_for(read_message(reader), 3)
    if login_message["type"] != "LoginResp":
        raise AssertionError(f"login failed: {login_message}")
    await send_message(writer, "MatchJoinReq", "")
    return reader, writer


async def play_end_turns(reader, writer, expected_match: str = "") -> dict:
    action_id = 0
    match_id = expected_match
    replay_events = []
    while True:
        message = await asyncio.wait_for(read_message(reader), 8)
        payload = message["payload"]
        if message["type"] == "MatchFound":
            match_id = payload["match_id"]
        elif message["type"] == "BattleSnapshot":
            match_id = payload["match_id"]
            if int(payload["turn"]) != int(payload["player_index"]):
                continue
            action_id += 1
            await send_message(writer, "EndTurnReq", {
                "match_id": match_id,
                "turn_id": int(payload["turn_id"]),
                "action_id": action_id,
            })
        elif message["type"] == "BattleEvent":
            if payload.get("type") != "end_turn":
                raise AssertionError(f"unexpected event in end-turn match: {payload}")
            replay_events.append((int(payload["player"]), int(payload["action_id"])))
        elif message["type"] == "Error":
            raise AssertionError(f"server rejected end-turn action: {payload}")
        elif message["type"] == "MatchResult":
            return {
                "match_id": match_id,
                "actions": action_id,
                "events": replay_events,
                "result": payload,
            }


async def run(server: str, port: int) -> None:
    process = subprocess.Popen([os.path.abspath(server), str(port)], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writers = []
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

        reader_a, writer_a = await login("max-turn-a", port)
        reader_b, writer_b = await login("max-turn-b", port)
        writers.extend((writer_a, writer_b))
        results = await asyncio.wait_for(asyncio.gather(
            play_end_turns(reader_a, writer_a),
            play_end_turns(reader_b, writer_b),
        ), 20)

        for result in results:
            payload = result["result"]
            if payload.get("reason") != "max_turns":
                raise AssertionError(f"match did not end at the turn limit: {payload}")
            if payload.get("winner") != "-1":
                raise AssertionError(f"all-end-turn match should be a draw: {payload}")
            if len(result["events"]) != 40 or result["actions"] != 20:
                raise AssertionError(f"terminal action missing from event stream: {result}")
            if int(payload["replay_revision"]) != 42:
                raise AssertionError(f"expected start + 40 actions + result: {payload}")
            if payload.get("replay_valid") != "1":
                raise AssertionError(f"server marked replay invalid: {payload}")

        if results[0]["match_id"] != results[1]["match_id"]:
            raise AssertionError(f"players received different matches: {results}")
        terminal_fields = ("replay_seed", "replay_revision", "replay_digest", "replay_valid")
        if tuple(results[0]["result"][key] for key in terminal_fields) != tuple(
                results[1]["result"][key] for key in terminal_fields):
            raise AssertionError("players received different replay summaries")
        events_a = results[0]["events"]
        events_b = results[1]["events"]
        if events_a != events_b:
            raise AssertionError("players observed different ordered action events")
        print("max-turn replay test passed", {
            "match_id": results[0]["match_id"],
            "actions": len(events_a),
            "replay_revision": results[0]["result"]["replay_revision"],
        })
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19111)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
