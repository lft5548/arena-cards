#!/usr/bin/env python3
"""Verify and reconstruct an ARENA_REPLAY_V1 battle replay."""
from __future__ import annotations

import argparse
import csv
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

FNV_OFFSET = 14695981039346656037
FNV_PRIME = 1099511628211
MAGIC = "ARENA_REPLAY_V1"


class ReplayError(ValueError):
    pass


@dataclass
class Event:
    revision: int
    turn_id: int
    action_id: int
    player: int
    kind: str
    payload: str


@dataclass
class Player:
    hp: int = 30
    energy: int = 3
    shield: int = 0
    hand: List[int] = field(default_factory=lambda: [1, 2, 3])
    deck: List[int] = field(default_factory=lambda: [1, 2, 3] * 3)
    refill_deck: List[int] = field(default_factory=lambda: [1, 2, 3] * 3)
    discard: List[int] = field(default_factory=list)
    boosts: Dict[str, Dict[str, int]] = field(default_factory=lambda: {
        "attack_boost": {"value": 0, "uses": 0}, "heal_boost": {"value": 0, "uses": 0}})
    statuses: Dict[str, Dict[str, int]] = field(default_factory=lambda: {
        "poison": {"value": 0, "turns": 0}, "regen": {"value": 0, "turns": 0},
        "burn": {"value": 0, "turns": 0}})


def _hash_u64(value: int) -> bytes:
    return struct.pack("<Q", value & 0xffffffffffffffff)


def digest(seed: int, events: Iterable[Event]) -> int:
    value = FNV_OFFSET

    def add(data: bytes) -> None:
        nonlocal value
        for byte in data:
            value = ((value ^ byte) * FNV_PRIME) & 0xffffffffffffffff

    def add_string(text: str) -> None:
        encoded = text.encode("utf-8", "surrogateescape")
        add(_hash_u64(len(encoded)))
        add(encoded)

    add(_hash_u64(seed))
    for event in events:
        add(_hash_u64(event.revision))
        add(_hash_u64(event.turn_id))
        add(_hash_u64(event.action_id))
        add(struct.pack("<I", event.player & 0xffffffff))
        add_string(event.kind)
        add_string(event.payload)
    return value


def _int(raw: str, label: str) -> int:
    if not raw or (raw.startswith("+") or raw.strip() != raw):
        raise ReplayError(f"invalid {label}")
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ReplayError(f"invalid {label}") from exc
    if str(value) != raw and not (raw.startswith("-") and str(value) == raw):
        raise ReplayError(f"invalid {label}")
    return value


def _unhex(raw: str, label: str) -> str:
    if len(raw) % 2 or any(ch not in "0123456789abcdefABCDEF" for ch in raw):
        raise ReplayError(f"invalid {label} hex")
    try:
        return bytes.fromhex(raw).decode("utf-8", "surrogateescape")
    except ValueError as exc:
        raise ReplayError(f"invalid {label} hex") from exc


def load_replay(path: Path) -> Tuple[str, int, int, List[Event]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            lines = stream.read().splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReplayError(f"cannot read replay: {exc}") from exc
    if len(lines) < 5 or lines[0] != MAGIC:
        raise ReplayError("invalid magic or truncated header")

    def header(index: int, key: str) -> str:
        fields = lines[index].split("\t")
        if len(fields) != 2 or fields[0] != key or not fields[1]:
            raise ReplayError(f"invalid {key} header")
        return fields[1]

    match_id = header(1, "match_id")
    if (not match_id or len(match_id) > 128 or match_id in {".", ".."} or
            any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in match_id)):
        raise ReplayError("invalid match_id")
    seed = _int(header(2, "seed"), "seed")
    expected_digest = _int(header(3, "digest"), "digest")
    count = _int(header(4, "count"), "count")
    if seed < 0 or expected_digest < 0 or count < 0 or count > 1_000_000:
        raise ReplayError("invalid replay header range")
    if len(lines) != 5 + count:
        raise ReplayError("event count or trailing data mismatch")
    events: List[Event] = []
    for index in range(count):
        fields = lines[5 + index].split("\t")
        if len(fields) != 6:
            raise ReplayError("invalid event fields")
        event = Event(_int(fields[0], "revision"), _int(fields[1], "turn_id"),
                      _int(fields[2], "action_id"), _int(fields[3], "player"),
                      _unhex(fields[4], "type"), _unhex(fields[5], "payload"))
        if event.player < -1 or event.player > 1:
            raise ReplayError("invalid player")
        if event.revision != index + 1 or not event.kind:
            raise ReplayError("non-contiguous replay revision")
        events.append(event)
    if digest(seed, events) != expected_digest:
        raise ReplayError("digest_mismatch")
    return match_id, seed, expected_digest, events


def parse_fields(payload: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for field in payload.split(";"):
        if not field or "=" not in field:
            raise ReplayError("malformed event payload")
        key, value = field.split("=", 1)
        if not key or key in result:
            raise ReplayError("duplicate or empty event field")
        result[key] = value
    return result


def _card_int(raw: str, label: str) -> int:
    text = raw.strip(" \t\r\n")
    digits = text[1:] if text.startswith(("+", "-")) else text
    if not digits or any(character not in "0123456789" for character in digits):
        raise ReplayError(f"invalid {label}")
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise ReplayError(f"invalid {label}") from exc
    if not -(1 << 31) <= value < (1 << 31):
        raise ReplayError(f"invalid {label}")
    return value


def load_cards(path: Path) -> Dict[int, Dict[str, int | str]]:
    cards: Dict[int, Dict[str, int | str]] = {}
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            lines = (line.strip(" \t\r\n") for line in stream)
            reader = csv.DictReader(line for line in lines if line and not line.startswith("#"))
            legacy_header = ["id", "name", "cost", "effect", "value"]
            if reader.fieldnames not in (legacy_header, legacy_header + ["duration"]):
                raise ReplayError("invalid card config header")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ReplayError("unexpected card field count")
                card_id = _card_int(row.get("id") or "", "card id")
                if card_id in cards or not 1 <= card_id <= 1000:
                    raise ReplayError("duplicate or invalid card id")
                if not (row.get("name") or "").strip():
                    raise ReplayError("empty card name")
                effect = (row.get("effect") or "").strip()
                if effect not in {"damage", "heal", "shield", "draw", "poison", "regen", "discard", "burn",
                                  "attack_boost", "heal_boost"}:
                    raise ReplayError("unsupported card effect")
                cost = _card_int(row.get("cost") or "", "card cost")
                value = _card_int(row.get("value") or "", "card value")
                duration = _card_int(row.get("duration") or "", "card duration") \
                    if "duration" in row else 0
                if not 0 <= cost <= 10 or not 0 <= value <= 1000:
                    raise ReplayError("invalid card numbers")
                if effect in {"poison", "regen", "burn"}:
                    if not 1 <= value <= 30 or not 1 <= duration <= 5:
                        raise ReplayError("invalid status parameters")
                elif effect in {"attack_boost", "heal_boost"}:
                    if not 1 <= value <= 10 or not 1 <= duration <= 5:
                        raise ReplayError("invalid boost parameters")
                elif effect == "discard":
                    if not 1 <= value <= 3 or duration != 0:
                        raise ReplayError("invalid discard parameters")
                elif duration != 0:
                    raise ReplayError("invalid status parameters")
                cards[card_id] = {"cost": cost, "effect": effect, "value": value,
                                  "duration": duration}
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReplayError(f"cannot read cards: {exc}") from exc
    if not {1, 2, 3}.issubset(cards):
        raise ReplayError("starter card id missing")
    return cards

def draw_one(player: Player, reset_empty_deck: bool) -> bool:
    if len(player.hand) >= 10:
        return False
    if not player.deck and reset_empty_deck:
        player.deck = player.refill_deck.copy()
    if not player.deck:
        return False
    player.hand.append(player.deck.pop(0))
    return True


def draw(player: Player, count: int, reset_empty_deck: bool) -> None:
    for _ in range(count):
        if not draw_one(player, reset_empty_deck):
            break


def consume_boost(player: Player, kind: str) -> int:
    boost = player.boosts[kind]
    if boost["uses"] == 0:
        return 0
    value = boost["value"]
    boost["uses"] -= 1
    if boost["uses"] == 0:
        boost["value"] = 0
    return value


def turn_start_ticks(player: Player) -> List[Dict[str, str]]:
    ticks = []
    for kind in ("poison", "regen"):
        status = player.statuses[kind]
        if player.hp <= 0:
            break
        if status["turns"] == 0:
            continue
        value = status["value"]
        player.hp = max(0, player.hp - value) if kind == "poison" else min(30, player.hp + value)
        status["turns"] -= 1
        ticks.append({"type": "status_tick", "status": kind, "value": str(value),
                      "remaining": str(status["turns"]), "hp": str(player.hp)})
        if status["turns"] == 0:
            status["value"] = 0
    return ticks


def turn_end_ticks(player: Player) -> List[Dict[str, str]]:
    status = player.statuses["burn"]
    if player.hp <= 0 or status["turns"] == 0:
        return []
    value = status["value"]
    player.hp = max(0, player.hp - value)
    status["turns"] -= 1
    tick = {"type": "status_tick", "phase": "end", "status": "burn", "value": str(value),
            "remaining": str(status["turns"]), "hp": str(player.hp)}
    if status["turns"] == 0:
        status["value"] = 0
    return [tick]


def reconstruct(events: List[Event], cards: Dict[int, Dict[str, int | str]], match_id: str) -> Dict[str, object]:
    if len(events) < 2 or events[0].kind != "match_start" or events[-1].kind != "match_result":
        raise ReplayError("replay must start with match_start and end with match_result")
    players = [Player(), Player()]
    start = parse_fields(events[0].payload)
    if (start.get("match_id") != match_id or events[0].player != -1 or
            events[0].action_id != 0):
        raise ReplayError("inconsistent match_start")
    rules = "legacy"
    if set(start) == {"match_id", "rules", "deck"} and start["rules"] in {
            "status_v1", "discard_v1", "turn_end_v1", "bonus_v1"}:
        rules = start["rules"]
        deck = [_int(card_id, "initial deck card") for card_id in start["deck"].split(",")]
        if deck != sorted(cards) * 3:
            raise ReplayError("initial deck does not match catalog")
        for player in players:
            player.deck = deck.copy()
            player.refill_deck = deck.copy()
    elif set(start) != {"match_id"}:
        raise ReplayError("unsupported match_start rules")
    supports_statuses = rules in {"status_v1", "discard_v1", "turn_end_v1", "bonus_v1"}
    supports_burn = rules in {"turn_end_v1", "bonus_v1"}
    supports_bonuses = rules == "bonus_v1"
    turn = 0
    turn_id = events[0].turn_id
    if turn_id != 1:
        raise ReplayError("invalid initial turn")
    action_ids = [0, 0]
    action_count = 0
    pending_ticks: List[Dict[str, str]] = []
    lethal_winner = None
    lethal_cause = ""
    terminal = parse_fields(events[-1].payload)
    if (set(terminal) - {"match_id", "winner", "turn_id", "reason"} or
            not {"match_id", "winner", "turn_id"}.issubset(terminal) or
            terminal.get("match_id") != match_id or events[-1].player != -1 or
            events[-1].action_id != 0):
        raise ReplayError("inconsistent match_result")
    for event in events[1:-1]:
        if event.kind != "battle_event":
            raise ReplayError("unexpected non-battle event")
        fields = parse_fields(event.payload)
        if fields.get("match_id") != match_id:
            raise ReplayError("event player or match mismatch")
        if _int(fields.get("turn_id", ""), "event turn_id") != event.turn_id:
            raise ReplayError("event turn mismatch")
        if (_int(fields.get("player", ""), "event player") != event.player or
                _int(fields.get("action_id", ""), "event action_id") != event.action_id):
            raise ReplayError("event identity mismatch")
        kind = fields.get("type")
        if kind == "status_tick":
            if not supports_statuses or event.action_id != 0 or not pending_ticks:
                raise ReplayError("unexpected status tick")
            expected = pending_ticks.pop(0)
            if fields != expected:
                raise ReplayError("inconsistent status tick")
            continue
        if pending_ticks:
            raise ReplayError("missing status tick before action")
        if event.player != turn:
            raise ReplayError("event player or match mismatch")
        if event.turn_id != turn_id:
            raise ReplayError("event turn mismatch")
        if lethal_winner is not None or turn_id > 40:
            raise ReplayError("events follow terminal action")
        if event.action_id <= action_ids[turn]:
            raise ReplayError("non-increasing action id")
        action_ids[turn] = event.action_id
        action_count += 1
        if kind == "end_turn":
            if set(fields) != {"match_id", "turn_id", "player", "action_id", "type"}:
                raise ReplayError("malformed end_turn event")
        else:
            is_status = kind in {"poison", "regen", "burn"}
            is_boost = kind in {"attack_boost", "heal_boost"}
            expected_keys = {"match_id", "turn_id", "player", "action_id", "card", "type", "value"}
            if is_status:
                expected_keys.add("duration")
            if kind == "discard":
                expected_keys.update({"target", "count"})
            if is_boost:
                expected_keys.add("uses")
            if supports_bonuses and kind in {"damage", "heal"}:
                expected_keys.add("bonus")
            if set(fields) != expected_keys:
                raise ReplayError("malformed card event")
            if kind not in {"damage", "heal", "shield", "draw", "poison", "regen", "discard", "burn",
                            "attack_boost", "heal_boost"} or \
                    (is_status and not supports_statuses) or (kind == "burn" and not supports_burn) or \
                    (is_boost and not supports_bonuses) or \
                    (kind == "discard" and rules not in {"discard_v1", "turn_end_v1", "bonus_v1"}):
                raise ReplayError("unsupported battle event")
            card_id = _int(fields.get("card", ""), "card")
            value = _int(fields.get("value", ""), "value")
            card = cards.get(card_id)
            if card is None or card["effect"] != kind or int(card["value"]) != value:
                raise ReplayError("card event does not match catalog")
            duration = _int(fields["duration"], "duration") if is_status else 0
            if is_status and (duration != int(card.get("duration", 0)) or
                              not 1 <= value <= 30 or not 1 <= duration <= 5):
                raise ReplayError("status card does not match catalog")
            uses = _int(fields["uses"], "boost uses") if is_boost else 0
            if is_boost and (uses != int(card.get("duration", 0)) or
                             not 1 <= value <= 10 or not 1 <= uses <= 5):
                raise ReplayError("boost card does not match catalog")
            player = players[turn]
            if supports_bonuses and kind in {"damage", "heal"}:
                boost = player.boosts["attack_boost" if kind == "damage" else "heal_boost"]
                expected_bonus = boost["value"] if boost["uses"] else 0
                if _int(fields["bonus"], "bonus") != expected_bonus:
                    raise ReplayError("inconsistent action bonus")
            if card_id not in player.hand or player.energy < int(card["cost"]):
                raise ReplayError("illegal card action")
            player.energy -= int(card["cost"])
            player.hand.remove(card_id)
            draw(player, 1, reset_empty_deck=True)
            opponent = players[1 - turn]
            if kind == "damage":
                bonus = consume_boost(player, "attack_boost") if supports_bonuses else 0
                damage = value + bonus
                blocked = min(damage, opponent.shield)
                opponent.shield -= blocked
                opponent.hp -= damage - blocked
            elif kind == "heal":
                bonus = consume_boost(player, "heal_boost") if supports_bonuses else 0
                player.hp = min(30, player.hp + value + bonus)
            elif kind == "shield":
                player.shield = min(20, player.shield + value)
            elif kind == "draw":
                draw(player, value, reset_empty_deck=False)
            elif kind == "discard":
                count = min(value, len(opponent.hand))
                if (not 1 <= value <= 3 or int(card.get("duration", 0)) != 0 or
                        _int(fields["target"], "discard target") != 1 - turn or
                        _int(fields["count"], "discard count") != count):
                    raise ReplayError("inconsistent discard event")
                opponent.discard.extend(opponent.hand[:count])
                del opponent.hand[:count]
            elif is_boost:
                player.boosts[kind] = {"value": value, "uses": uses}
            elif kind in {"poison", "burn"}:
                opponent.statuses[kind] = {"value": value, "turns": duration}
            else:
                player.statuses["regen"] = {"value": value, "turns": duration}
        if players[1 - turn].hp <= 0:
            lethal_winner, lethal_cause = turn, "damage"
            continue
        if supports_burn:
            pending_ticks.extend(
                {"match_id": match_id, "turn_id": str(turn_id), "player": str(turn),
                 "action_id": "0", **tick} for tick in turn_end_ticks(players[turn]))
            if players[turn].hp <= 0:
                lethal_winner, lethal_cause = 1 - turn, "burn"
                continue
        turn = 1 - turn
        turn_id += 1
        if turn_id <= 40:
            players[turn].energy = 3
            if supports_statuses:
                pending_ticks.extend(
                    {"match_id": match_id, "turn_id": str(turn_id), "player": str(turn),
                     "action_id": "0", **tick} for tick in turn_start_ticks(players[turn]))
                if players[turn].hp <= 0:
                    lethal_winner, lethal_cause = 1 - turn, "poison"
    if pending_ticks:
        raise ReplayError("missing status tick before result")
    result_turn = _int(terminal.get("turn_id", ""), "result turn_id")
    if result_turn != events[-1].turn_id or result_turn != turn_id:
        raise ReplayError("inconsistent terminal turn")
    winner = _int(terminal.get("winner", ""), "winner")
    if winner not in {-1, 0, 1}:
        raise ReplayError("invalid winner")
    reason = terminal.get("reason", "")
    if reason == "max_turns":
        expected_winner = -1 if players[0].hp == players[1].hp else (
            0 if players[0].hp > players[1].hp else 1)
        if (result_turn != 41 or action_count != 40 or winner != expected_winner or
                lethal_winner is not None or players[0].hp <= 0 or players[1].hp <= 0):
            raise ReplayError("invalid max-turn result")
    elif reason in {"turn_timeout", "reconnect_timeout"}:
        if lethal_winner is not None or result_turn > 40:
            raise ReplayError("timeout follows terminal battle state")
        if reason == "turn_timeout" and winner != 1 - turn:
            raise ReplayError("invalid turn-timeout winner")
        if winner == -1:
            raise ReplayError("timeout result cannot be a draw")
    elif reason in {"", "poison", "burn"}:
        cause = reason or "damage"
        final_action = parse_fields(events[-2].payload) if len(events) > 2 else {}
        if (winner not in (0, 1) or lethal_winner != winner or lethal_cause != cause or
                players[winner].hp <= 0 or players[1 - winner].hp > 0 or
                events[-2].kind != "battle_event" or
                (cause == "damage" and (final_action.get("type") != "damage" or
                                        events[-2].player != winner)) or
                (cause == "poison" and (final_action.get("type") != "status_tick" or
                                        final_action.get("status") != "poison" or
                                        events[-2].player != 1 - winner)) or
                (cause == "burn" and (not supports_burn or
                                      final_action.get("type") != "status_tick" or
                                      final_action.get("phase") != "end" or
                                      final_action.get("status") != "burn" or
                                      events[-2].player != turn or turn != 1 - winner or
                                      events[-2].turn_id != result_turn))):
            raise ReplayError("normal result is not a lethal win" if not reason else
                              f"{reason} result is not a lethal win")
    else:
        raise ReplayError("unsupported terminal reason")
    return {"match_id": match_id, "winner": winner, "turn_id": result_turn,
            "reason": reason, "rules": rules,
            "players": [{"hp": player.hp, "energy": player.energy, "shield": player.shield,
                         "hand": player.hand, "deck": player.deck, "discard": player.discard,
                         **({"boosts": player.boosts} if supports_bonuses else {}),
                         "statuses": {kind: status for kind, status in player.statuses.items()
                                      if supports_burn or kind != "burn"}}
                        for player in players]}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    parser.add_argument("--cards", type=Path,
                        default=Path(__file__).resolve().parents[2] / "server" / "config" / "cards.csv")
    args = parser.parse_args()
    try:
        match_id, seed, expected_digest, events = load_replay(args.replay)
        state = reconstruct(events, load_cards(args.cards), match_id)
        state.update({"seed": seed, "digest": str(expected_digest),
                      "revision": len(events), "verified": True})
    except ReplayError as exc:
        parser.error(str(exc))
    print(json.dumps(state, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
