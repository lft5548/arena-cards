"""Run the original Bot entry point in all modes and rebuild every replay."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from status_effects_integration_test import ROOT, replay, wait_for_server


async def run(server: str, port: int) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        environment = os.environ.copy()
        environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0",
                           ARENA_REDIS_ENABLED="0", ARENA_REPLAY_DIR=temporary)
        process = subprocess.Popen([server, str(port)], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await wait_for_server(process, port)
            cards = replay.load_cards(ROOT / "server/config/cards.csv")
            verified = set()
            for mode in ("text_v1", "proto_v1", "mixed"):
                command = [sys.executable,
                           str(ROOT / "tools/bot/bot.py"), "--port", str(port), "--count", "4",
                           "--rounds", "3", "--timeout", "20"]
                if mode != "text_v1":
                    command += ["--protocol", mode]
                result = await asyncio.to_thread(subprocess.run, command, cwd=ROOT, capture_output=True,
                                                 text=True, check=True, timeout=45)
                report = json.loads(result.stdout)
                if (not report["ok"] or sum(bot["completed_rounds"] for bot in report["results"]) != 12 or
                        sum(bot["errors"] for bot in report["results"]) != 0):
                    raise AssertionError(report)
                if mode == "mixed" and report["protocol_counts"] != {"text_v1": 2, "proto_v1": 2}:
                    raise AssertionError("mixed Bot did not exercise both codecs")
                deadline = asyncio.get_running_loop().time() + 3
                current = set(Path(temporary).glob("*.replay")) - verified
                while len(current) < 6 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.02)
                    current = set(Path(temporary).glob("*.replay")) - verified
                if len(current) != 6:
                    raise AssertionError(f"expected 6 {mode} matches, got {len(current)}")
                counters = {"discard": 0, "status_tick": 0, "burn_ticks": 0,
                            "attack_boost": 0, "heal_boost": 0, "boosted_actions": 0}
                for path in current:
                    match_id, _, _, events = replay.load_replay(path)
                    rebuilt = replay.reconstruct(events, cards, match_id)
                    if rebuilt["rules"] != "bonus_v1":
                        raise AssertionError("Bot replay used wrong rules")
                    for event in events:
                        if event.kind == "battle_event":
                            fields = replay.parse_fields(event.payload)
                            kind = fields["type"]
                            if kind in counters:
                                counters[kind] += 1
                            if kind == "status_tick" and fields.get("status") == "burn":
                                counters["burn_ticks"] += 1
                            if int(fields.get("bonus", "0")) > 0:
                                counters["boosted_actions"] += 1
                if not all(counters.values()):
                    raise AssertionError("Bot did not exercise new battle states")
                verified.update(current)
                print("Bot regression passed", {"mode": mode, "player_rounds": 12,
                                                 "replays": len(current), **counters})
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19128)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
