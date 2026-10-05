"""End-to-end two-player test for the C++17 server."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message


async def play_client(name: str, port: int) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await send_message(writer, "LoginReq", name)
    login = await asyncio.wait_for(read_message(reader), 2)
    if login["type"] != "LoginResp":
        raise AssertionError(f"{name}: login failed: {login}")
    await send_message(writer, "MatchJoinReq", "")
    match_id = ""
    player_index = -1
    action_id = 0
    result = None
    replay_seed = None
    replay_revision = 0
    replay_digest_by_revision = {}
    battle_event_count = 0
    replay_action_ids = set()
    deadline = time.monotonic() + 8
    try:
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(read_message(reader), 2)
            payload = msg["payload"]
            if msg["type"] == "MatchFound":
                match_id = payload.get("match_id", match_id)
            elif msg["type"] == "BattleSnapshot":
                match_id = payload.get("match_id", match_id)
                for replay_field in ("replay_seed", "replay_revision", "replay_digest", "replay_valid"):
                    if replay_field not in payload:
                        raise AssertionError(f"{name}: snapshot missing {replay_field}: {payload}")
                if payload["replay_valid"] != "1":
                    raise AssertionError(f"{name}: server marked replay invalid: {payload}")
                snapshot_seed = int(payload["replay_seed"])
                snapshot_revision = int(payload["replay_revision"])
                snapshot_digest = payload["replay_digest"]
                if replay_seed is not None and replay_seed != snapshot_seed:
                    raise AssertionError(f"{name}: replay seed changed during match")
                if snapshot_revision < replay_revision:
                    raise AssertionError(f"{name}: replay revision moved backwards")
                previous_digest = replay_digest_by_revision.get(snapshot_revision)
                if previous_digest is not None and previous_digest != snapshot_digest:
                    raise AssertionError(f"{name}: digest changed without a replay revision")
                replay_seed = snapshot_seed
                replay_revision = snapshot_revision
                replay_digest_by_revision[snapshot_revision] = snapshot_digest
                player_index = int(payload.get("player_index", player_index))
                if int(payload.get("turn", -1)) != player_index:
                    continue
                action_id += 1
                hand = [int(x) for x in payload.get("hand", "").split(",") if x]
                card = next((x for x in hand if x in (1, 2, 3)), None)
                request = {"match_id": match_id, "turn_id": int(payload["turn_id"]), "action_id": action_id}
                if card is None:
                    await send_message(writer, "EndTurnReq", request)
                else:
                    request["card"] = card
                    await send_message(writer, "PlayCardReq", request)
            elif msg["type"] == "BattleEvent":
                event = payload
                event_action_id = int(event.get("action_id", "0"))
                player = int(event.get("player", "-1"))
                if event_action_id <= 0 or player not in (0, 1):
                    raise AssertionError(f"{name}: battle event missing replay action identity: {event}")
                key = (player, event_action_id)
                if key in replay_action_ids:
                    raise AssertionError(f"{name}: duplicate battle event in stream: {event}")
                replay_action_ids.add(key)
                battle_event_count += 1
            elif msg["type"] == "Error":
                raise AssertionError(f"{name}: server rejected action: {payload}")
            elif msg["type"] == "MatchResult":
                result = payload
                break
        if result is None:
            raise AssertionError(f"{name}: match did not finish")
        for replay_field in ("replay_seed", "replay_revision", "replay_digest", "replay_valid"):
            if replay_field not in result:
                raise AssertionError(f"{name}: MatchResult missing {replay_field}: {result}")
        if result["replay_valid"] != "1":
            raise AssertionError(f"{name}: terminal replay was marked invalid: {result}")
        result_seed = int(result["replay_seed"])
        result_revision = int(result["replay_revision"])
        result_digest = result["replay_digest"]
        if replay_seed is not None and result_seed != replay_seed:
            raise AssertionError(f"{name}: terminal replay seed differs from snapshot")
        if result_revision <= replay_revision:
            raise AssertionError(f"{name}: terminal result was not included in replay")
        if result_revision != battle_event_count + 2:
            raise AssertionError(f"{name}: replay revision does not match start/events/result: {result}")
        if not replay_action_ids:
            raise AssertionError(f"{name}: match produced no replayable actions")
        return {"name": name, "match_id": match_id, "result": result, "actions": action_id,
                "replay": (result_seed, result_revision, result_digest, result["replay_valid"])}
    finally:
        writer.close()
        await writer.wait_closed()


async def run(server: str, port: int) -> None:
    replay_directory = tempfile.TemporaryDirectory(prefix="arena-replays-")
    server_env = os.environ.copy()
    server_env["ARENA_REPLAY_DIR"] = replay_directory.name
    process = subprocess.Popen([server, str(port)], cwd=ROOT, env=server_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(0.25)
        results = await asyncio.gather(play_client("integration-a", port), play_client("integration-b", port))
        assert results[0]["match_id"] == results[1]["match_id"] != ""
        assert results[0]["result"]["match_id"] == results[0]["match_id"]
        assert results[1]["result"]["match_id"] == results[1]["match_id"]
        assert all(item["actions"] > 0 for item in results)
        assert results[0]["replay"] == results[1]["replay"], f"players disagree on terminal replay: {results}"
        replay_path = Path(replay_directory.name) / (results[0]["match_id"] + ".replay")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not replay_path.exists():
            await asyncio.sleep(0.02)
        if not replay_path.exists():
            raise AssertionError(f"server did not persist replay: {replay_path}")
        command = [sys.executable, str(ROOT / "tools" / "replay" / "replay_battle.py"), str(replay_path)]
        replayed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                  timeout=5, check=False)
        if replayed.returncode != 0:
            raise AssertionError(f"offline replay tool failed: {replayed.stderr}")
        reconstructed = json.loads(replayed.stdout)
        if reconstructed.get("verified") is not True or reconstructed.get("match_id") != results[0]["match_id"]:
            raise AssertionError(f"offline replay returned inconsistent match: {reconstructed}")
        terminal = results[0]["result"]
        if str(reconstructed["digest"]) != terminal["replay_digest"]:
            raise AssertionError(f"offline replay digest differs from protocol result: {reconstructed}")
        if reconstructed["winner"] != int(terminal["winner"]):
            raise AssertionError(f"offline replay winner differs from protocol result: {reconstructed}")
        print("integration replay test passed", {"match_id": results[0]["match_id"],
                                                 "revision": reconstructed["revision"],
                                                 "winner": reconstructed["winner"]})
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        replay_directory.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19100)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
