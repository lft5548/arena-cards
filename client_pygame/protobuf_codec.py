"""Structured ProtoV1 payloads adapted to the authoritative client view."""
from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from google.protobuf.message import DecodeError

if __package__:
    from .generated import arena_cards_pb2 as messages
else:
    from generated import arena_cards_pb2 as messages


REQUEST_MESSAGES = {
    "LoginReq": messages.LoginRequest,
    "MatchJoinReq": messages.MatchJoinRequest,
    "MatchCancelReq": messages.MatchCancelRequest,
    "PlayCardReq": messages.PlayCardRequest,
    "EndTurnReq": messages.EndTurnRequest,
    "Heartbeat": messages.Heartbeat,
    "ReconnectReq": messages.ReconnectRequest,
    "AdminRoomsReq": messages.AdminRoomsRequest,
    "LeaderboardReq": messages.LeaderboardRequest,
}
RESPONSE_MESSAGES = {
    "LoginResp": messages.LoginResponse,
    "MatchJoinReq": messages.MatchJoinResponse,
    "MatchCancelReq": messages.MatchCancelResponse,
    "MatchFound": messages.MatchFound,
    "BattleSnapshot": messages.BattleSnapshot,
    "BattleEvent": messages.BattleEvent,
    "Error": messages.ErrorResponse,
    "Pong": messages.Pong,
    "ReconnectResp": messages.ReconnectResponse,
    "MatchResult": messages.MatchResult,
    "AdminRoomsResp": messages.AdminRoomsResponse,
    "ActionAck": messages.ActionAck,
    "LeaderboardResp": messages.LeaderboardResponse,
}


def _values(msg_type, payload):
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, str):
        if msg_type == "LoginReq":
            return {"user": payload}
        if msg_type == "ReconnectReq":
            return {"session_token": payload}
        if not payload:
            return {}
    raise TypeError("ProtoV1 requests require a mapping; login/reconnect also accept strings")


def _integer(values, key):
    value = values.get(key, 0)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    raise ValueError(f"invalid integer for {key}")


def _request_id(values):
    request_id = values.get("request_id")
    if request_id is None or request_id == "":
        return uuid4().hex
    if not isinstance(request_id, str):
        raise TypeError("request_id must be a string")
    return request_id


def encode_payload(msg_type, payload=None):
    """Serialize a client request; supplied request IDs are never replaced."""
    message_class = REQUEST_MESSAGES.get(msg_type)
    if message_class is None:
        raise ValueError(f"unsupported ProtoV1 client request: {msg_type}")
    values = _values(msg_type, payload)
    message = message_class()
    request_id = _request_id(values)
    if msg_type in ("PlayCardReq", "EndTurnReq"):
        message.context.match_id = values.get("match_id", "")
        message.context.turn_id = _integer(values, "turn_id")
        message.context.action_id = _integer(values, "action_id")
        message.context.revision = _integer(values, "revision")
        message.context.request_id = request_id
        if msg_type == "PlayCardReq":
            card_values = dict(values)
            if "card" not in card_values and "card_id" in card_values:
                card_values["card"] = card_values["card_id"]
            message.card_id = _integer(card_values, "card")
    else:
        message.request_id = request_id
        if msg_type == "LoginReq":
            message.user = values.get("user", "")
        elif msg_type == "ReconnectReq":
            message.session_token = values.get("session_token", values.get("token", ""))
        elif msg_type == "LeaderboardReq" and "limit" in values:
            message.limit = _integer(values, "limit")
    return message.SerializeToString(deterministic=True)


def _context(context):
    return {
        "match_id": context.match_id,
        "turn_id": str(context.turn_id),
        "action_id": str(context.action_id),
        "revision": str(context.revision),
        "request_id": context.request_id,
    }


def _replay(replay):
    return {
        "replay_seed": str(replay.seed),
        "replay_revision": str(replay.revision),
        "replay_digest": str(replay.digest),
        "replay_valid": "1" if replay.valid else "0",
    }


def _snapshot(message):
    if not message.HasField("context") or not message.context.match_id:
        raise ValueError("ProtoV1 snapshot has no match context")
    if not message.HasField("state") or len(message.state.players) != 2:
        raise ValueError("ProtoV1 snapshot must contain exactly two public players")
    if message.player_index not in (0, 1):
        raise ValueError("invalid ProtoV1 snapshot player index")
    state = message.state
    if state.discard_count != len(state.discard):
        raise ValueError("ProtoV1 private discard count does not match history")
    payload = _context(message.context)
    payload.update({
        "player_index": str(message.player_index),
        "turn": str(state.turn),
        "revision": str(state.snapshot_revision),
        "remaining_ms": str(state.remaining_ms),
        "done": "1" if state.done else "0",
        "hand": ",".join(str(card) for card in state.hand),
        "opponent_hand_count": str(state.opponent_hand_count),
        "deck_count": str(state.deck_count),
        "discard": ",".join(str(card) for card in state.discard),
        "discard_count": str(state.discard_count),
        "opponent_discard_count": str(state.opponent_discard_count),
        "last_action_id": str(state.last_action_id),
    })
    payload.update(_replay(state.replay))
    for player_index, player in enumerate(state.players):
        prefix = f"p{player_index}_"
        for field in ("hp", "energy", "shield"):
            payload[prefix + field] = str(getattr(player, field))
        for status in ("poison", "regen", "burn"):
            effect = getattr(player, status)
            payload[prefix + status + "_value"] = str(effect.value)
            payload[prefix + status + "_turns"] = str(effect.turns)
        for kind in ("attack_boost", "heal_boost"):
            bonus = getattr(player, kind)
            payload[prefix + kind + "_value"] = str(bonus.value)
            payload[prefix + kind + "_uses"] = str(bonus.uses)
    return payload


def decode_payload(msg_type, payload_bytes):
    """Decode a server response into TextV1-compatible string-valued fields."""
    message_class = RESPONSE_MESSAGES.get(msg_type)
    if message_class is None:
        raise ValueError(f"unsupported ProtoV1 server response: {msg_type}")
    if not isinstance(payload_bytes, (bytes, bytearray)):
        raise TypeError("ProtoV1 payload must be bytes")
    message = message_class()
    try:
        message.ParseFromString(bytes(payload_bytes))
    except DecodeError as error:
        raise ValueError(f"malformed ProtoV1 {msg_type} payload") from error
    if msg_type == "LoginResp":
        return {"ok": "1" if message.ok else "0", "user": message.user,
                "token": message.session_token, "error": message.error_code,
                "request_id": message.request_id}
    if msg_type == "MatchJoinReq":
        return {"queued": "1" if message.queued else "0", "request_id": message.request_id}
    if msg_type == "MatchCancelReq":
        return {"cancelled": "1" if message.cancelled else "0", "request_id": message.request_id}
    if msg_type == "Pong":
        return {"request_id": message.request_id}
    if msg_type == "AdminRoomsResp":
        return {**{key: str(value) for key, value in message.counters.items()},
                "request_id": message.request_id}
    if msg_type == "LeaderboardResp":
        payload = {"ok": "1" if message.ok else "0", "code": message.error_code,
                   "source": message.source, "count": str(len(message.entries)),
                   "request_id": message.request_id}
        for index, entry in enumerate(message.entries):
            prefix = f"entry_{index}_"
            payload.update({prefix + "rank": str(entry.rank), prefix + "user": entry.player_id,
                            prefix + "rating": str(entry.rating), prefix + "wins": str(entry.wins),
                            prefix + "losses": str(entry.losses)})
        return payload
    if msg_type == "BattleSnapshot":
        return _snapshot(message)
    payload = _context(message.context)
    if msg_type == "MatchFound":
        payload.update({"player_index": str(message.player_index), "turn": str(message.turn),
                        "room": str(message.room)})
    elif msg_type == "ActionAck":
        payload.update({"status": "applied" if message.applied else "rejected",
                        "applied": "1" if message.applied else "0"})
        if message.error_code:
            payload["code"] = message.error_code
    elif msg_type == "ReconnectResp":
        payload.update({"ok": "1" if message.ok else "0", "error": message.error_code,
                        "player_index": str(message.player_index)})
    elif msg_type == "Error":
        payload.update({"code": message.code, "message": message.message})
    elif msg_type == "MatchResult":
        payload.update({"winner": str(message.winner), "reason": message.reason})
        payload.update(_replay(message.replay))
    elif msg_type == "BattleEvent":
        if message.type not in {"end_turn", "status_tick", "damage", "heal", "shield", "draw", "poison", "regen", "discard", "burn", "attack_boost", "heal_boost"}:
            raise ValueError("unsupported ProtoV1 battle event")
        payload.update({"type": message.type, "player": str(message.player)})
        if message.type == "status_tick":
            payload.update({"status": message.status, "value": str(message.value),
                            "remaining": str(message.remaining), "hp": str(message.hp)})
            if message.phase:
                payload["phase"] = message.phase
        elif message.type != "end_turn":
            payload.update({"card": str(message.card_id), "value": str(message.value)})
            if message.type in {"poison", "regen", "burn"}:
                payload["duration"] = str(message.duration)
            if message.type == "discard":
                payload.update({"target": str(message.target), "count": str(message.count)})
            if message.type in {"attack_boost", "heal_boost"}:
                payload["uses"] = str(message.uses)
            if message.type in {"damage", "heal"} and message.HasField("bonus"):
                payload["bonus"] = str(message.bonus)
    return payload
