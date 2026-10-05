"""Verify finite discard through TextV1, or against an existing Docker server."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from status_effects_integration_test import MatchProbe, replay, send_message


def cards_in(snapshot: dict, field: str) -> list[int]:
    return [int(card) for card in snapshot.get(field, "").split(",") if card]


def check_private(match: MatchProbe) -> None:
    for viewer, peer in enumerate(match.peers):
        snapshot = peer["snapshot"]
        opponent = match.peers[1 - viewer]["snapshot"]
        if ("opponent_discard" in snapshot or "opponent_hand" in snapshot or
                int(snapshot["discard_count"]) != len(cards_in(snapshot, "discard")) or
                int(snapshot["opponent_discard_count"]) != len(cards_in(opponent, "discard")) or
                int(snapshot["opponent_hand_count"]) != len(cards_in(opponent, "hand"))):
            raise AssertionError("snapshot exposes opponent cards or inconsistent private counts")


async def discard_action(match: MatchProbe, card: int, count: int) -> int:
    actor = int(match.peers[0]["snapshot"]["turn"])
    target = 1 - actor
    before = dict(match.peers[target]["snapshot"])
    hand, history = cards_in(before, "hand"), cards_in(before, "discard")
    actor_history = cards_in(match.peers[actor]["snapshot"], "discard")
    actual = min(count, len(hand))
    events = await match.action(card)
    event = events[0]
    expected_keys = {"match_id", "turn_id", "player", "action_id", "card", "type", "value",
                     "target", "count"}
    if (set(event) != expected_keys or event["type"] != "discard" or int(event["card"]) != card or
            int(event["value"]) != count or int(event["target"]) != target or int(event["count"]) != actual):
        raise AssertionError(f"invalid public discard event: {event}")
    after = match.peers[target]["snapshot"]
    if (cards_in(after, "hand") != hand[actual:] or
            cards_in(after, "discard") != history + hand[:actual] or
            after["deck_count"] != before["deck_count"] or
            cards_in(match.peers[actor]["snapshot"], "discard") != actor_history):
        raise AssertionError("discard changed wrong cards, order, history, or deck")
    check_private(match)
    return actual


async def reject(match: MatchProbe, player: int, card: int, code: str) -> None:
    before = [dict(peer["snapshot"]) for peer in match.peers]
    counts = [len(peer["events"]) for peer in match.peers]
    await send_message(match.peers[player]["writer"], "PlayCardReq", {
        "match_id": match.match_id, "turn_id": match.turn_id, "action_id": 99, "card": card})
    response = await match.read_until(match.peers[player], "Error")
    if response.get("code") != code:
        raise AssertionError(f"expected {code}: {response}")
    for peer in match.peers:
        await send_message(peer["writer"], "Heartbeat", "")
        await match.read_until(peer, "Pong")
    if before != [peer["snapshot"] for peer in match.peers] or counts != [len(peer["events"]) for peer in match.peers]:
        raise AssertionError("rejected card changed authoritative state")


async def finish(match: MatchProbe, container: str = "") -> dict:
    while match.turn_id < 40:
        await match.action()
    await match.action(terminal=True)
    if container:
        destination = match.directory / "replays" / (match.match_id + ".replay")
        destination.parent.mkdir()
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            result = subprocess.run(["docker", "cp", container + ":/app/replays/" + destination.name,
                                     str(destination)], capture_output=True, text=True, check=False, timeout=10)
            if result.returncode == 0:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(result.stderr.strip() or "container replay was not persisted")
            await asyncio.sleep(0.1)
    state, events = await match.verify_replay()
    match.compare_final_snapshot(state)
    effects = {card["effect"] for card in replay.load_cards(match.config).values()}
    expected_rules = "bonus_v1" if effects & {"attack_boost", "heal_boost"} else "turn_end_v1" if "burn" in effects else "discard_v1"
    if state["rules"] != expected_rules or state["reason"] != "max_turns":
        raise AssertionError(f"wrong new replay rules or terminal state: {state}")
    print("discard replay verified", {"match_id": match.match_id, "revision": len(events),
                                      "discard": [player["discard"] for player in state["players"]]})
    return state


async def run_local(server: str, port: int, directory: Path) -> None:
    rows = "1,Disrupt,2,discard,2,0\n2,Cycle,1,draw,0,0\n3,Barrier,1,shield,0,0\n"
    async with MatchProbe(server, port, directory / "ordered", rows) as match:
        await reject(match, 1, 1, "not_your_turn")
        await reject(match, 0, 999, "unknown_card")
        if await discard_action(match, 1, 2) != 2:
            raise AssertionError("first discard did not remove two cards")
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(1)
        await match.retry(0)
        await match.action()
        if await discard_action(match, 1, 2) != 1:
            raise AssertionError("short hand did not report actual count")
        await match.action()
        await reject(match, 0, 1, "card_not_in_hand")
        await match.action(2)
        await match.action()
        await match.action(3)
        await match.action()
        if await discard_action(match, 1, 2) != 0:
            raise AssertionError("empty target did not produce zero count")
        await match.retry(0)
        await match.reconnect(1)
        state = await finish(match)
        if state["players"][1]["discard"] != [1, 2, 3] or state["players"][1]["hand"]:
            raise AssertionError("unexpected final ordered discard state")
    print("discard order/short/empty/privacy/idempotency/reconnect passed")

    rows = "1,Venom,1,poison,3,2\n2,Renew,1,regen,2,2\n3,Disrupt,2,discard,2,0\n"
    async with MatchProbe(server, port + 1, directory / "statuses", rows) as match:
        await match.action(1)
        await match.action(2)
        await discard_action(match, 3, 2)
        match.check(p1_hp=26, p1_poison_turns=0, p1_regen_turns=1)
        await match.action()
        await match.action()
        match.check(p1_hp=28, p1_regen_turns=0)
        state = await finish(match)
        if state["players"][1]["discard"] != [1, 3] or state["players"][1]["hand"] != [1]:
            raise AssertionError("discard did not preserve duplicate hand order")
    print("discard and poison/regen turn triggers passed")

    rows = "1,Disrupt,2,discard,3,0\n2,Cycle,1,draw,0,0\n3,Barrier,1,shield,0,0\n"
    async with MatchProbe(server, port + 2, directory / "maximum", rows) as match:
        await discard_action(match, 1, 3)
        await match.action()
        await discard_action(match, 1, 3)
        await finish(match)
    print("maximum discard bound passed")

    rows = "1,Disrupt,4,discard,1,0\n2,Cycle,1,draw,0,0\n3,Barrier,1,shield,0,0\n"
    async with MatchProbe(server, port + 3, directory / "energy", rows) as match:
        await reject(match, 0, 1, "not_enough_energy")
        await finish(match)
    print("unaffordable discard rejected without mutation")

    rows = (Path(__file__).resolve().parents[1] / "server" / "config" / "cards.csv").read_text(
        encoding="utf-8").split("\n", 1)[1]
    async with MatchProbe(server, port + 4, directory / "default", rows) as match:
        await run_default(match)
    print("default catalog and Docker client verification sequence passed locally")


async def run_default(match: MatchProbe, container: str = "") -> None:
    for peer in match.peers:
        await send_message(peer["writer"], "ProtocolHelloReq", "")
        capabilities = await match.read_until(peer, "ProtocolHelloResp")
        if (capabilities.get("text_v1") != "1" or capabilities.get("proto_v1") != "1" or
                capabilities.get("proto_v1_runtime") != os.getenv("ARENA_EXPECT_PROTOBUF", "1") or capabilities.get("selected") != "text_v1"):
            raise AssertionError(f"unexpected D4 protocol capabilities: {capabilities}")
    for card in (3, 2, 2, 3, 7, 8):
        await match.action(card)
        await match.action()
    if await discard_action(match, 9, 2) != 2:
        raise AssertionError("default discard card did not remove two cards")
    await match.retry(0)
    await match.retry(0, conflict=True)
    await match.reconnect(1)
    await match.retry(0)
    state = await finish(match, container)
    if state["players"][1]["discard"] != [1, 2]:
        raise AssertionError("default replay lost private discarded cards")


async def run_existing(port: int, container: str, directory: Path) -> None:
    config = directory / "container-cards.csv"
    subprocess.run(["docker", "cp", container + ":/app/config/cards.csv", str(config)], check=True, timeout=10)
    cards = replay.load_cards(config)
    expected = {1: "damage", 2: "heal", 3: "shield", 7: "poison", 8: "regen", 9: "discard", 10: "burn", 11: "attack_boost", 12: "heal_boost"}
    if (set(cards) != set(expected) or any(cards[card]["effect"] != effect for card, effect in expected.items()) or
            cards[9]["value"] != 2):
        raise AssertionError("container does not use the current default discard catalog; rebuild arena-server")
    rows = config.read_text(encoding="utf-8").split("\n", 1)[1]
    async with MatchProbe("", port, directory / "container", rows) as match:
        await run_default(match, container)
    print("Docker D4-foundation/discard/status/privacy/ACK/reconnect/persisted-replay test passed")


def main() -> int:
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
            asyncio.run(run_existing(args.port or 9000, args.server_container, directory))
        else:
            asyncio.run(run_local(os.path.abspath(args.server), args.port or 19116, directory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
