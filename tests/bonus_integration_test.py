"""Finite attack/heal bonuses through TextV1, ProtoV1, reconnect, and replay."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from status_effects_integration_test import MatchProbe, replay, send_message
from discard_integration_test import finish, check_private


def require_bonus(events: list[dict], kind: str, value: int) -> None:
    event = events[0]
    if event["type"] != kind or int(event.get("bonus", "-1")) != value:
        raise AssertionError(f"wrong applied bonus: {events}")


async def rejected(match: MatchProbe, card: int, expected: str) -> None:
    player = int(match.peers[0]["snapshot"]["turn"])
    peer = match.peers[player]
    counts = [len(peer["events"]) for peer in match.peers]
    snapshots = [dict(peer["snapshot"]) for peer in match.peers]
    await send_message(peer["writer"], "PlayCardReq", {"match_id": match.match_id,
        "turn_id": match.turn_id, "action_id": 99, "card": card}, protocol=peer["protocol"])
    error = await match.read_until(peer, "Error")
    if error.get("code") != expected:
        raise AssertionError(f"wrong rejected action: {error}")
    for peer in match.peers:
        await send_message(peer["writer"], "Heartbeat", "", protocol=peer["protocol"])
        await match.read_until(peer, "Pong")
    if counts != [len(peer["events"]) for peer in match.peers] or snapshots != [peer["snapshot"] for peer in match.peers]:
        raise AssertionError("rejected action consumed a bonus")


async def normal(server: str, port: int, directory: Path, protocols: tuple[str, str]) -> None:
    rows = "1,Focus,1,attack_boost,3,2\n2,Strike,2,damage,4,0\n3,Bless,1,heal_boost,2,2\n4,Mend,1,heal,4,0\n5,Heavy,4,damage,8,0\n"
    async with MatchProbe(server, port, directory, rows, protocols) as match:
        events = await match.action(1)
        if events[0].get("uses") != "2" or "duration" in events[0]:
            raise AssertionError("bonus application uses were not structured")
        await match.action(2)
        await match.action(3)
        await match.action()
        match.check(p0_hp=26, p0_attack_boost_uses=2, p0_heal_boost_uses=2)
        await rejected(match, 5, "card_not_in_hand")
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(0)
        require_bonus(await match.action(2), "damage", 3)
        await match.action()
        match.check(p1_hp=23, p0_attack_boost_uses=1, p0_heal_boost_uses=2)
        await match.action(1)
        await match.action()
        match.check(p0_attack_boost_value=3, p0_attack_boost_uses=2)
        require_bonus(await match.action(2), "damage", 3)
        await match.action()
        await rejected(match, 5, "not_enough_energy")
        require_bonus(await match.action(4), "heal", 2)
        await match.action()
        match.check(p0_hp=30, p0_heal_boost_uses=1, p0_attack_boost_uses=1)
        await match.action(3)
        await match.action()
        match.check(p0_heal_boost_uses=2, p0_attack_boost_uses=1)
        require_bonus(await match.action(2), "damage", 3)
        await match.action()
        match.check(p1_hp=9, p0_attack_boost_value=0, p0_attack_boost_uses=0)
        await match.action(3)
        await match.action()
        require_bonus(await match.action(4), "heal", 2)
        await match.action()
        match.check(p0_hp=30, p0_heal_boost_uses=1)
        await match.retry(0)
        await match.reconnect(0)
        await match.retry(0)
        match.check(p0_heal_boost_uses=1)
        await finish(match)
    print("bonus independence/replace/consume/cap/errors/ACK/reconnect/replay passed", protocols)


async def boundaries(server: str, port: int, directory: Path) -> None:
    directory.mkdir()
    rows = "1,Focus,1,attack_boost,3,1\n2,Tap,1,damage,0,0\n3,Barrier,1,shield,6,0\n"
    async with MatchProbe(server, port, directory / "shield", rows) as match:
        await match.action(1)
        await match.action(3)
        require_bonus(await match.action(2), "damage", 3)
        await match.action()
        match.check(p1_hp=30, p1_shield=3, p0_attack_boost_uses=0, p0_attack_boost_value=0)
        await match.action(3)
        await match.action()
        require_bonus(await match.action(2), "damage", 0)
        await match.action()
        match.check(p1_hp=30, p1_shield=3)
        await finish(match)
    rows = "1,Bless,1,heal_boost,10,1\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"
    async with MatchProbe(server, port + 1, directory / "full-health", rows,
                          ("proto_v1", "text_v1")) as match:
        await match.action(1)
        await match.action()
        require_bonus(await match.action(2), "heal", 10)
        match.check(p0_hp=30, p0_heal_boost_value=0, p0_heal_boost_uses=0)
        await match.retry(0)
        await match.action()
        await finish(match)
    rows = "1,Focus,1,attack_boost,3,2\n2,Venom,1,poison,2,2\n3,Ember,1,burn,2,2\n"
    async with MatchProbe(server, port + 2, directory / "statuses", rows) as match:
        await match.action(1)
        await match.action()
        await match.action(2)
        await match.action()
        await match.action(3)
        await match.action()
        match.check(p0_attack_boost_uses=2, p1_hp=24)
        rebuilt = await finish(match)
        if rebuilt["players"][0]["boosts"]["attack_boost"] != {"value": 3, "uses": 2}:
            raise AssertionError("periodic damage consumed attack bonus")
    rows = "1,Focus,1,attack_boost,3,1\n2,Strike,2,damage,27,0\n3,Ember,1,burn,30,2\n"
    async with MatchProbe(server, port + 3, directory / "lethal", rows,
                          ("proto_v1", "proto_v1")) as match:
        await match.action(1)
        await match.action(3)
        observed = await match.action(2, terminal=True)
        require_bonus(observed, "damage", 3)
        rebuilt, _ = await match.verify_replay()
        if len(observed) != 1 or rebuilt["winner"] != 0 or rebuilt["reason"] or rebuilt["players"][0]["hp"] != 30:
            raise AssertionError("enhanced direct lethal did not precede outgoing burn")
        if rebuilt["players"][0]["boosts"]["attack_boost"] != {"value": 0, "uses": 0}:
            raise AssertionError("lethal attack did not consume last bonus")
    print("shield/zero base/full-health/status isolation/lethal priority passed")


async def default_match(server: str, port: int, directory: Path, rows: str,
                        protocols: tuple[str, str], container: str = "") -> None:
    async with MatchProbe(server, port, directory, rows, protocols) as match:
        for card in (3, 2, 2, 3, 7, 8, 9, 10):
            await match.action(card)
            await match.action()
        await match.action(11)
        await match.action()
        await match.action(12)
        await match.action()
        match.check(p0_attack_boost_value=2, p0_attack_boost_uses=2, p0_heal_boost_value=2, p0_heal_boost_uses=2)
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(0)
        require_bonus(await match.action(1), "damage", 2)
        await match.action()
        require_bonus(await match.action(2), "heal", 2)
        await match.action()
        match.check(p0_attack_boost_uses=1, p0_heal_boost_uses=1)
        check_private(match)
        await match.retry(0)
        await finish(match, container)
    print("default Focus/Bless, public boosts/private cards, both codecs and replay passed", protocols)


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
            for card_id, kind in ((11, "attack_boost"), (12, "heal_boost")):
                if cards.get(card_id) != {"cost": 1, "effect": kind, "value": 2, "duration": 2}:
                    raise AssertionError("container catalog is not rebuilt with Focus/Bless")
            rows = config.read_text(encoding="utf-8").split("\n", 1)[1]
            for index, protocols in enumerate((("text_v1", "proto_v1"), ("proto_v1", "proto_v1"))):
                asyncio.run(default_match("", args.port or 9000, directory / str(index), rows,
                                          protocols, args.server_container))
        else:
            server = os.path.abspath(args.server)
            port = args.port or 19144
            for index, protocols in enumerate((("text_v1", "text_v1"), ("proto_v1", "proto_v1"),
                                                ("text_v1", "proto_v1"))):
                asyncio.run(normal(server, port + index, directory / str(index), protocols))
            asyncio.run(boundaries(server, port + 3, directory / "boundaries"))
            rows = (Path(__file__).resolve().parents[1] / "server/config/cards.csv").read_text(encoding="utf-8").split("\n", 1)[1]
            asyncio.run(default_match(server, port + 7, directory / "default", rows,
                                      ("proto_v1", "text_v1")))
    print("finite attack/heal bonus acceptance passed")


if __name__ == "__main__":
    main()
