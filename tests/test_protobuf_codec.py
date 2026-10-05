import asyncio
import struct
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client_pygame.generated import arena_cards_pb2 as messages
from client_pygame.protobuf_codec import decode_payload, encode_payload
from client_pygame.protocol import (MAX_FRAME, MESSAGE_TYPES, PROTO_MESSAGE_TYPES,
                                    make_message, make_proto_message, negotiate_protocol,
                                    parse_message, read_message, send_message)
from client_pygame.state import ClientState
from tools.bot.bot import Bot, bot_protocol


MAX_UINT64 = (1 << 64) - 1


def snapshot_message(player_index=0):
    message = messages.BattleSnapshot(player_index=player_index)
    message.context.match_id = "match-proto"
    message.context.turn_id = MAX_UINT64
    message.context.action_id = MAX_UINT64 - 1
    message.context.revision = MAX_UINT64 - 2
    message.context.request_id = "snapshot-request"
    state = message.state
    state.turn = 0
    state.snapshot_revision = MAX_UINT64 - 3
    state.remaining_ms = 0
    state.done = False
    state.hand.extend([1, 9, 1])
    state.opponent_hand_count = 2
    state.deck_count = 0
    state.discard.extend([2, 3])
    state.discard_count = 2
    state.opponent_discard_count = 4
    state.last_action_id = MAX_UINT64 - 4
    state.replay.seed = MAX_UINT64
    state.replay.revision = MAX_UINT64 - 1
    state.replay.digest = MAX_UINT64 - 2
    state.replay.valid = True
    first = state.players.add(hp=0, energy=0, shield=0)
    first.poison.value = 3
    first.poison.turns = 2
    second = state.players.add(hp=30, energy=3, shield=5)
    second.regen.value = 3
    second.regen.turns = 1
    return message


class ProtobufCodecTest(unittest.TestCase):
    def test_leaderboard_optional_limit_and_response_integers(self):
        default = messages.LeaderboardRequest.FromString(encode_payload("LeaderboardReq", {"request_id": "rank-default"}))
        self.assertFalse(default.HasField("limit"))
        zero = messages.LeaderboardRequest.FromString(encode_payload("LeaderboardReq", {"limit": 0}))
        self.assertTrue(zero.HasField("limit"))
        self.assertEqual(zero.limit, 0)
        response = messages.LeaderboardResponse(ok=True, source="mysql", request_id="rank-result")
        response.entries.add(rank=1, player_id="rank;player", rating=-(1 << 63), wins=MAX_UINT64, losses=1 << 63)
        values = decode_payload("LeaderboardResp", response.SerializeToString())
        self.assertEqual(values["source"], "mysql")
        self.assertEqual(values["count"], "1")
        self.assertEqual(values["request_id"], "rank-result")
        self.assertEqual(values["entry_0_user"], "rank;player")
        self.assertEqual(values["entry_0_rating"], str(-(1 << 63)))
        self.assertEqual(values["entry_0_wins"], str(MAX_UINT64))
        self.assertEqual(values["entry_0_losses"], str(1 << 63))

    def test_request_strings_and_generated_request_ids(self):
        login = messages.LoginRequest.FromString(encode_payload("LoginReq", "bot-user"))
        self.assertEqual(login.user, "bot-user")
        self.assertTrue(login.request_id)
        reconnect = messages.ReconnectRequest.FromString(encode_payload("ReconnectReq", "token"))
        self.assertEqual(reconnect.session_token, "token")
        self.assertTrue(reconnect.request_id)
        first = messages.MatchJoinRequest.FromString(encode_payload("MatchJoinReq"))
        second = messages.MatchJoinRequest.FromString(encode_payload("MatchJoinReq"))
        self.assertNotEqual(first.request_id, second.request_id)

    def test_supplied_request_id_and_action_uint64(self):
        values = {"match_id": "match-proto", "turn_id": str(MAX_UINT64),
                  "action_id": MAX_UINT64, "revision": str(MAX_UINT64),
                  "request_id": "action-request", "card": 9}
        body = encode_payload("PlayCardReq", values)
        message = messages.PlayCardRequest.FromString(body)
        self.assertEqual(message.context.turn_id, MAX_UINT64)
        self.assertEqual(message.context.action_id, MAX_UINT64)
        self.assertEqual(message.context.revision, MAX_UINT64)
        self.assertEqual(message.context.request_id, "action-request")
        self.assertEqual(message.card_id, 9)
        self.assertEqual(body, encode_payload("PlayCardReq", values))
        self.assertNotIn("card_id", values)
        end_turn = messages.EndTurnRequest.FromString(encode_payload("EndTurnReq", values))
        self.assertEqual(end_turn.context.request_id, "action-request")

    def test_every_client_request(self):
        for message_type, message_class in (("MatchJoinReq", messages.MatchJoinRequest),
                                             ("MatchCancelReq", messages.MatchCancelRequest),
                                             ("Heartbeat", messages.Heartbeat),
                                             ("AdminRoomsReq", messages.AdminRoomsRequest)):
            with self.subTest(message_type=message_type):
                message = message_class.FromString(encode_payload(message_type, {"request_id": "supplied"}))
                self.assertEqual(message.request_id, "supplied")
        message = messages.ReconnectRequest.FromString(encode_payload("ReconnectReq", {"token": "resume"}))
        self.assertEqual(message.session_token, "resume")

    def test_zero_defaults_are_not_replaced_with_missing_values(self):
        message = messages.PlayCardRequest.FromString(encode_payload("PlayCardReq", {
            "turn_id": 0, "action_id": 0, "revision": 0, "card": 0}))
        self.assertEqual(message.context.turn_id, 0)
        self.assertEqual(message.context.action_id, 0)
        self.assertEqual(message.card_id, 0)
        payload = decode_payload("LoginResp", messages.LoginResponse().SerializeToString())
        self.assertEqual(payload["ok"], "0")

    def test_snapshot_uint64_private_lists_and_status_fields(self):
        payload = decode_payload("BattleSnapshot", snapshot_message().SerializeToString())
        self.assertEqual(payload["turn_id"], str(MAX_UINT64))
        self.assertEqual(payload["revision"], str(MAX_UINT64 - 3))
        self.assertEqual(payload["last_action_id"], str(MAX_UINT64 - 4))
        self.assertEqual(payload["replay_seed"], str(MAX_UINT64))
        self.assertEqual(payload["replay_digest"], str(MAX_UINT64 - 2))
        self.assertEqual(payload["hand"], "1,9,1")
        self.assertEqual(payload["discard"], "2,3")
        self.assertEqual(payload["discard_count"], "2")
        self.assertEqual(payload["opponent_discard_count"], "4")
        self.assertNotIn("opponent_hand", payload)
        self.assertNotIn("opponent_discard", payload)
        self.assertNotIn("players", payload)
        self.assertEqual(payload["p0_hp"], "0")
        self.assertEqual(payload["p0_poison_value"], "3")
        self.assertEqual(payload["p0_poison_turns"], "2")
        self.assertEqual(payload["p1_regen_turns"], "1")
        self.assertEqual(payload["done"], "0")
        self.assertTrue(all(isinstance(value, str) for value in payload.values()))

    def test_snapshot_applies_to_authoritative_client_state(self):
        state = ClientState()
        state.apply({"type": "Connected", "payload": {}})
        state.apply({"type": "LoginResp", "payload": {"ok": "1", "user": "proto"}})
        found = messages.MatchFound(player_index=0, turn=0, room=1)
        found.context.match_id = "match-proto"
        state.apply({"type": "MatchFound", "payload": decode_payload("MatchFound", found.SerializeToString())})
        payload = decode_payload("BattleSnapshot", snapshot_message().SerializeToString())
        self.assertTrue(state.apply({"type": "BattleSnapshot", "payload": payload}))
        view = state.view()
        self.assertEqual(view["snapshot"]["hand"], [1, 9, 1])
        self.assertEqual(view["snapshot"]["discard"], [2, 3])
        self.assertEqual(view["snapshot"]["turn_id"], MAX_UINT64)

    def test_empty_private_hand_and_history(self):
        message = snapshot_message(player_index=1)
        message.state.ClearField("hand")
        message.state.ClearField("discard")
        message.state.discard_count = 0
        payload = decode_payload("BattleSnapshot", message.SerializeToString())
        self.assertEqual(payload["hand"], "")
        self.assertEqual(payload["discard"], "")
        self.assertEqual(payload["player_index"], "1")

    def test_negative_winner_and_replay_uint64(self):
        message = messages.MatchResult(winner=-1, reason="draw")
        message.context.match_id = "match-proto"
        message.context.turn_id = MAX_UINT64
        message.replay.seed = MAX_UINT64
        message.replay.digest = MAX_UINT64
        payload = decode_payload("MatchResult", message.SerializeToString())
        self.assertEqual(payload["winner"], "-1")
        self.assertEqual(payload["replay_digest"], str(MAX_UINT64))
        self.assertEqual(payload["replay_valid"], "0")

    def test_queue_directions_share_message_id(self):
        request = messages.MatchJoinRequest.FromString(encode_payload("MatchJoinReq", {"request_id": "join"}))
        self.assertEqual(request.request_id, "join")
        payload = decode_payload("MatchJoinReq", messages.MatchJoinResponse(queued=True, request_id="join").SerializeToString())
        self.assertEqual(payload, {"queued": "1", "request_id": "join"})
        payload = decode_payload("MatchCancelReq", messages.MatchCancelResponse(cancelled=False).SerializeToString())
        self.assertEqual(payload["cancelled"], "0")

    def test_response_field_aliases_and_ack_context(self):
        login = messages.LoginResponse(ok=True, user="proto", session_token="secret", request_id="login")
        self.assertEqual(decode_payload("LoginResp", login.SerializeToString())["token"], "secret")
        reconnect = messages.ReconnectResponse(ok=False, error_code="expired", player_index=-1)
        self.assertEqual(decode_payload("ReconnectResp", reconnect.SerializeToString())["error"], "expired")
        ack = messages.ActionAck(applied=False, error_code="stale_turn")
        ack.context.action_id = MAX_UINT64
        ack.context.request_id = "ack-request"
        payload = decode_payload("ActionAck", ack.SerializeToString())
        self.assertEqual(payload["action_id"], str(MAX_UINT64))
        self.assertEqual(payload["applied"], "0")
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["request_id"], "ack-request")
        applied = messages.ActionAck(applied=True)
        payload = decode_payload("ActionAck", applied.SerializeToString())
        self.assertEqual(payload["status"], "applied")
        self.assertNotIn("code", payload)
        error = messages.ErrorResponse(code="invalid_card", message="bad card")
        self.assertEqual(decode_payload("Error", error.SerializeToString())["code"], "invalid_card")
        pong = messages.Pong(request_id="heartbeat")
        self.assertEqual(decode_payload("Pong", pong.SerializeToString()), {"request_id": "heartbeat"})

    def test_discard_event_preserves_zero_and_signed_player(self):
        message = messages.BattleEvent(type="discard", player=-1, card_id=9, value=2, target=0, count=0)
        message.context.match_id = "match-proto"
        payload = decode_payload("BattleEvent", message.SerializeToString())
        self.assertEqual(payload["player"], "-1")
        self.assertEqual(payload["target"], "0")
        self.assertEqual(payload["count"], "0")
        self.assertEqual(payload["card"], "9")
        self.assertNotIn("discard", payload)
        for field in ("duration", "status", "remaining", "hp"):
            self.assertNotIn(field, payload)

    def test_event_fields_are_conditional_by_type(self):
        context_keys = {"match_id", "turn_id", "action_id", "revision", "request_id"}
        cases = {
            "end_turn": {"type", "player"},
            "status_tick": {"type", "player", "status", "value", "remaining", "hp"},
            "damage": {"type", "player", "card", "value"},
            "heal": {"type", "player", "card", "value"},
            "shield": {"type", "player", "card", "value"},
            "draw": {"type", "player", "card", "value"},
            "poison": {"type", "player", "card", "value", "duration"},
            "regen": {"type", "player", "card", "value", "duration"},
            "burn": {"type", "player", "card", "value", "duration"},
            "attack_boost": {"type", "player", "card", "value", "uses"},
            "heal_boost": {"type", "player", "card", "value", "uses"},
            "discard": {"type", "player", "card", "value", "target", "count"},
        }
        for event_type, expected in cases.items():
            with self.subTest(event_type=event_type):
                message = messages.BattleEvent(type=event_type)
                payload = decode_payload("BattleEvent", message.SerializeToString())
                self.assertEqual(set(payload) - context_keys, expected)
        for event_type in ("endturn", "status_expired", "unknown"):
            with self.assertRaises(ValueError):
                decode_payload("BattleEvent", messages.BattleEvent(type=event_type).SerializeToString())

    def test_admin_counter_uint64(self):
        message = messages.AdminRoomsResponse(request_id="admin")
        message.counters["active_rooms"] = MAX_UINT64
        message.counters["active_connections"] = 0
        payload = decode_payload("AdminRoomsResp", message.SerializeToString())
        self.assertEqual(payload["active_rooms"], str(MAX_UINT64))
        self.assertEqual(payload["active_connections"], "0")

    def test_burn_snapshot_and_turn_end_phase(self):
        snapshot = snapshot_message()
        snapshot.state.players[1].burn.value = 4
        snapshot.state.players[1].burn.turns = 2
        decoded = decode_payload("BattleSnapshot", snapshot.SerializeToString())
        self.assertEqual(decoded["p1_burn_value"], "4")
        self.assertEqual(decoded["p1_burn_turns"], "2")
        snapshot.state.players[1].ClearField("burn")
        self.assertEqual(decode_payload("BattleSnapshot", snapshot.SerializeToString())["p1_burn_turns"], "0")
        tick = messages.BattleEvent(type="status_tick", phase="end", status="burn", player=1,
                                   value=4, remaining=0, hp=22)
        decoded = decode_payload("BattleEvent", tick.SerializeToString())
        self.assertEqual(decoded["phase"], "end")
        self.assertEqual(decoded["remaining"], "0")
        self.assertNotIn("card", decoded)

    def test_bonus_snapshot_application_and_zero_presence(self):
        message = snapshot_message()
        message.state.players[0].attack_boost.value = 3
        message.state.players[0].attack_boost.uses = 2
        message.state.players[1].heal_boost.value = 4
        message.state.players[1].heal_boost.uses = 1
        snapshot = decode_payload("BattleSnapshot", message.SerializeToString())
        self.assertEqual(snapshot["p0_attack_boost_value"], "3")
        self.assertEqual(snapshot["p0_attack_boost_uses"], "2")
        self.assertEqual(snapshot["p1_heal_boost_uses"], "1")
        self.assertEqual(snapshot["p0_heal_boost_uses"], "0")
        action = messages.BattleEvent(type="damage", card_id=1, value=8)
        self.assertNotIn("bonus", decode_payload("BattleEvent", action.SerializeToString()))
        action.bonus = 0
        self.assertEqual(decode_payload("BattleEvent", action.SerializeToString())["bonus"], "0")
        action.bonus = 3
        self.assertEqual(decode_payload("BattleEvent", action.SerializeToString())["bonus"], "3")
        applied = messages.BattleEvent(type="attack_boost", card_id=11, value=2, uses=2)
        fields = decode_payload("BattleEvent", applied.SerializeToString())
        self.assertEqual(fields["uses"], "2")
        self.assertNotIn("duration", fields)

    def test_invalid_requests_and_malformed_payloads(self):
        with self.assertRaises(ValueError):
            encode_payload("LoginResp", {})
        with self.assertRaises(TypeError):
            encode_payload("PlayCardReq", "card=1")
        for value in (-1, MAX_UINT64 + 1, 1.5, "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                encode_payload("EndTurnReq", {"action_id": value})
        with self.assertRaises(ValueError):
            decode_payload("Unknown(16499)", b"")
        with self.assertRaises(TypeError):
            decode_payload("Pong", "text")
        for body in (b"\x80", b"\x0a\x05a", b"\x00"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                decode_payload("LoginResp", body)

    def test_invalid_snapshot_structure(self):
        with self.assertRaises(ValueError):
            decode_payload("BattleSnapshot", b"")
        for mutation in ("players", "context", "player_index", "discard_count"):
            message = snapshot_message()
            if mutation == "players":
                message.state.players.pop()
            elif mutation == "context":
                message.ClearField("context")
            elif mutation == "player_index":
                message.player_index = 2
            else:
                message.state.discard_count = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                decode_payload("BattleSnapshot", message.SerializeToString())

    def test_bot_modes_preserve_default_and_alternate(self):
        self.assertEqual(Bot("localhost", 9000, "old").protocol, "text_v1")
        self.assertEqual([bot_protocol("mixed", index) for index in range(4)],
                         ["text_v1", "proto_v1", "text_v1", "proto_v1"])
        self.assertEqual(bot_protocol("proto_v1", 0), "proto_v1")
        with self.assertRaises(ValueError):
            bot_protocol("invalid", 0)


class CaptureWriter:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


class ProtobufTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_raw_parse_and_default_read_remain_binary(self):
        frame = make_proto_message("LoginResp", messages.LoginResponse(ok=True).SerializeToString())
        parsed = parse_message(frame[4:])
        self.assertEqual(parsed["payload"], {})
        self.assertEqual(parsed["payload_bytes"], frame[6:])
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        decoded = await read_message(reader)
        self.assertEqual(decoded, parsed)

    async def test_proto_send_and_structured_read(self):
        writer = CaptureWriter()
        await send_message(writer, "LoginReq", {"user": "proto", "request_id": "request"}, "proto_v1")
        frame = writer.frames[0]
        self.assertEqual(struct.unpack(">H", frame[4:6])[0], PROTO_MESSAGE_TYPES["LoginReq"])
        self.assertEqual(messages.LoginRequest.FromString(frame[6:]).user, "proto")
        response = messages.LoginResponse(ok=True, user="proto", session_token="token")
        reader = asyncio.StreamReader()
        reader.feed_data(make_proto_message("LoginResp", response.SerializeToString()))
        message = await read_message(reader, "proto_v1")
        self.assertEqual(message["payload"]["ok"], "1")
        self.assertEqual(message["payload"]["token"], "token")
        self.assertIsNone(message["raw"])

    async def test_default_send_keeps_text_login(self):
        writer = CaptureWriter()
        await send_message(writer, "LoginReq", "legacy")
        self.assertEqual(writer.frames[0], make_message("LoginReq", "legacy"))

    async def test_negotiation_is_text_and_preserves_next_proto_frame(self):
        reader = asyncio.StreamReader()
        reader.feed_data(make_message("ProtocolHelloResp", {"proto_v1_runtime": 1, "selected": "proto_v1"})
                         + make_proto_message("Pong", messages.Pong(request_id="next").SerializeToString()))
        writer = CaptureWriter()
        fields = await negotiate_protocol(reader, writer)
        self.assertEqual(fields["selected"], "proto_v1")
        hello = parse_message(writer.frames[0][4:])
        self.assertEqual(hello["message_id"], MESSAGE_TYPES["ProtocolHelloReq"])
        self.assertEqual(hello["payload"], {"protocol": "proto_v1", "version": "1"})
        message = await read_message(reader, "proto_v1")
        self.assertEqual(message["payload"]["request_id"], "next")

    async def test_negotiation_rejects_unavailable_runtime_without_fallback(self):
        for fields in ({"selected": "text_v1", "proto_v1_runtime": 1},
                       {"selected": "proto_v1", "proto_v1_runtime": 0},
                       {"selected": "proto_v1", "proto_v1_runtime": 1, "version": 2}):
            reader = asyncio.StreamReader()
            reader.feed_data(make_message("ProtocolHelloResp", fields))
            writer = CaptureWriter()
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                await negotiate_protocol(reader, writer)
            self.assertEqual(len(writer.frames), 1)

    async def test_text_mode_requires_no_hello(self):
        writer = CaptureWriter()
        fields = await negotiate_protocol(asyncio.StreamReader(), writer, "text_v1")
        self.assertEqual(fields["selected"], "text_v1")
        self.assertEqual(writer.frames, [])

    async def test_wrong_protocol_and_invalid_frame_rejected(self):
        reader = asyncio.StreamReader()
        reader.feed_data(make_message("Pong", ""))
        with self.assertRaises(ValueError):
            await read_message(reader, "proto_v1")
        for header in (struct.pack(">I", MAX_FRAME + 1), struct.pack(">I", 1) + b"\x00"):
            reader = asyncio.StreamReader()
            reader.feed_data(header)
            with self.subTest(header=header), self.assertRaises(ValueError):
                await read_message(reader, "proto_v1")
        with self.assertRaises(ValueError):
            await send_message(CaptureWriter(), "Heartbeat", {}, "invalid")


if __name__ == "__main__":
    unittest.main()
