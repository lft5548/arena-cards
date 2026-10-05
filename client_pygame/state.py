"""Authoritative view model shared by the interactive client and bots.

The model never simulates a card effect. Only snapshots change visible battle
state. A pending command is released by a newer snapshot, an error or a result.
"""
from __future__ import annotations

from copy import deepcopy
import threading
import time

try:
    from .protocol import payload_int
except ImportError:  # direct script imports used by tools/bot
    from protocol import payload_int

CARDS = {
    1: {"name": "Strike", "cost": 2, "text": "Deal 8 damage", "kind": "attack"},
    2: {"name": "Mend", "cost": 1, "text": "Restore 4 HP", "kind": "heal"},
    3: {"name": "Barrier", "cost": 1, "text": "Gain 5 shield", "kind": "shield"},
    4: {"name": "Insight", "cost": 1, "text": "Draw 2 cards", "kind": "draw"},
    5: {"name": "Heavy", "cost": 3, "text": "Deal 10 damage", "kind": "attack"},
    6: {"name": "Spark", "cost": 0, "text": "Deal 2 damage", "kind": "attack"},
    7: {"name": "Venom", "cost": 1, "text": "Poison 3 HP for 2 starts", "kind": "poison"},
    8: {"name": "Renew", "cost": 1, "text": "Regen 3 HP for 2 starts", "kind": "regen"},
    9: {"name": "Disrupt", "cost": 2, "text": "Discard opponent first 2 cards", "kind": "discard"},
    10: {"name": "Ember", "cost": 1, "text": "Burn 3 HP for 2 ends", "kind": "burn"},
    11: {"name": "Focus", "cost": 1, "text": "Direct attacks +2 for 2 uses", "kind": "attack_boost"},
    12: {"name": "Bless", "cost": 1, "text": "Direct heals +2 for 2 uses", "kind": "heal_boost"},
}


class ClientState:
    def __init__(self):
        self.lock = threading.RLock()
        self.connected = False
        self.logged_in = False
        self.queued = False
        self.user = ""
        self.session_token = ""
        self.match_id = ""
        self.player_index = -1
        self.snapshot = {}
        self.result = None
        self.pending = None
        self.last_error = ""
        self.action_id = 0
        self.received_at = 0.0

    def apply(self, event):
        """Apply a decoded message. Returns False for stale battle messages."""
        typ, data = event.get("type"), event.get("payload", {})
        with self.lock:
            if typ == "Connected":
                self.connected = True
                self.last_error = ""
            elif typ == "Disconnected":
                # A transport loss is recoverable while the server keeps the
                # room alive. Only the final event expires the local session.
                reconnecting = str(data.get("reconnecting", "0")) == "1"
                self.connected = False
                self.last_error = data.get("error", "Connection closed")
                if not reconnecting:
                    self.logged_in = self.queued = False
                    self.pending = None
                    self.match_id = ""
                    self.player_index = -1
                    self.snapshot = {}
                    self.result = None
                    self.session_token = ""
            elif typ == "LoginResp":
                self.logged_in = str(data.get("ok")) == "1"
                self.user = data.get("user", "")
                if self.logged_in:
                    self.session_token = str(data.get("token", ""))
                    self.last_error = ""
                else:
                    self.last_error = str(data.get("error", "login_failed"))
            elif typ == "ReconnectResp":
                self.logged_in = str(data.get("ok")) == "1"
                if self.logged_in:
                    match_id = str(data.get("match_id", ""))
                    if match_id:
                        self.match_id = match_id
                    self.player_index = payload_int(data, "player_index", self.player_index)
                    self.last_error = ""
                else:
                    self.last_error = str(data.get("error", "reconnect_failed"))
            elif typ == "ActionAck":
                if self.pending and payload_int(data, "action_id", -1) == self.pending[2]:
                    self.pending = None
            elif typ == "MatchJoinReq":
                self.queued = str(data.get("queued")) == "1" and not self.active()
            elif typ == "MatchCancelReq":
                self.queued = False
            elif typ == "MatchFound":
                if not data.get("match_id"):
                    self.last_error = "Malformed MatchFound"
                    return False
                self.match_id = str(data["match_id"])
                self.player_index = payload_int(data, "player_index", -1)
                self.snapshot = {}
                self.result = self.pending = None
                self.queued = False
                self.last_error = ""
            elif typ in ("BattleSnapshot", "BattleEvent", "MatchResult"):
                if str(data.get("match_id")) != self.match_id:
                    return False
                if typ == "BattleSnapshot":
                    self.player_index = payload_int(data, "player_index", self.player_index)
                    revision = payload_int(data, "revision", -1)
                    if self.snapshot and revision <= self.snapshot["revision"]:
                        return False
                    if self.result is not None:
                        return False
                    snapshot = {key: payload_int(data, key) for key in (
                        "turn", "turn_id", "revision", "remaining_ms", "done",
                        "p0_hp", "p1_hp", "p0_energy", "p1_energy", "p0_shield", "p1_shield",
                        "p0_poison_value", "p0_poison_turns", "p0_regen_value", "p0_regen_turns",
                        "p1_poison_value", "p1_poison_turns", "p1_regen_value", "p1_regen_turns",
                        "p0_burn_value", "p0_burn_turns", "p1_burn_value", "p1_burn_turns",
                        "p0_attack_boost_value", "p0_attack_boost_uses", "p1_attack_boost_value", "p1_attack_boost_uses",
                        "p0_heal_boost_value", "p0_heal_boost_uses", "p1_heal_boost_value", "p1_heal_boost_uses",
                        "opponent_hand_count", "deck_count", "discard_count", "opponent_discard_count", "last_action_id")}
                    snapshot["hand"] = [int(card) for card in str(data.get("hand", "")).split(",") if card]
                    snapshot["discard"] = [int(card) for card in str(data.get("discard", "")).split(",") if card]
                    self.snapshot = snapshot
                    self.action_id = max(self.action_id, snapshot["last_action_id"])
                    self.received_at = time.monotonic()
                    self.pending = None
                    self.last_error = ""
                elif typ == "MatchResult":
                    self.result = deepcopy(data)
                    self.pending = None
                    self.queued = False
                    # The server removes a room's resume token as soon as it
                    # broadcasts the final result. Do not retry that token if
                    # the transport drops after the result has been shown.
                    self.session_token = ""
            elif typ == "Error":
                self.last_error = str(data.get("code", "unknown_error"))
                self.pending = None
                # A failed queue request must be retryable.
                if not self.active():
                    self.queued = False
            return True

    def active(self):
        return bool(self.match_id) and self.result is None

    def view(self):
        with self.lock:
            return deepcopy({key: value for key, value in self.__dict__.items() if key != "lock"})

    def clear_session_token(self):
        with self.lock:
            self.session_token = ""

    def reserve_action(self, slot=None):
        """Reserve an action using the latest snapshot; None means end turn."""
        with self.lock:
            if not self.connected or not self.logged_in:
                raise ValueError("Wait for login")
            if not self.snapshot or self.result or self.snapshot["done"]:
                raise ValueError("No active battle")
            if self.snapshot["turn"] != self.player_index:
                raise ValueError("Wait for your turn")
            if self.pending:
                raise ValueError("Waiting for server acknowledgement")
            data = {"match_id": self.match_id, "turn_id": self.snapshot["turn_id"]}
            typ = "EndTurnReq" if slot is None else "PlayCardReq"
            if slot is not None:
                hand = self.snapshot["hand"]
                if not 0 <= slot < len(hand):
                    raise ValueError("Select a card in your hand")
                card = CARDS.get(hand[slot])
                if not card:
                    raise ValueError("Unknown card")
                if card["cost"] > self.snapshot[f"p{self.player_index}_energy"]:
                    raise ValueError("Not enough energy")
                data["card"] = hand[slot]
            self.action_id += 1
            data["action_id"] = self.action_id
            self.pending = (self.match_id, self.snapshot["revision"], self.action_id)
            return typ, data

    def reserve_queue(self):
        with self.lock:
            if not self.connected or not self.logged_in:
                raise ValueError("Wait for login")
            if self.active() or self.queued:
                raise ValueError("Already in a battle or matchmaking")
            self.queued = True


def choose_card(snapshot, player_index):
    """Prefer affordable attacks, then useful support; None ends the turn."""
    energy = snapshot[f"p{player_index}_energy"]
    choices = []
    for slot, card_id in enumerate(snapshot["hand"]):
        card = CARDS.get(card_id)
        if not card or card["cost"] > energy:
            continue
        if card["kind"] in ("heal", "regen") and snapshot[f"p{player_index}_hp"] >= 30:
            continue
        if card["kind"] in ("attack_boost", "heal_boost") and snapshot.get(f"p{player_index}_{card['kind']}_uses", 0) > 0:
            continue
        priority = {6: 0, 1: 1, 5: 2, 7: 2, 10: 2, 11: 2, 12: 3, 4: 3, 9: 3, 2: 4, 8: 4, 3: 5}.get(card_id, 9)
        choices.append((priority, slot))
    return min(choices)[1] if choices else None
