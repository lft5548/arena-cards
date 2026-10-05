"""Verify finite turn-end burn through both protocols and persisted replay."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from status_effects_integration_test import MatchProbe, replay, send_message
from discard_integration_test import finish


def require_tick(events: list[dict], player: int, turn_id: int, hp: int, remaining: int) -> None:
    ticks = [event for event in events if event.get("status") == "burn"]
    if (len(ticks) != 1 or ticks[0].get("phase") != "end" or ticks[0]["action_id"] != "0" or
            int(ticks[0]["player"]) != player or int(ticks[0]["turn_id"]) != turn_id or
            int(ticks[0]["hp"]) != hp or int(ticks[0]["remaining"]) != remaining):
        raise AssertionError(f"invalid turn-end burn event: {events}")


async def normal(server: str, port: int, directory: Path, protocols: tuple[str, str]) -> None:
    rows = "1,Ember,1,burn,4,2\n2,Renew,1,regen,3,2\n3,Barrier,1,shield,6,0\n"
    async with MatchProbe(server, port, directory, rows, protocols) as match:
        await match.action(1)
        match.check(turn_id=2, p1_hp=30, p1_burn_turns=2)
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(1)
        match.check(p1_burn_turns=2)
        peer = match.peers[1]
        await send_message(peer["writer"], "PlayCardReq", {"match_id": match.match_id,
            "turn_id": match.turn_id - 1, "action_id": 99, "card": 3}, protocol=peer["protocol"])
        error = await match.read_until(peer, "Error")
        if error.get("code") != "stale_turn":
            raise AssertionError("invalid action triggered burn")
        events = await match.action(3)
        require_tick(events, 1, 2, 26, 1)
        match.check(p1_hp=26, p1_shield=6, p1_burn_turns=1)
        await match.action(2)
        events = await match.action(1)
        require_tick(events, 1, 4, 22, 0)
        if ([event["type"] for event in events] != ["burn", "status_tick", "status_tick"] or
                events[-1].get("status") != "regen" or events[-1].get("phase") is not None):
            raise AssertionError("end burn did not precede next-player start regen")
        match.check(turn_id=5, p1_burn_value=0, p1_burn_turns=0, p0_burn_turns=2)
        events = await match.action(2)
        require_tick(events, 0, 5, 26, 1)
        await match.action(1)
        match.check(p0_hp=29, p0_burn_turns=2)
        events = await match.action(3)
        require_tick(events, 0, 7, 25, 1)
        await match.retry(0)
        await match.reconnect(0)
        await match.retry(0)
        match.check(p0_burn_turns=1, p0_hp=25)
        await match.action()
        events = await match.action()
        require_tick(events, 0, 9, 24, 0)
        match.check(p0_burn_value=0, p0_burn_turns=0)
        await finish(match)
    print("turn-end replacement/expiry/shield/order/errors/ACK/reconnect/replay passed", protocols)


async def boundaries(server: str, port: int, directory: Path) -> None:
    directory.mkdir()
    rows = "1,Ember,1,burn,30,2\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"
    for index, card in enumerate((None, 3)):
        async with MatchProbe(server, port + index, directory / str(index), rows,
                              ("proto_v1", "text_v1")) as match:
            await match.action(1)
            events = await match.action(card, terminal=True)
            require_tick(events, 1, 2, 0, 1)
            rebuilt, _ = await match.verify_replay()
            if (rebuilt["reason"] != "burn" or rebuilt["winner"] != 0 or rebuilt["turn_id"] != 2 or
                    rebuilt["players"][1]["statuses"]["burn"] != {"value": 30, "turns": 1}):
                raise AssertionError("lethal end tick advanced the turn or lost status")
    rows = "1,Strike,2,damage,30,0\n2,Ember,1,burn,30,2\n3,Barrier,1,shield,6,0\n"
    async with MatchProbe(server, port + 2, directory / "direct", rows) as match:
        await match.action()
        await match.action(2)
        match.check(p0_burn_turns=2, p0_hp=30)
        events = await match.action(1, terminal=True)
        rebuilt, _ = await match.verify_replay()
        if len(events) != 1 or rebuilt["winner"] != 0 or rebuilt["reason"] or rebuilt["players"][0]["hp"] != 30:
            raise AssertionError("burn triggered after direct lethal damage")
    for index, value in enumerate((3, 30)):
        rows = f"1,Ember,1,burn,{value},2\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"
        async with MatchProbe(server, port + 3 + index, directory / f"final-{value}", rows) as match:
            while match.turn_id < 39:
                await match.action()
            await match.action(1)
            events = await match.action(terminal=True)
            require_tick(events, 1, 40, 30 - value, 1)
            rebuilt, _ = await match.verify_replay()
            if rebuilt["reason"] != ("burn" if value == 30 else "max_turns") or \
                    rebuilt["turn_id"] != (40 if value == 30 else 41):
                raise AssertionError("final legal turn-end trigger did not precede max-turn cutoff")
    print("lethal Play/EndTurn, direct-win priority, turn-40 end/max-turn boundaries passed")


async def default_match(server: str, port: int, directory: Path, rows: str,
                        protocols: tuple[str, str], container: str = "") -> None:
    async with MatchProbe(server, port, directory, rows, protocols) as match:
        for card in (3, 2, 2, 3, 7, 8, 9):
            await match.action(card)
            await match.action()
        before_hp = int(match.peers[0]["snapshot"]["p1_hp"])
        events = await match.action(10)
        if [event["type"] for event in events] != ["burn"]:
            raise AssertionError("new burn incorrectly triggered on start")
        match.check(p1_burn_value=3, p1_burn_turns=2)
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(1)
        events = await match.action()
        require_tick(events, 1, 16, before_hp - 3, 1)
        await match.action()
        events = await match.action()
        require_tick(events, 1, 18, before_hp - 6, 0)
        match.check(p1_burn_value=0, p1_burn_turns=0)
        await finish(match, container)
    print("default Ember/both protocol state/private cards/replay passed", protocols)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="")
    parser.add_argument("--existing-server", action="store_true")
    parser.add_argument("--server-container", default="arena-cards-server")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if not args.existing_server and not args.server:
        parser.error("--server is required unless --existing-server is set")
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        if args.existing_server:
            config = directory / "cards.csv"
            subprocess.run(["docker", "cp", args.server_container + ":/app/config/cards.csv", str(config)], check=True)
            cards = replay.load_cards(config)
            if cards.get(10) != {"cost": 1, "effect": "burn", "value": 3, "duration": 2}:
                raise AssertionError("container card catalog is not rebuilt with Ember")
            rows = config.read_text(encoding="utf-8").split("\n", 1)[1]
            for index, protocols in enumerate((("text_v1", "proto_v1"), ("proto_v1", "proto_v1"))):
                asyncio.run(default_match("", args.port or 9000, directory / f"docker-{index}", rows,
                                          protocols, args.server_container))
        else:
            server = os.path.abspath(args.server)
            port = args.port or 19134
            for index, protocols in enumerate((("text_v1", "text_v1"), ("proto_v1", "proto_v1"),
                                                ("text_v1", "proto_v1"))):
                asyncio.run(normal(server, port + index, directory / str(index), protocols))
            asyncio.run(boundaries(server, port + 3, directory / "boundaries"))
            rows = (Path(__file__).resolve().parents[1] / "server/config/cards.csv").read_text(encoding="utf-8").split("\n", 1)[1]
            asyncio.run(default_match(server, port + 8, directory / "default", rows,
                                      ("proto_v1", "text_v1")))
    print("turn-end burn full acceptance passed")


if __name__ == "__main__":
    main()
