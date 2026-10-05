"""Focused offline replay format, digest, reconstruction, and corruption tests."""
from __future__ import annotations

import sys
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARDS_PATH = ROOT / "server" / "config" / "cards.csv"
sys.path.insert(0, str(ROOT / "tools" / "replay"))
import replay_battle as replay


def _event(revision: int, turn_id: int, action_id: int, player: int,
           kind: str, payload: str) -> replay.Event:
    return replay.Event(revision, turn_id, action_id, player, kind, payload)


def _payload(match_id: str, turn_id: int, player: int,
             action_id: int, kind: str, extra: str = "") -> str:
    fields = [f"match_id={match_id}", f"turn_id={turn_id}",
              f"player={player}", f"action_id={action_id}", f"type={kind}"]
    if extra:
        fields.append(extra)
    return ";".join(fields)


def _lethal_replay(match_id: str = "lethal-match") -> list[replay.Event]:
    events = [_event(1, 1, 0, -1, "match_start", f"match_id={match_id}")]
    events.append(_event(
        2, 1, 1, 0, "battle_event",
        _payload(match_id, 1, 0, 1, "damage", "card=1;value=30")))
    events.append(_event(3, 1, 0, -1, "match_result",
                         f"match_id={match_id};winner=0;turn_id=1"))
    return events


def _max_turn_replay(match_id: str = "max-turn-match") -> list[replay.Event]:
    events = [_event(1, 1, 0, -1, "match_start", f"match_id={match_id}")]
    action_ids = [0, 0]
    for turn_id in range(1, 41):
        player = (turn_id - 1) % 2
        action_ids[player] += 1
        events.append(_event(
            len(events) + 1, turn_id, action_ids[player], player, "battle_event",
            _payload(match_id, turn_id, player, action_ids[player], "end_turn")))
    events.append(_event(42, 41, 0, -1, "match_result",
                         f"match_id={match_id};winner=-1;reason=max_turns;turn_id=41"))
    return events


def _write(path: Path, match_id: str, seed: int,
           events: list[replay.Event], digest: int | None = None) -> int:
    digest = replay.digest(seed, events) if digest is None else digest
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(f"ARENA_REPLAY_V1\nmatch_id\t{match_id}\nseed\t{seed}\n"
                     f"digest\t{digest}\ncount\t{len(events)}\n")
        for event in events:
            stream.write(f"{event.revision}\t{event.turn_id}\t{event.action_id}\t"
                         f"{event.player}\t{event.kind.encode().hex()}\t"
                         f"{event.payload.encode().hex()}\n")
    return digest


def _status_cards() -> dict[int, dict[str, int | str]]:
    return {
        1: {"cost": 1, "effect": "poison", "value": 4, "duration": 2},
        2: {"cost": 1, "effect": "regen", "value": 3, "duration": 2},
        3: {"cost": 1, "effect": "shield", "value": 6, "duration": 0},
    }


def _status_start(match_id: str, cards: dict) -> replay.Event:
    deck = ",".join(str(card_id) for card_id in sorted(cards) * 3)
    return _event(1, 1, 0, -1, "match_start", f"match_id={match_id};rules=status_v1;deck={deck}")


def _status_replay(match_id: str = "status-match") -> list[replay.Event]:
    events = [_status_start(match_id, _status_cards())]
    records = [
        (1, 1, 0, "poison", "card=1;value=4;duration=2"),
        (2, 0, 1, "status_tick", "status=poison;value=4;remaining=1;hp=26"),
        (2, 1, 1, "regen", "card=2;value=3;duration=2"),
        (3, 2, 0, "poison", "card=1;value=4;duration=2"),
        (4, 0, 1, "status_tick", "status=poison;value=4;remaining=1;hp=22"),
        (4, 0, 1, "status_tick", "status=regen;value=3;remaining=1;hp=25"),
        (4, 2, 1, "end_turn", ""),
        (5, 3, 0, "end_turn", ""),
        (6, 0, 1, "status_tick", "status=poison;value=4;remaining=0;hp=21"),
        (6, 0, 1, "status_tick", "status=regen;value=3;remaining=0;hp=24"),
    ]
    for turn_id, action_id, player, kind, extra in records:
        events.append(_event(len(events) + 1, turn_id, action_id, player, "battle_event",
                             _payload(match_id, turn_id, player, action_id, kind, extra)))
    events.append(_event(len(events) + 1, 6, 0, -1, "match_result",
                         f"match_id={match_id};winner=0;reason=turn_timeout;turn_id=6"))
    return events


def _poison_lethal_replay() -> tuple[dict, list[replay.Event]]:
    cards = {
        1: {"cost": 1, "effect": "shield", "value": 6},
        2: {"cost": 1, "effect": "regen", "value": 30, "duration": 2},
        3: {"cost": 1, "effect": "poison", "value": 30, "duration": 2},
    }
    match_id = "poison-lethal"
    events = [_status_start(match_id, cards)]
    records = [
        (1, 1, 0, "shield", "card=1;value=6"),
        (2, 1, 1, "end_turn", ""),
        (3, 2, 0, "regen", "card=2;value=30;duration=2"),
        (4, 2, 1, "poison", "card=3;value=30;duration=2"),
        (5, 0, 0, "status_tick", "status=poison;value=30;remaining=1;hp=0"),
    ]
    for turn_id, action_id, player, kind, extra in records:
        events.append(_event(len(events) + 1, turn_id, action_id, player, "battle_event",
                             _payload(match_id, turn_id, player, action_id, kind, extra)))
    events.append(_event(len(events) + 1, 5, 0, -1, "match_result",
                         f"match_id={match_id};winner=1;reason=poison;turn_id=5"))
    return cards, events


def _discard_cards(count: int = 2) -> dict[int, dict[str, int | str]]:
    return {
        1: {"cost": 2, "effect": "discard", "value": count, "duration": 0},
        2: {"cost": 1, "effect": "draw", "value": 0, "duration": 0},
        3: {"cost": 1, "effect": "shield", "value": 0, "duration": 0},
    }


def _discard_replay(count: int = 2) -> list[replay.Event]:
    match_id = "discard-match"
    start = _status_start(match_id, _discard_cards(count))
    start.payload = start.payload.replace("status_v1", "discard_v1")
    events = [start]
    action_ids = [0, 0]
    remaining = 3
    for turn_id in range(1, 10):
        player = (turn_id - 1) % 2
        action_ids[player] += 1
        kind, extra = "end_turn", ""
        if turn_id in (1, 3, 9):
            actual = min(count, remaining)
            remaining -= actual
            kind, extra = "discard", f"card=1;value={count};target=1;count={actual}"
        elif turn_id == 5:
            kind, extra = "draw", "card=2;value=0"
        elif turn_id == 7:
            kind, extra = "shield", "card=3;value=0"
        events.append(_event(len(events) + 1, turn_id, action_ids[player], player, "battle_event",
                             _payload(match_id, turn_id, player, action_ids[player], kind, extra)))
    events.append(_event(len(events) + 1, 10, 0, -1, "match_result",
                         f"match_id={match_id};winner=0;reason=turn_timeout;turn_id=10"))
    return events



def _burn_cards(value: int = 4, duration: int = 2) -> dict[int, dict[str, int | str]]:
    return {
        1: {"cost": 1, "effect": "burn", "value": value, "duration": duration},
        2: {"cost": 1, "effect": "regen", "value": 3, "duration": 2},
        3: {"cost": 1, "effect": "shield", "value": 6, "duration": 0},
    }


def _turn_end_trace(cards: dict, records: list, terminal_turn: int,
                    winner: int, reason: str, match_id: str = "burn-match") -> list[replay.Event]:
    start = _status_start(match_id, cards)
    start.payload = start.payload.replace("status_v1", "turn_end_v1")
    events = [start]
    for turn_id, action_id, player, kind, extra in records:
        events.append(_event(len(events) + 1, turn_id, action_id, player, "battle_event",
                             _payload(match_id, turn_id, player, action_id, kind, extra)))
    events.append(_event(
        len(events) + 1, terminal_turn, 0, -1, "match_result",
        f"match_id={match_id};winner={winner};reason={reason};turn_id={terminal_turn}"))
    return events


def _burn_replay(refresh: bool = False) -> list[replay.Event]:
    records = [
        (1, 1, 0, "burn", "card=1;value=4;duration=2"),
        (2, 1, 1, "shield", "card=3;value=6"),
        (2, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26"),
        (3, 2, 0, "burn" if refresh else "end_turn",
         "card=1;value=4;duration=2" if refresh else ""),
        (4, 2, 1, "end_turn", ""),
        (4, 0, 1, "status_tick",
         f"phase=end;status=burn;value=4;remaining={1 if refresh else 0};hp=22"),
    ]
    if refresh:
        records.extend([
            (5, 3, 0, "end_turn", ""),
            (6, 3, 1, "end_turn", ""),
            (6, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=0;hp=18"),
        ])
    return _turn_end_trace(_burn_cards(), records, 7 if refresh else 5, 1, "turn_timeout")


def _cross_phase_replay() -> tuple[dict, list[replay.Event]]:
    cards = _burn_cards()
    cards[2] = {"cost": 1, "effect": "poison", "value": 3, "duration": 2}
    cards[3] = {"cost": 1, "effect": "regen", "value": 2, "duration": 2}
    records = [
        (1, 1, 0, "burn", "card=1;value=4;duration=2"),
        (2, 1, 1, "poison", "card=2;value=3;duration=2"),
        (2, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26"),
        (3, 0, 0, "status_tick", "status=poison;value=3;remaining=1;hp=27"),
        (3, 2, 0, "regen", "card=3;value=2;duration=2"),
        (4, 2, 1, "end_turn", ""),
        (4, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=0;hp=22"),
        (5, 0, 0, "status_tick", "status=poison;value=3;remaining=0;hp=24"),
        (5, 0, 0, "status_tick", "status=regen;value=2;remaining=1;hp=26"),
    ]
    return cards, _turn_end_trace(cards, records, 5, 1, "turn_timeout")


def _final_turn_burn_replay(value: int) -> tuple[dict, list[replay.Event]]:
    cards = _burn_cards(value)
    cards[2] = {"cost": 1, "effect": "poison", "value": 4, "duration": 2}
    records = []
    action_ids = [0, 0]
    for turn_id in range(1, 41):
        player = (turn_id - 1) % 2
        action_ids[player] += 1
        kind, extra = "end_turn", ""
        if turn_id == 39:
            kind, extra = "burn", f"card=1;value={value};duration=2"
        elif turn_id == 40:
            kind, extra = "poison", "card=2;value=4;duration=2"
        records.append((turn_id, action_ids[player], player, kind, extra))
    records.append((40, 0, 1, "status_tick",
                    f"phase=end;status=burn;value={value};remaining=1;hp={max(0, 30 - value)}"))
    lethal = value == 30
    return cards, _turn_end_trace(cards, records, 40 if lethal else 41, 0,
                                 "burn" if lethal else "max_turns")



def _bonus_cards() -> dict[int, dict[str, int | str]]:
    return {
        1: {"cost": 1, "effect": "attack_boost", "value": 3, "duration": 2},
        2: {"cost": 1, "effect": "heal_boost", "value": 4, "duration": 2},
        3: {"cost": 1, "effect": "damage", "value": 5, "duration": 0},
        4: {"cost": 1, "effect": "heal", "value": 6, "duration": 0},
    }


def _bonus_trace(cards: dict, records: list, terminal_turn: int,
                 winner: int, reason: str = "turn_timeout") -> list[replay.Event]:
    events = _turn_end_trace(cards, records, terminal_turn, winner, reason, "bonus-match")
    events[0].payload = events[0].payload.replace("turn_end_v1", "bonus_v1")
    return events


def _bonus_action_trace(cards: dict, actions: list[tuple[int | None, int | None]]) -> list[replay.Event]:
    records = []
    action_ids = [0, 0]
    for turn_id, (card_id, bonus) in enumerate(actions, 1):
        player = (turn_id - 1) % 2
        action_ids[player] += 1
        kind, extra = "end_turn", ""
        if card_id is not None:
            card = cards[card_id]
            kind = str(card["effect"])
            extra = f"card={card_id};value={card['value']}"
            if kind in {"attack_boost", "heal_boost"}:
                extra += f";uses={card['duration']}"
            elif kind in {"damage", "heal"}:
                extra += f";bonus={bonus}"
        records.append((turn_id, action_ids[player], player, kind, extra))
    terminal_turn = len(actions) + 1
    return _bonus_trace(cards, records, terminal_turn, terminal_turn % 2)


class ReplayBattleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.cards = replay.load_cards(CARDS_PATH)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_file_format_and_fnv_digest_vector(self) -> None:
        events = [_event(1, 2, 3, -1, "battle_event", "type=damage;value=8")]
        expected = replay.digest(0x12345678, events)
        self.assertEqual(expected, 14449955194524173080)
        path = self.directory / "vector.replay"
        _write(path, "vector-match", 0x12345678, events, expected)
        match_id, seed, loaded_digest, loaded_events = replay.load_replay(path)
        self.assertEqual((match_id, seed, loaded_digest, loaded_events),
                         ("vector-match", 0x12345678, expected, events))

    def test_reconstructs_normal_lethal_result(self) -> None:
        events = _lethal_replay()
        cards = dict(self.cards)
        cards[1] = {"cost": 2, "effect": "damage", "value": 30}
        state = replay.reconstruct(events, cards, "lethal-match")
        self.assertEqual(state["winner"], 0)
        self.assertEqual(state["turn_id"], 1)
        self.assertEqual(state["players"][1]["hp"], 0)

    def test_reconstructs_max_turn_draw(self) -> None:
        events = _max_turn_replay()
        state = replay.reconstruct(events, self.cards, "max-turn-match")
        self.assertEqual(state["winner"], -1)
        self.assertEqual(state["turn_id"], 41)
        self.assertEqual(state["players"][0]["energy"], 3)

    def test_accepts_timeout_win_without_hp_damage(self) -> None:
        match_id = "timeout-match"
        events = [
            _event(1, 1, 0, -1, "match_start", f"match_id={match_id}"),
            _event(2, 1, 0, -1, "match_result",
                   f"match_id={match_id};winner=1;reason=turn_timeout;turn_id=1"),
        ]
        state = replay.reconstruct(events, self.cards, match_id)
        self.assertEqual(state["winner"], 1)
        self.assertEqual(state["players"][0]["hp"], 30)

    def test_cli_emits_verified_json(self) -> None:
        match_id = "cli-match"
        events = [
            _event(1, 1, 0, -1, "match_start", f"match_id={match_id}"),
            _event(2, 1, 0, -1, "match_result",
                   f"match_id={match_id};winner=1;reason=turn_timeout;turn_id=1"),
        ]
        path = self.directory / "cli.replay"
        _write(path, match_id, 99, events)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "replay" / "replay_battle.py"), str(path),
             "--cards", str(CARDS_PATH)],
            check=True, capture_output=True, text=True)
        decoded = json.loads(result.stdout)
        self.assertTrue(decoded["verified"])
        self.assertEqual(decoded["match_id"], match_id)

    def test_rejects_digest_tampering_and_revision_gap(self) -> None:
        path = self.directory / "corrupt.replay"
        events = _lethal_replay("corrupt-match")
        _write(path, "corrupt-match", 11, events)
        content = path.read_text(encoding="utf-8")
        path.write_text(content.replace("76616c75653d3330", "76616c75653d3331"), encoding="utf-8")
        with self.assertRaisesRegex(replay.ReplayError, "digest_mismatch"):
            replay.load_replay(path)

        events[1].revision = 3
        _write(path, "corrupt-match", 11, events)
        with self.assertRaisesRegex(replay.ReplayError, "non-contiguous"):
            replay.load_replay(path)

    def test_rejects_inconsistent_terminal_outcome(self) -> None:
        events = _max_turn_replay("bad-result")
        events[-1].payload = "match_id=bad-result;winner=0;reason=max_turns;turn_id=41"
        with self.assertRaisesRegex(replay.ReplayError, "invalid max-turn result"):
            replay.reconstruct(events, self.cards, "bad-result")

    def test_reconstructs_status_refresh_order_and_expiry(self) -> None:
        state = replay.reconstruct(_status_replay(), _status_cards(), "status-match")
        self.assertEqual(state["rules"], "status_v1")
        self.assertEqual(state["players"][1]["hp"], 24)
        self.assertEqual(state["players"][1]["statuses"], {
            "poison": {"value": 0, "turns": 0}, "regen": {"value": 0, "turns": 0}})

    def test_status_trace_round_trips_format_and_digest(self) -> None:
        events = _status_replay()
        path = self.directory / "status.replay"
        expected = _write(path, "status-match", 2026, events)
        match_id, seed, loaded_digest, loaded_events = replay.load_replay(path)
        self.assertEqual((seed, loaded_digest, loaded_events), (2026, expected, events))
        self.assertEqual(replay.reconstruct(loaded_events, _status_cards(), match_id)["winner"], 0)

    def test_rejects_missing_tick_before_action_and_result(self) -> None:
        for position in (2, -2):
            with self.subTest(position=position):
                events = _status_replay()
                events.pop(position)
                with self.assertRaisesRegex(replay.ReplayError, "missing status tick"):
                    replay.reconstruct(events, _status_cards(), "status-match")

    def test_rejects_tick_value_hp_remaining_and_order_tampering(self) -> None:
        for original, replacement in (("value=4", "value=5"), ("hp=22", "hp=23"),
                                      ("remaining=1", "remaining=2"), ("status=poison", "status=regen")):
            with self.subTest(replacement=replacement):
                events = _status_replay()
                events[5].payload = events[5].payload.replace(original, replacement)
                with self.assertRaisesRegex(replay.ReplayError, "inconsistent status tick"):
                    replay.reconstruct(events, _status_cards(), "status-match")
        events = _status_replay()
        events[5], events[6] = events[6], events[5]
        with self.assertRaisesRegex(replay.ReplayError, "inconsistent status tick"):
            replay.reconstruct(events, _status_cards(), "status-match")

    def test_rejects_nonzero_system_action_id(self) -> None:
        events = _status_replay()
        events[2].action_id = 1
        events[2].payload = events[2].payload.replace("action_id=0", "action_id=1")
        with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
            replay.reconstruct(events, _status_cards(), "status-match")

    def test_rejects_status_card_duration_tampering(self) -> None:
        events = _status_replay()
        events[1].payload = events[1].payload.replace("duration=2", "duration=3")
        with self.assertRaisesRegex(replay.ReplayError, "status card does not match catalog"):
            replay.reconstruct(events, _status_cards(), "status-match")

    def test_rejects_status_events_in_legacy_rules(self) -> None:
        events = _status_replay()
        events[0].payload = "match_id=status-match"
        with self.assertRaisesRegex(replay.ReplayError, "unsupported battle event"):
            replay.reconstruct(events, _status_cards(), "status-match")

    def test_rejects_unknown_rules_and_mismatched_initial_deck(self) -> None:
        events = _status_replay()
        events[0].payload = events[0].payload.replace("status_v1", "status_v2")
        with self.assertRaisesRegex(replay.ReplayError, "unsupported match_start rules"):
            replay.reconstruct(events, _status_cards(), "status-match")
        events = _status_replay()
        events[0].payload = events[0].payload.replace("deck=1,2,3", "deck=3,2,1")
        with self.assertRaisesRegex(replay.ReplayError, "initial deck does not match catalog"):
            replay.reconstruct(events, _status_cards(), "status-match")

    def test_poison_lethal_bypasses_shield_and_stops_regen(self) -> None:
        cards, events = _poison_lethal_replay()
        state = replay.reconstruct(events, cards, "poison-lethal")
        self.assertEqual((state["winner"], state["turn_id"]), (1, 5))
        self.assertEqual(state["players"][0]["hp"], 0)
        self.assertEqual(state["players"][0]["shield"], 6)
        self.assertEqual(state["players"][0]["statuses"]["regen"], {"value": 30, "turns": 2})

    def test_rejects_regen_after_lethal_poison_and_wrong_winner(self) -> None:
        cards, events = _poison_lethal_replay()
        events.insert(-1, _event(7, 5, 0, 0, "battle_event", _payload(
            "poison-lethal", 5, 0, 0, "status_tick", "status=regen;value=30;remaining=1;hp=30")))
        with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
            replay.reconstruct(events, cards, "poison-lethal")
        cards, events = _poison_lethal_replay()
        events[-1].payload = events[-1].payload.replace("winner=1", "winner=0")
        with self.assertRaisesRegex(replay.ReplayError, "poison result is not a lethal win"):
            replay.reconstruct(events, cards, "poison-lethal")

    def test_max_turn_result_counts_actions_not_system_ticks(self) -> None:
        match_id = "status-max-turn"
        events = [_status_start(match_id, _status_cards())]
        action_ids = [0, 0]
        for turn_id in range(1, 41):
            player = (turn_id - 1) % 2
            action_ids[player] += 1
            kind, extra = ("poison", "card=1;value=4;duration=2") if turn_id == 39 else ("end_turn", "")
            events.append(_event(len(events) + 1, turn_id, action_ids[player], player, "battle_event",
                                 _payload(match_id, turn_id, player, action_ids[player], kind, extra)))
            if turn_id == 39:
                events.append(_event(len(events) + 1, 40, 0, 1, "battle_event", _payload(
                    match_id, 40, 1, 0, "status_tick", "status=poison;value=4;remaining=1;hp=26")))
        events.append(_event(len(events) + 1, 41, 0, -1, "match_result",
                             f"match_id={match_id};winner=0;reason=max_turns;turn_id=41"))
        state = replay.reconstruct(events, _status_cards(), match_id)
        self.assertEqual(len(events), 43)
        self.assertEqual(state["players"][1]["statuses"]["poison"]["turns"], 1)

    def test_no_turn_41_tick_after_final_status_card(self) -> None:
        events = _max_turn_replay()
        events[0] = _status_start("max-turn-match", _status_cards())
        events[-2].payload = _payload("max-turn-match", 40, 1, 20, "poison", "card=1;value=4;duration=2")
        state = replay.reconstruct(events, _status_cards(), "max-turn-match")
        self.assertEqual(state["players"][0]["hp"], 30)
        self.assertEqual(state["players"][0]["statuses"]["poison"], {"value": 4, "turns": 2})

    def test_empty_deck_refills_from_recorded_pattern(self) -> None:
        player = replay.Player(deck=[], refill_deck=[7, 8, 1])
        self.assertTrue(replay.draw_one(player, reset_empty_deck=True))
        self.assertEqual((player.hand[-1], player.deck), (7, [8, 1]))

    def test_discard_reconstructs_order_short_and_empty_hands(self) -> None:
        for count in (1, 2, 3):
            with self.subTest(count=count):
                state = replay.reconstruct(_discard_replay(count), _discard_cards(count), "discard-match")
                self.assertEqual(state["rules"], "discard_v1")
                self.assertEqual(state["players"][1]["hand"], [])
                self.assertEqual(state["players"][1]["discard"], [1, 2, 3])
                self.assertEqual(state["players"][1]["deck"], [1, 2, 3] * 3)
                self.assertEqual(state["players"][0]["discard"], [])
                self.assertEqual(state["players"][0]["hand"], [2, 3, 2])

    def test_discard_trace_round_trips_format_and_digest(self) -> None:
        events = _discard_replay()
        path = self.directory / "discard.replay"
        expected = _write(path, "discard-match", 2026, events)
        match_id, seed, loaded_digest, loaded = replay.load_replay(path)
        self.assertEqual((seed, loaded_digest, loaded), (2026, expected, events))
        self.assertEqual(replay.reconstruct(loaded, _discard_cards(), match_id)["players"][1]["discard"],
                         [1, 2, 3])

    def test_rejects_discard_count_target_and_private_identity_tampering(self) -> None:
        for original, replacement, expected_error in (
                ("count=2", "count=1", "inconsistent discard event"),
                ("count=2", "count=-1", "inconsistent discard event"),
                ("target=1", "target=0", "inconsistent discard event"),
                ("target=1", "target=2", "inconsistent discard event"),
                ("value=2", "value=3", "card event does not match catalog"),
                ("count=2", "count=2;discard=1,2", "malformed card event"),
                (";target=1", "", "malformed card event")):
            with self.subTest(replacement=replacement):
                events = _discard_replay()
                events[1].payload = events[1].payload.replace(original, replacement)
                with self.assertRaisesRegex(replay.ReplayError, expected_error):
                    replay.reconstruct(events, _discard_cards(), "discard-match")
        events = _discard_replay()
        events[-2].payload = events[-2].payload.replace("count=0", "count=1")
        with self.assertRaisesRegex(replay.ReplayError, "inconsistent discard event"):
            replay.reconstruct(events, _discard_cards(), "discard-match")

    def test_rejects_discard_events_in_legacy_and_status_rules(self) -> None:
        for start in ("match_id=discard-match", _status_start("discard-match", _discard_cards()).payload):
            with self.subTest(start=start):
                events = _discard_replay()
                events[0].payload = start
                with self.assertRaisesRegex(replay.ReplayError, "unsupported battle event"):
                    replay.reconstruct(events, _discard_cards(), "discard-match")

    def test_status_ticks_are_preserved_under_discard_rules(self) -> None:
        events = _status_replay()
        events[0].payload = events[0].payload.replace("status_v1", "discard_v1")
        state = replay.reconstruct(events, _status_cards(), "status-match")
        self.assertEqual(state["players"][1]["hp"], 24)
        self.assertEqual(state["players"][1]["statuses"], {
            "poison": {"value": 0, "turns": 0}, "regen": {"value": 0, "turns": 0}})

    def test_old_status_catalog_must_be_supplied_for_old_replay(self) -> None:
        cards = {card_id: card for card_id, card in self.cards.items() if card_id not in {9, 10, 11, 12}}
        events = [_status_start("old-status", cards), _event(2, 1, 0, -1, "match_result",
                  "match_id=old-status;winner=1;reason=turn_timeout;turn_id=1")]
        state = replay.reconstruct(events, cards, "old-status")
        self.assertEqual(state["rules"], "status_v1")
        self.assertEqual(state["players"][0]["discard"], [])
        with self.assertRaisesRegex(replay.ReplayError, "initial deck does not match catalog"):
            replay.reconstruct(events, self.cards, "old-status")

    def test_discard_catalog_bounds_and_legacy_header(self) -> None:
        path = self.directory / "discard.csv"
        path.write_text("id,name,cost,effect,value\n1,D,2,discard,2\n2,H,1,heal,4\n3,S,1,shield,6\n",
                        encoding="utf-8")
        self.assertEqual(replay.load_cards(path)[1]["duration"], 0)
        for count in (1, 2, 3):
            path.write_text(f"id,name,cost,effect,value,duration\n1,D,2,discard,{count},0\n"
                            "2,H,1,heal,4,0\n3,S,1,shield,6,0\n", encoding="utf-8")
            self.assertEqual(replay.load_cards(path)[1]["value"], count)
        for value, duration in ((0, 0), (4, 0), (2, 1), (2, -1)):
            with self.subTest(value=value, duration=duration):
                path.write_text(f"id,name,cost,effect,value,duration\n1,D,2,discard,{value},{duration}\n"
                                "2,H,1,heal,4,0\n3,S,1,shield,6,0\n", encoding="utf-8")
                with self.assertRaisesRegex(replay.ReplayError, "invalid discard parameters"):
                    replay.load_cards(path)

    def test_status_catalog_validation_and_legacy_header(self) -> None:
        path = self.directory / "cards.csv"
        path.write_text("id,name,cost,effect,value\n1,A,1,damage,8\n2,B,1,heal,4\n3,C,1,shield,6\n",
                        encoding="utf-8")
        self.assertEqual(replay.load_cards(path)[1]["duration"], 0)
        path.write_text("# card config\n\nid,name,cost,effect,value,duration\n"
                        "1,A,01,poison,+003,+02\n2,B,1,heal,4,0\n3,C,1,shield,6,0\n",
                        encoding="utf-8")
        self.assertEqual(replay.load_cards(path)[1], {
            "cost": 1, "effect": "poison", "value": 3, "duration": 2})
        for value, duration in ((0, 2), (31, 2), (3, 0), (3, 6), (3, -1)):
            with self.subTest(value=value, duration=duration):
                path.write_text(f"id,name,cost,effect,value,duration\n1,A,1,poison,{value},{duration}\n"
                                "2,B,1,heal,4,0\n3,C,1,shield,6,0\n", encoding="utf-8")
                with self.assertRaisesRegex(replay.ReplayError, "invalid status parameters"):
                    replay.load_cards(path)


    def test_burn_expiry_refresh_and_shield_bypass(self) -> None:
        for refresh, hp in ((False, 22), (True, 18)):
            with self.subTest(refresh=refresh):
                state = replay.reconstruct(_burn_replay(refresh), _burn_cards(), "burn-match")
                holder = state["players"][1]
                self.assertEqual(state["rules"], "turn_end_v1")
                self.assertEqual((holder["hp"], holder["shield"]), (hp, 6))
                self.assertEqual(holder["statuses"]["burn"], {"value": 0, "turns": 0})
                self.assertEqual(state["players"][0]["statuses"]["burn"], {"value": 0, "turns": 0})

    def test_burn_refresh_replaces_value_and_duration(self) -> None:
        cards = _burn_cards()
        cards[2] = {"cost": 1, "effect": "burn", "value": 7, "duration": 1}
        records = [
            (1, 1, 0, "burn", "card=1;value=4;duration=2"),
            (2, 1, 1, "end_turn", ""),
            (2, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26"),
            (3, 2, 0, "burn", "card=2;value=7;duration=1"),
            (4, 2, 1, "end_turn", ""),
            (4, 0, 1, "status_tick", "phase=end;status=burn;value=7;remaining=0;hp=19"),
        ]
        state = replay.reconstruct(
            _turn_end_trace(cards, records, 5, 1, "turn_timeout"), cards, "burn-match")
        self.assertEqual(state["players"][1]["hp"], 19)
        self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 0, "turns": 0})

    def test_cross_phase_tick_order_and_full_event_identities(self) -> None:
        cards, events = _cross_phase_replay()
        state = replay.reconstruct(events, cards, "burn-match")
        self.assertEqual((state["players"][0]["hp"], state["players"][1]["hp"]), (26, 22))
        self.assertEqual(state["players"][0]["statuses"]["regen"], {"value": 2, "turns": 1})
        self.assertEqual([(event.turn_id, event.player, event.action_id) for event in events[7:10]],
                         [(4, 1, 0), (5, 0, 0), (5, 0, 0)])
        self.assertNotIn("phase", replay.parse_fields(events[8].payload))

    def test_rejects_burn_phase_value_remaining_hp_and_extra_fields(self) -> None:
        for original, replacement in (
                ("phase=end", "phase=start"), ("phase=end;", ""),
                ("phase=end", "phase=unknown"), ("value=4", "value=5"),
                ("remaining=1", "remaining=0"), ("hp=26", "hp=27"),
                ("status=burn", "status=poison"), ("hp=26", "hp=26;count=1")):
            with self.subTest(replacement=replacement):
                events = _burn_replay()
                events[3].payload = events[3].payload.replace(original, replacement)
                with self.assertRaisesRegex(replay.ReplayError, "inconsistent status tick"):
                    replay.reconstruct(events, _burn_cards(), "burn-match")

    def test_rejects_burn_tick_player_turn_and_action_identity(self) -> None:
        for attribute, original, value in (
                ("player", 1, 0), ("turn_id", 2, 3), ("action_id", 0, 1)):
            for change_payload in (False, True):
                with self.subTest(attribute=attribute, change_payload=change_payload):
                    events = _burn_replay()
                    setattr(events[3], attribute, value)
                    if change_payload:
                        events[3].payload = events[3].payload.replace(
                            f"{attribute}={original}", f"{attribute}={value}")
                    with self.assertRaises(replay.ReplayError):
                        replay.reconstruct(events, _burn_cards(), "burn-match")

    def test_rejects_missing_duplicate_and_reordered_cross_phase_ticks(self) -> None:
        for position in (3, 4, 7, 8, 9):
            with self.subTest(missing=position):
                cards, events = _cross_phase_replay()
                events.pop(position)
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "burn-match")
        for position in (3, 7, 8, 9):
            with self.subTest(duplicate=position):
                cards, events = _cross_phase_replay()
                events.insert(position + 1, events[position])
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "burn-match")
        for first, second in ((3, 4), (7, 8), (8, 9)):
            with self.subTest(reordered=(first, second)):
                cards, events = _cross_phase_replay()
                events[first], events[second] = events[second], events[first]
                with self.assertRaisesRegex(replay.ReplayError, "inconsistent status tick"):
                    replay.reconstruct(events, cards, "burn-match")

    def test_lethal_burn_clamps_hp_and_keeps_outgoing_turn(self) -> None:
        cards = _burn_cards(30, 1)
        cards[2] = {"cost": 1, "effect": "poison", "value": 4, "duration": 2}
        records = [
            (1, 1, 0, "burn", "card=1;value=30;duration=1"),
            (2, 1, 1, "shield", "card=3;value=6"),
            (2, 0, 1, "status_tick", "phase=end;status=burn;value=30;remaining=0;hp=0"),
        ]
        state = replay.reconstruct(_turn_end_trace(cards, records, 2, 0, "burn"), cards, "burn-match")
        self.assertEqual((state["winner"], state["turn_id"], state["reason"]), (0, 2, "burn"))
        self.assertEqual((state["players"][1]["hp"], state["players"][1]["shield"]), (0, 6))
        self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 0, "turns": 0})
        records[1] = (2, 1, 1, "poison", "card=2;value=4;duration=2")
        state = replay.reconstruct(_turn_end_trace(cards, records, 2, 0, "burn"), cards, "burn-match")
        self.assertEqual(state["players"][0]["statuses"]["poison"], {"value": 4, "turns": 2})
        self.assertEqual(state["players"][0]["hp"], 30)

    def test_burn_after_damage_clamps_instead_of_going_negative(self) -> None:
        cards = _burn_cards(30)
        cards[2] = {"cost": 1, "effect": "damage", "value": 8, "duration": 0}
        records = [
            (1, 1, 0, "damage", "card=2;value=8"),
            (2, 1, 1, "end_turn", ""),
            (3, 2, 0, "burn", "card=1;value=30;duration=2"),
            (4, 2, 1, "end_turn", ""),
            (4, 0, 1, "status_tick", "phase=end;status=burn;value=30;remaining=1;hp=0"),
        ]
        state = replay.reconstruct(_turn_end_trace(cards, records, 4, 0, "burn"), cards, "burn-match")
        self.assertEqual(state["players"][1]["hp"], 0)

    def test_direct_damage_lethal_preempts_outgoing_burn(self) -> None:
        cards = _burn_cards()
        cards[2] = {"cost": 2, "effect": "damage", "value": 30, "duration": 0}
        records = [
            (1, 1, 0, "burn", "card=1;value=4;duration=2"),
            (2, 1, 1, "damage", "card=2;value=30"),
        ]
        events = _turn_end_trace(cards, records, 2, 1, "")
        state = replay.reconstruct(events, cards, "burn-match")
        self.assertEqual((state["winner"], state["turn_id"]), (1, 2))
        self.assertEqual(state["players"][1]["hp"], 30)
        self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 4, "turns": 2})
        events.insert(-1, _event(4, 2, 0, 1, "battle_event", _payload(
            "burn-match", 2, 1, 0, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26")))
        with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
            replay.reconstruct(events, cards, "burn-match")

    def test_final_turn_burn_runs_before_cutoff_without_next_start_ticks(self) -> None:
        for value, expected_turn, hp in ((4, 41, 26), (30, 40, 0)):
            with self.subTest(value=value):
                cards, events = _final_turn_burn_replay(value)
                state = replay.reconstruct(events, cards, "burn-match")
                self.assertEqual((state["winner"], state["turn_id"]), (0, expected_turn))
                self.assertEqual(state["players"][1]["hp"], hp)
                self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": value, "turns": 1})
                self.assertEqual(state["players"][0]["statuses"]["poison"], {"value": 4, "turns": 2})
                self.assertEqual(state["players"][0]["hp"], 30)
                missing_tick = events[:-2] + events[-1:]
                with self.assertRaisesRegex(replay.ReplayError, "missing status tick"):
                    replay.reconstruct(missing_tick, cards, "burn-match")

    def test_timeout_and_reconnect_do_not_generate_end_ticks(self) -> None:
        records = [(1, 1, 0, "burn", "card=1;value=4;duration=2")]
        for reason in ("turn_timeout", "reconnect_timeout"):
            with self.subTest(reason=reason):
                events = _turn_end_trace(_burn_cards(), records, 2, 0, reason)
                state = replay.reconstruct(events, _burn_cards(), "burn-match")
                self.assertEqual(state["players"][1]["hp"], 30)
                self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 4, "turns": 2})
                events.insert(-1, _event(3, 2, 0, 1, "battle_event", _payload(
                    "burn-match", 2, 1, 0, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26")))
                with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
                    replay.reconstruct(events, _burn_cards(), "burn-match")

    def test_rejects_duplicate_action_without_extra_burn_tick(self) -> None:
        events = _burn_replay()
        events.insert(4, events[2])
        with self.assertRaises(replay.ReplayError):
            replay.reconstruct(events, _burn_cards(), "burn-match")

    def test_rejects_invalid_burn_terminal_reason_winner_and_turn(self) -> None:
        for original, replacement in (
                ("reason=burn", "reason=poison"), ("reason=burn", "reason="),
                ("reason=burn", "reason=turn_timeout"), ("reason=burn", "reason=max_turns"),
                ("winner=0", "winner=1"), ("winner=0", "winner=-1"),
                ("turn_id=40", "turn_id=41")):
            with self.subTest(replacement=replacement):
                cards, events = _final_turn_burn_replay(30)
                events[-1].payload = events[-1].payload.replace(original, replacement)
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "burn-match")
        cards = _burn_cards()
        cards[2] = {"cost": 2, "effect": "damage", "value": 30, "duration": 0}
        records = [(1, 1, 0, "burn", "card=1;value=4;duration=2"),
                   (2, 1, 1, "damage", "card=2;value=30")]
        with self.assertRaisesRegex(replay.ReplayError, "burn result is not a lethal win"):
            replay.reconstruct(_turn_end_trace(cards, records, 2, 1, "burn"), cards, "burn-match")

    def test_rejects_burn_and_new_rule_version_in_old_rules(self) -> None:
        for rules in ("legacy", "status_v1", "discard_v1", "turn_end_v2"):
            with self.subTest(rules=rules):
                events = _burn_replay()
                events[0].payload = ("match_id=burn-match" if rules == "legacy" else
                                     events[0].payload.replace("turn_end_v1", rules))
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, _burn_cards(), "burn-match")
        for rules in ("legacy", "status_v1", "discard_v1"):
            with self.subTest(tick_rules=rules):
                cards = _status_cards()
                events = _turn_end_trace(cards, [
                    (1, 1, 0, "end_turn", ""),
                    (1, 0, 0, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26"),
                ], 2, 0, "turn_timeout")
                events[0].payload = ("match_id=burn-match" if rules == "legacy" else
                                     events[0].payload.replace("turn_end_v1", rules))
                with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
                    replay.reconstruct(events, cards, "burn-match")

    def test_old_effects_are_preserved_under_turn_end_rules(self) -> None:
        events = _status_replay()
        events[0].payload = events[0].payload.replace("status_v1", "turn_end_v1")
        state = replay.reconstruct(events, _status_cards(), "status-match")
        self.assertEqual(state["players"][1]["hp"], 24)
        self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 0, "turns": 0})
        events = _discard_replay()
        events[0].payload = events[0].payload.replace("discard_v1", "turn_end_v1")
        state = replay.reconstruct(events, _discard_cards(), "discard-match")
        self.assertEqual(state["players"][1]["discard"], [1, 2, 3])
        cards, events = _poison_lethal_replay()
        events[0].payload = events[0].payload.replace("status_v1", "turn_end_v1")
        state = replay.reconstruct(events, cards, "poison-lethal")
        self.assertEqual((state["winner"], state["reason"]), (1, "poison"))

    def test_old_rules_json_keeps_only_original_status_keys(self) -> None:
        for cards, events, match_id in (
                (_status_cards(), _status_replay(), "status-match"),
                (_discard_cards(), _discard_replay(), "discard-match"),
                (self.cards, _max_turn_replay(), "max-turn-match")):
            with self.subTest(match_id=match_id):
                state = replay.reconstruct(events, cards, match_id)
                for player in state["players"]:
                    self.assertEqual(set(player["statuses"]), {"poison", "regen"})
                    self.assertEqual(set(player), {"hp", "energy", "shield", "hand",
                                                   "deck", "discard", "statuses"})

    def test_burn_trace_round_trips_format_digest_and_cli(self) -> None:
        cards, events = _cross_phase_replay()
        path = self.directory / "burn.replay"
        card_path = self.directory / "burn.csv"
        card_path.write_text("id,name,cost,effect,value,duration\n" + "".join(
            f"{card_id},Card{card_id},{card['cost']},{card['effect']},{card['value']},{card['duration']}\n"
            for card_id, card in sorted(cards.items())), encoding="utf-8")
        expected = _write(path, "burn-match", 2026, events)
        match_id, seed, loaded_digest, loaded = replay.load_replay(path)
        self.assertEqual((seed, loaded_digest, loaded), (2026, expected, events))
        state = replay.reconstruct(loaded, replay.load_cards(card_path), match_id)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "replay" / "replay_battle.py"),
             str(path), "--cards", str(card_path)], check=True, capture_output=True, text=True)
        decoded = json.loads(result.stdout)
        self.assertTrue(decoded["verified"])
        self.assertEqual(decoded["players"], state["players"])
        self.assertEqual(decoded["rules"], "turn_end_v1")

    def test_burn_catalog_bounds_and_duration_validation(self) -> None:
        path = self.directory / "burn.csv"
        for value, duration in ((1, 1), (30, 5), (4, 2), (0, 2), (31, 2),
                                (4, 0), (4, 6), (4, -1)):
            with self.subTest(value=value, duration=duration):
                path.write_text(f"id,name,cost,effect,value,duration\n1,A,1,burn,{value},{duration}\n"
                                "2,H,1,heal,4,0\n3,S,1,shield,6,0\n", encoding="utf-8")
                if 1 <= value <= 30 and 1 <= duration <= 5:
                    self.assertEqual(replay.load_cards(path)[1], {
                        "cost": 1, "effect": "burn", "value": value, "duration": duration})
                else:
                    with self.assertRaisesRegex(replay.ReplayError, "invalid status parameters"):
                        replay.load_cards(path)
        path.write_text("id,name,cost,effect,value\n1,A,1,burn,4\n2,H,1,heal,4\n3,S,1,shield,6\n",
                        encoding="utf-8")
        with self.assertRaisesRegex(replay.ReplayError, "invalid status parameters"):
            replay.load_cards(path)

    def test_bonus_refresh_and_independent_charge_slots(self) -> None:
        cards = _bonus_cards()
        actions = [(1, None), (None, None), (2, None), (None, None), (3, 3),
                   (None, None), (1, None), (None, None), (4, 4),
                   (None, None), (3, 3)]
        for count, attack_uses, heal_uses, opponent_hp in (
                (3, 2, 2, 30), (5, 1, 2, 22), (7, 2, 2, 22),
                (9, 2, 1, 22), (11, 1, 1, 14)):
            with self.subTest(count=count):
                state = replay.reconstruct(_bonus_action_trace(cards, actions[:count]),
                                           cards, "bonus-match")
                self.assertEqual(state["players"][0]["boosts"], {
                    "attack_boost": {"value": 3, "uses": attack_uses},
                    "heal_boost": {"value": 4, "uses": heal_uses}})
                self.assertEqual(state["players"][1]["boosts"], {
                    "attack_boost": {"value": 0, "uses": 0},
                    "heal_boost": {"value": 0, "uses": 0}})
                self.assertEqual(state["players"][1]["hp"], opponent_hp)
        cards = {
            1: {"cost": 1, "effect": "attack_boost", "value": 2, "duration": 2},
            2: {"cost": 1, "effect": "attack_boost", "value": 7, "duration": 1},
            3: {"cost": 1, "effect": "damage", "value": 0, "duration": 0},
        }
        actions = [(1, None), (None, None), (2, None), (None, None), (3, 7),
                   (None, None), (3, 0)]
        refreshed = replay.reconstruct(_bonus_action_trace(cards, actions[:3]),
                                       cards, "bonus-match")
        self.assertEqual(refreshed["players"][0]["boosts"]["attack_boost"],
                         {"value": 7, "uses": 1})
        for count in (5, 7):
            state = replay.reconstruct(_bonus_action_trace(cards, actions[:count]),
                                       cards, "bonus-match")
            self.assertEqual(state["players"][1]["hp"], 23)
            self.assertEqual(state["players"][0]["boosts"]["attack_boost"],
                             {"value": 0, "uses": 0})

    def test_bonus_capped_and_full_hp_heals_consume_once(self) -> None:
        cards = {
            1: {"cost": 1, "effect": "heal_boost", "value": 8, "duration": 2},
            2: {"cost": 1, "effect": "damage", "value": 5, "duration": 0},
            3: {"cost": 1, "effect": "heal", "value": 6, "duration": 0},
        }
        actions = [(1, None), (2, 0), (3, 8), (None, None),
                   (2, 0), (None, None), (3, 8)]
        for count, remaining, opponent_hp in ((3, 1, 30), (7, 0, 25)):
            with self.subTest(count=count):
                state = replay.reconstruct(_bonus_action_trace(cards, actions[:count]),
                                           cards, "bonus-match")
                self.assertEqual(state["players"][0]["hp"], 30)
                self.assertEqual(state["players"][1]["hp"], opponent_hp)
                self.assertEqual(state["players"][0]["boosts"]["heal_boost"],
                                 {"value": 8 if remaining else 0, "uses": remaining})

    def test_bonus_shield_absorbs_combined_damage_but_consumes_charge(self) -> None:
        cards = {
            1: {"cost": 1, "effect": "attack_boost", "value": 3, "duration": 1},
            2: {"cost": 1, "effect": "damage", "value": 5, "duration": 0},
            3: {"cost": 1, "effect": "shield", "value": 20, "duration": 0},
        }
        state = replay.reconstruct(
            _bonus_action_trace(cards, [(1, None), (3, None), (2, 3)]), cards, "bonus-match")
        self.assertEqual((state["players"][1]["hp"], state["players"][1]["shield"]), (30, 12))
        self.assertEqual(state["players"][0]["boosts"]["attack_boost"], {"value": 0, "uses": 0})

    def test_bonus_non_direct_actions_and_status_ticks_preserve_charges(self) -> None:
        for effect in ("shield", "draw"):
            with self.subTest(effect=effect):
                cards = _bonus_cards()
                del cards[4]
                cards[3] = {"cost": 1, "effect": effect, "value": 0, "duration": 0}
                actions = [(1, None), (None, None), (2, None), (None, None),
                           (3, None), (None, None), (None, None)]
                state = replay.reconstruct(_bonus_action_trace(cards, actions), cards, "bonus-match")
                self.assertEqual(state["players"][0]["boosts"], {
                    "attack_boost": {"value": 3, "uses": 2},
                    "heal_boost": {"value": 4, "uses": 2}})
        cards = _bonus_cards()
        del cards[4]
        cards[2] = {"cost": 1, "effect": "regen", "value": 2, "duration": 2}
        cards[3] = {"cost": 1, "effect": "burn", "value": 3, "duration": 2}
        records = [
            (1, 1, 0, "attack_boost", "card=1;value=3;uses=2"),
            (2, 1, 1, "burn", "card=3;value=3;duration=2"),
            (3, 2, 0, "regen", "card=2;value=2;duration=2"),
            (3, 0, 0, "status_tick", "phase=end;status=burn;value=3;remaining=1;hp=27"),
            (4, 2, 1, "end_turn", ""),
            (5, 0, 0, "status_tick", "status=regen;value=2;remaining=1;hp=29"),
            (5, 3, 0, "end_turn", ""),
            (5, 0, 0, "status_tick", "phase=end;status=burn;value=3;remaining=0;hp=26"),
            (6, 3, 1, "end_turn", ""),
            (7, 0, 0, "status_tick", "status=regen;value=2;remaining=0;hp=28"),
        ]
        state = replay.reconstruct(_bonus_trace(cards, records, 7, 1), cards, "bonus-match")
        self.assertEqual(state["players"][0]["hp"], 28)
        self.assertEqual(state["players"][0]["boosts"]["attack_boost"], {"value": 3, "uses": 2})
        cards = _bonus_cards()
        del cards[4]
        cards[3] = {"cost": 1, "effect": "poison", "value": 2, "duration": 1}
        records = [
            (1, 1, 0, "attack_boost", "card=1;value=3;uses=2"),
            (2, 1, 1, "poison", "card=3;value=2;duration=1"),
            (3, 0, 0, "status_tick", "status=poison;value=2;remaining=0;hp=28"),
            (3, 2, 0, "heal_boost", "card=2;value=4;uses=2"),
            (4, 2, 1, "end_turn", ""),
            (5, 3, 0, "end_turn", ""),
        ]
        state = replay.reconstruct(_bonus_trace(cards, records, 6, 0), cards, "bonus-match")
        self.assertEqual(state["players"][0]["hp"], 28)
        self.assertEqual(state["players"][0]["boosts"], {
            "attack_boost": {"value": 3, "uses": 2}, "heal_boost": {"value": 4, "uses": 2}})

    def test_bonus_rules_preserve_prior_effects_and_burn_order(self) -> None:
        for cards, events, match_id, original_rules, expected_hp in (
                (_status_cards(), _status_replay(), "status-match", "status_v1", 24),
                (_discard_cards(), _discard_replay(), "discard-match", "discard_v1", 30),
                (_burn_cards(), _burn_replay(), "burn-match", "turn_end_v1", 22)):
            with self.subTest(match_id=match_id):
                old_state = replay.reconstruct(events, cards, match_id)
                events[0].payload = events[0].payload.replace(original_rules, "bonus_v1")
                new_state = replay.reconstruct(events, cards, match_id)
                self.assertEqual(new_state["rules"], "bonus_v1")
                self.assertEqual(new_state["players"][1]["hp"], expected_hp)
                for old_player, new_player in zip(old_state["players"], new_state["players"]):
                    self.assertEqual(new_player["boosts"], {
                        "attack_boost": {"value": 0, "uses": 0},
                        "heal_boost": {"value": 0, "uses": 0}})
                    new_player.pop("boosts")
                    if original_rules != "turn_end_v1":
                        new_player["statuses"].pop("burn")
                    self.assertEqual(new_player, old_player)
        cards, events = _cross_phase_replay()
        events[0].payload = events[0].payload.replace("turn_end_v1", "bonus_v1")
        state = replay.reconstruct(events, cards, "burn-match")
        self.assertEqual((state["players"][0]["hp"], state["players"][1]["hp"]), (26, 22))
        cards, events = _poison_lethal_replay()
        events[0].payload = events[0].payload.replace("status_v1", "bonus_v1")
        self.assertEqual(replay.reconstruct(events, cards, "poison-lethal")["reason"], "poison")

    def test_bonus_rejects_missing_negative_wrong_or_extra_action_fields(self) -> None:
        cards = _bonus_cards()
        for replacement in ("", ";bonus=-1", ";bonus=4", ";bonus=0", ";bonus=03",
                            ";bonus=invalid", ";bonus=3;target=1", ";bonus=3;uses=1",
                            ";bonus=3;duration=1", ";bonus=3;extra=0", ";bonus=3;bonus=3"):
            with self.subTest(replacement=replacement):
                events = _bonus_action_trace(cards, [(1, None), (None, None), (3, 3)])
                events[3].payload = events[3].payload.replace(";bonus=3", replacement)
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "bonus-match")
        for effect in ("damage", "heal"):
            for replacement in ("", ";bonus=1", ";bonus=-1"):
                with self.subTest(effect=effect, replacement=replacement):
                    cards = _bonus_cards()
                    cards[3]["effect"] = effect
                    events = _bonus_action_trace(cards, [(3, 0)])
                    events[1].payload = events[1].payload.replace(";bonus=0", replacement)
                    with self.assertRaises(replay.ReplayError):
                        replay.reconstruct(events, cards, "bonus-match")

    def test_bonus_rejects_wrong_uses_target_duration_and_boost_bounds(self) -> None:
        for kind in ("attack_boost", "heal_boost"):
            for replacement in ("", ";uses=1", ";uses=0", ";uses=6", ";uses=-1",
                                ";uses=2;target=0", ";uses=2;target=1", ";duration=2",
                                ";uses=2;duration=2", ";uses=2;bonus=0", ";uses=2;extra=0"):
                with self.subTest(kind=kind, replacement=replacement):
                    cards = _bonus_cards()
                    cards[1]["effect"] = kind
                    events = _bonus_action_trace(cards, [(1, None)])
                    events[1].payload = events[1].payload.replace(";uses=2", replacement)
                    with self.assertRaises(replay.ReplayError):
                        replay.reconstruct(events, cards, "bonus-match")
            for value, uses in ((0, 2), (11, 2), (3, 0), (3, 6), (3, -1)):
                with self.subTest(kind=kind, value=value, uses=uses):
                    cards = _bonus_cards()
                    cards[1].update(effect=kind, value=value, duration=uses)
                    with self.assertRaisesRegex(replay.ReplayError, "boost card does not match catalog"):
                        replay.reconstruct(_bonus_action_trace(cards, [(1, None)]), cards, "bonus-match")

    def test_bonus_catalog_charge_bounds_and_six_column_format(self) -> None:
        path = self.directory / "boost.csv"
        for kind in ("attack_boost", "heal_boost"):
            for value, uses in ((1, 1), (10, 5), (3, 2), (0, 2), (11, 2),
                                (3, 0), (3, 6), (3, -1)):
                with self.subTest(kind=kind, value=value, uses=uses):
                    path.write_text(f"id,name,cost,effect,value,duration\n1,A,1,{kind},{value},{uses}\n"
                                    "2,H,1,heal,4,0\n3,S,1,shield,6,0\n", encoding="utf-8")
                    if 1 <= value <= 10 and 1 <= uses <= 5:
                        self.assertEqual(replay.load_cards(path)[1], {
                            "cost": 1, "effect": kind, "value": value, "duration": uses})
                    else:
                        with self.assertRaisesRegex(replay.ReplayError, "invalid boost parameters"):
                            replay.load_cards(path)
            path.write_text(f"id,name,cost,effect,value\n1,A,1,{kind},3\n"
                            "2,H,1,heal,4\n3,S,1,shield,6\n", encoding="utf-8")
            with self.assertRaisesRegex(replay.ReplayError, "invalid boost parameters"):
                replay.load_cards(path)
        self.assertEqual(set(self.cards), {1, 2, 3, 7, 8, 9, 10, 11, 12})

    def test_bonus_lethal_damage_preempts_outgoing_burn(self) -> None:
        cards = {
            1: {"cost": 1, "effect": "attack_boost", "value": 5, "duration": 1},
            2: {"cost": 1, "effect": "damage", "value": 25, "duration": 0},
            3: {"cost": 1, "effect": "burn", "value": 4, "duration": 2},
        }
        records = [
            (1, 1, 0, "burn", "card=3;value=4;duration=2"),
            (2, 1, 1, "attack_boost", "card=1;value=5;uses=1"),
            (2, 0, 1, "status_tick", "phase=end;status=burn;value=4;remaining=1;hp=26"),
            (3, 2, 0, "end_turn", ""),
            (4, 2, 1, "damage", "card=2;value=25;bonus=5"),
        ]
        events = _bonus_trace(cards, records, 4, 1, "")
        state = replay.reconstruct(events, cards, "bonus-match")
        self.assertEqual((state["winner"], state["turn_id"], state["reason"]), (1, 4, ""))
        self.assertEqual(state["players"][0]["hp"], 0)
        self.assertEqual(state["players"][1]["hp"], 26)
        self.assertEqual(state["players"][1]["boosts"]["attack_boost"], {"value": 0, "uses": 0})
        self.assertEqual(state["players"][1]["statuses"]["burn"], {"value": 4, "turns": 1})
        events.insert(-1, _event(len(events), 4, 0, 1, "battle_event", _payload(
            "bonus-match", 4, 1, 0, "status_tick",
            "phase=end;status=burn;value=4;remaining=0;hp=22")))
        with self.assertRaisesRegex(replay.ReplayError, "unexpected status tick"):
            replay.reconstruct(events, cards, "bonus-match")

    def test_bonus_rejects_duplicate_actions_and_separate_consumption_events(self) -> None:
        cards = _bonus_cards()
        for position in (1, 3):
            with self.subTest(position=position):
                events = _bonus_action_trace(cards, [(1, None), (None, None), (3, 3)])
                events.insert(position + 1, events[position])
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "bonus-match")
        for kind in ("bonus_used", "action_ack", "rejected"):
            with self.subTest(kind=kind):
                events = _bonus_action_trace(cards, [(1, None)])
                events.insert(-1, _event(3, 2, 0, 1, "battle_event",
                                         _payload("bonus-match", 2, 1, 0, kind)))
                with self.assertRaises(replay.ReplayError):
                    replay.reconstruct(events, cards, "bonus-match")
        cards[3]["cost"] = 4
        with self.assertRaisesRegex(replay.ReplayError, "illegal card action"):
            replay.reconstruct(_bonus_action_trace(cards, [(1, None), (None, None), (3, 3)]),
                               cards, "bonus-match")

    def test_old_rules_reject_boost_effects_and_bonus_fields(self) -> None:
        for rules in ("legacy", "status_v1", "discard_v1", "turn_end_v1"):
            for actions in ([(1, None)], [(2, None)], [(3, 0)]):
                with self.subTest(rules=rules, actions=actions):
                    cards = _bonus_cards()
                    events = _bonus_action_trace(cards, actions)
                    events[0].payload = ("match_id=bonus-match" if rules == "legacy" else
                                         events[0].payload.replace("bonus_v1", rules))
                    with self.assertRaises(replay.ReplayError):
                        replay.reconstruct(events, cards, "bonus-match")
        cards = _bonus_cards()
        del cards[4]
        cards[3] = {"cost": 1, "effect": "shield", "value": 0, "duration": 0}
        for actions in ([(None, None)], [(3, None)]):
            events = _bonus_action_trace(cards, actions)
            events[1].payload += ";bonus=0"
            with self.assertRaises(replay.ReplayError):
                replay.reconstruct(events, cards, "bonus-match")
        old_state = replay.reconstruct(_max_turn_replay(), self.cards, "max-turn-match")
        self.assertEqual(old_state, {
            "match_id": "max-turn-match", "winner": -1, "turn_id": 41,
            "reason": "max_turns", "rules": "legacy", "players": [{
                "hp": 30, "energy": 3, "shield": 0, "hand": [1, 2, 3],
                "deck": [1, 2, 3] * 3, "discard": [],
                "statuses": {"poison": {"value": 0, "turns": 0},
                             "regen": {"value": 0, "turns": 0}}} for _ in range(2)]})

    def test_bonus_trace_round_trips_format_digest_and_cli(self) -> None:
        cards = _bonus_cards()
        actions = [(1, None), (None, None), (2, None), (None, None), (3, 3)]
        events = _bonus_action_trace(cards, actions)
        path = self.directory / "bonus.replay"
        card_path = self.directory / "bonus.csv"
        card_path.write_text("id,name,cost,effect,value,duration\n" + "".join(
            f"{card_id},Card{card_id},{card['cost']},{card['effect']},{card['value']},{card['duration']}\n"
            for card_id, card in sorted(cards.items())), encoding="utf-8")
        expected = _write(path, "bonus-match", 2026, events)
        match_id, seed, loaded_digest, loaded = replay.load_replay(path)
        self.assertEqual((seed, loaded_digest, loaded), (2026, expected, events))
        state = replay.reconstruct(loaded, replay.load_cards(card_path), match_id)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "replay" / "replay_battle.py"),
             str(path), "--cards", str(card_path)], check=True, capture_output=True, text=True)
        decoded = json.loads(result.stdout)
        self.assertTrue(decoded["verified"])
        self.assertEqual(decoded["players"], state["players"])
        self.assertEqual(decoded["rules"], "bonus_v1")
        self.assertEqual(decoded["revision"], len(events))
        self.assertEqual(decoded["digest"], str(expected))
        events[5].payload = events[5].payload.replace("bonus=3", "bonus=4")
        _write(path, "bonus-match", 2026, events)
        with self.assertRaisesRegex(replay.ReplayError, "inconsistent action bonus"):
            replay.reconstruct(replay.load_replay(path)[3], cards, match_id)



if __name__ == "__main__":
    unittest.main()
