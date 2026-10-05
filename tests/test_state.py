import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client_pygame"))
from state import ClientState, choose_card


class ClientStateTest(unittest.TestCase):
    def setUp(self):
        self.state = ClientState()
        self.state.apply({"type": "Connected", "payload": {}})
        self.state.apply({"type": "LoginResp", "payload": {"ok": "1", "user": "alice"}})
        self.state.apply({"type": "MatchFound", "payload": {"match_id": "match-1"}})
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "1",
            "revision": "1", "remaining_ms": "30000", "done": "0",
            "p0_hp": "30", "p1_hp": "30", "p0_energy": "3", "p1_energy": "3",
            "p0_shield": "0", "p1_shield": "0", "hand": "1,2,3",
            "opponent_hand_count": "3", "deck_count": "6", "discard_count": "0"}})

    def test_reserves_server_validated_card_action(self):
        message_type, payload = self.state.reserve_action(0)
        self.assertEqual(message_type, "PlayCardReq")
        self.assertEqual(payload["match_id"], "match-1")
        self.assertEqual(payload["turn_id"], 1)
        self.assertEqual(payload["card"], 1)
        self.assertGreater(payload["action_id"], 0)

    def test_stale_snapshot_does_not_replace_state(self):
        accepted = self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "1", "turn_id": "0",
            "revision": "1", "remaining_ms": "1", "done": "0"}})
        self.assertFalse(accepted)
        self.assertEqual(self.state.view()["snapshot"]["turn"], 0)

    def test_legacy_snapshot_defaults_to_no_statuses(self):
        snapshot = self.state.view()["snapshot"]
        for player in (0, 1):
            for kind in ("poison", "regen"):
                self.assertEqual(snapshot[f"p{player}_{kind}_value"], 0)
                self.assertEqual(snapshot[f"p{player}_{kind}_turns"], 0)

    def test_status_snapshot_remains_authoritative(self):
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "3",
            "revision": "3", "p0_hp": "29", "p0_energy": "3", "hand": "7,8",
            "p0_regen_value": "3", "p0_regen_turns": "2",
            "p1_poison_value": "4", "p1_poison_turns": "1"}})
        self.state.apply({"type": "BattleEvent", "payload": {
            "match_id": "match-1", "type": "status_tick", "player": "0", "hp": "0"}})
        snapshot = self.state.view()["snapshot"]
        self.assertEqual(snapshot["p0_hp"], 29)
        self.assertEqual(snapshot["p0_regen_turns"], 2)
        self.assertEqual(snapshot["p1_poison_value"], 4)
        self.assertEqual(self.state.reserve_action(0)[1]["card"], 7)

    def test_bot_can_select_status_cards(self):
        snapshot = {"p0_energy": 3, "p0_hp": 20, "hand": [8]}
        self.assertEqual(choose_card(snapshot, 0), 0)
        snapshot["p0_hp"] = 30
        self.assertIsNone(choose_card(snapshot, 0))
        snapshot["hand"] = [7]
        self.assertEqual(choose_card(snapshot, 0), 0)

    def test_legacy_snapshot_defaults_to_empty_discard(self):
        snapshot = self.state.view()["snapshot"]
        self.assertEqual(snapshot["discard"], [])
        self.assertEqual(snapshot["opponent_discard_count"], 0)

    def test_discard_snapshot_remains_private_and_authoritative(self):
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "3",
            "revision": "3", "p0_energy": "3", "hand": "9,1",
            "discard": "2,3,2", "discard_count": "3", "opponent_discard_count": "2"}})
        self.state.apply({"type": "BattleEvent", "payload": {
            "match_id": "match-1", "type": "discard", "target": "0", "count": "1"}})
        snapshot = self.state.view()["snapshot"]
        self.assertEqual(snapshot["discard"], [2, 3, 2])
        self.assertEqual(snapshot["discard_count"], 3)
        self.assertEqual(snapshot["opponent_discard_count"], 2)
        self.assertEqual(snapshot["hand"], [9, 1])
        self.assertNotIn("opponent_discard", snapshot)
        self.assertEqual(self.state.reserve_action(0)[1]["card"], 9)

    def test_bot_can_select_discard_card_with_enough_energy(self):
        snapshot = {"p0_energy": 2, "p0_hp": 30, "hand": [9]}
        self.assertEqual(choose_card(snapshot, 0), 0)
        snapshot["p0_energy"] = 1
        self.assertIsNone(choose_card(snapshot, 0))

    def test_action_ack_releases_pending_request(self):
        _, payload = self.state.reserve_action(0)
        accepted = self.state.apply({"type": "ActionAck", "payload": {
            "match_id": payload["match_id"], "turn_id": str(payload["turn_id"]),
            "action_id": str(payload["action_id"]), "status": "applied"}})
        self.assertTrue(accepted)
        self.assertIsNone(self.state.view()["pending"])

    def test_reconnect_snapshot_restores_action_sequence(self):
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "3",
            "revision": "3", "p0_energy": "3", "hand": "1,2", "last_action_id": "7"}})
        self.assertEqual(self.state.reserve_action(0)[1]["action_id"], 8)

    def test_burn_snapshot_is_authoritative_and_legacy_default_is_zero(self):
        self.assertEqual(self.state.view()["snapshot"]["p0_burn_turns"], 0)
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "3",
            "revision": "3", "p0_hp": "26", "p0_energy": "3", "hand": "10",
            "p0_burn_value": "4", "p0_burn_turns": "1"}})
        self.state.apply({"type": "BattleEvent", "payload": {
            "match_id": "match-1", "type": "status_tick", "phase": "end", "status": "burn", "hp": "0"}})
        snapshot = self.state.view()["snapshot"]
        self.assertEqual(snapshot["p0_hp"], 26)
        self.assertEqual(snapshot["p0_burn_turns"], 1)
        self.assertEqual(choose_card(snapshot, 0), 0)
        self.assertEqual(self.state.reserve_action(0)[1]["card"], 10)

    def test_match_result_expires_resume_token(self):
        self.state.apply({"type": "LoginResp", "payload": {
            "ok": "1", "user": "alice", "token": "room-token"}})
        accepted = self.state.apply({"type": "MatchResult", "payload": {
            "match_id": "match-1", "winner": "0", "reason": "knockout"}})
        self.assertTrue(accepted)
        view = self.state.view()
        self.assertEqual(view["result"]["reason"], "knockout")
        self.assertEqual(view["session_token"], "")
        self.assertFalse(self.state.active())

    def test_bonus_snapshot_is_authoritative_and_bot_skips_active_refresh(self):
        self.assertEqual(self.state.view()["snapshot"]["p0_attack_boost_uses"], 0)
        self.state.apply({"type": "BattleSnapshot", "payload": {
            "match_id": "match-1", "player_index": "0", "turn": "0", "turn_id": "3",
            "revision": "3", "p0_hp": "26", "p0_energy": "3", "hand": "11,12",
            "p0_attack_boost_value": "3", "p0_attack_boost_uses": "2"}})
        self.state.apply({"type": "BattleEvent", "payload": {
            "match_id": "match-1", "type": "damage", "bonus": "3"}})
        snapshot = self.state.view()["snapshot"]
        self.assertEqual(snapshot["p0_attack_boost_uses"], 2)
        self.assertEqual(choose_card(snapshot, 0), 1)
        snapshot["p0_heal_boost_uses"] = 1
        self.assertIsNone(choose_card(snapshot, 0))
        snapshot["p0_attack_boost_uses"] = 0
        self.assertEqual(choose_card(snapshot, 0), 0)


if __name__ == "__main__":
    unittest.main()
