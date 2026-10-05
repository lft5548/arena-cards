"""TextV1 and opt-in ProtoV1 share a uint32 length / uint16 type envelope."""
from __future__ import annotations
import asyncio, struct
from enum import IntEnum
from typing import Any, Dict

MAX_FRAME = 65536
class ProtocolId(IntEnum):
    """Wire protocol generations sharing the length/type envelope."""
    TEXT_V1 = 1
    PROTO_V1 = 2

TEXT_V1_VERSION = 1
PROTO_V1_VERSION = 1
PROTO_V1_MESSAGE_BASE = 0x4000

MESSAGE_TYPES = {"LoginReq":1,"LoginResp":2,"MatchJoinReq":3,"MatchCancelReq":4,
                 "MatchFound":5,"PlayCardReq":6,"BattleSnapshot":7,"BattleEvent":8,
                 "Error":9,"Heartbeat":10,"Pong":11,"ReconnectReq":12,
                 "ReconnectResp":13,"MatchResult":14,"EndTurnReq":15,"AdminRoomsReq":16,
                 "AdminRoomsResp":17,"ActionAck":18,"ProtocolHelloReq":19,
                 "ProtocolHelloResp":20,"LeaderboardReq":21,"LeaderboardResp":22}
MESSAGE_NAMES = {v:k for k,v in MESSAGE_TYPES.items()}

PROTO_MESSAGE_TYPES = {name: PROTO_V1_MESSAGE_BASE + value
                       for name, value in MESSAGE_TYPES.items()
                       if name not in {"ProtocolHelloReq", "ProtocolHelloResp"}}
PROTO_MESSAGE_NAMES = {v:k for k,v in PROTO_MESSAGE_TYPES.items()}

def make_message(msg_type: str, payload: Dict[str, Any] | str | None = None) -> bytes:
    mid = MESSAGE_TYPES.get(msg_type, msg_type if isinstance(msg_type, int) else 0)
    if not mid: raise ValueError(f"unknown message type: {msg_type}")
    if isinstance(payload, str): body_text = payload
    else:
        values = payload or {}
        body_text = ";".join(f"{k}={str(v).replace(';','%3B')}" for k,v in values.items())
    body = struct.pack(">H", int(mid)) + body_text.encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ValueError("message too large")
    return struct.pack(">I", len(body)) + body

def make_proto_message(msg_type: str | int, payload: bytes = b"") -> bytes:
    """Build a binary frame without changing the raw/default TextV1 codec."""
    mid = PROTO_MESSAGE_TYPES.get(msg_type, msg_type if isinstance(msg_type, int) else 0)
    if not mid or mid < PROTO_V1_MESSAGE_BASE:
        raise ValueError(f"unknown ProtoV1 message type: {msg_type}")
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("ProtoV1 payload must be bytes")
    body = struct.pack(">H", int(mid)) + bytes(payload)
    if len(body) > MAX_FRAME:
        raise ValueError("message too large")
    return struct.pack(">I", len(body)) + body

def parse_message(body: bytes) -> Dict[str, Any]:
    if len(body) < 2: raise ValueError("invalid message")
    mid = struct.unpack(">H", body[:2])[0]
    protocol = "ProtoV1" if mid >= PROTO_V1_MESSAGE_BASE else "TextV1"
    if protocol == "ProtoV1":
        return {"type": PROTO_MESSAGE_NAMES.get(mid, f"Unknown({mid})"),
                "message_id": mid, "protocol": protocol, "payload": {},
                "payload_bytes": bytes(body[2:]), "raw": None}
    text = body[2:].decode("utf-8")
    payload: Dict[str, Any] = {}
    for item in text.split(";") if text else []:
        if "=" in item:
            k,v=item.split("=",1)
            value = v.replace("%3B", ";")
            if mid == MESSAGE_TYPES["LeaderboardResp"] and (k == "request_id" or
                    (k.startswith("entry_") and k.endswith("_user"))):
                value = value.replace("%25", "%")
            payload[k] = value
    if text and not payload: payload["value"] = text
    return {"type": MESSAGE_NAMES.get(mid, f"Unknown({mid})"), "message_id": mid,
            "protocol": protocol, "payload": payload, "raw": text}

def payload_int(payload: Dict[str, Any], key: str, default: int = 0) -> int:
    try: return int(payload.get(key, default))
    except (TypeError, ValueError): return default

class FrameReader:
    def __init__(self) -> None: self.buffer = bytearray()
    def feed(self, data: bytes) -> list[Dict[str, Any]]:
        self.buffer.extend(data); out = []
        while len(self.buffer) >= 4:
            n = struct.unpack(">I", self.buffer[:4])[0]
            if n > MAX_FRAME: raise ValueError("frame too large")
            if len(self.buffer) < 4 + n: break
            body = bytes(self.buffer[4:4+n]); del self.buffer[:4+n]
            out.append(parse_message(body))
        return out

def _validate_protocol(protocol):
    if protocol not in ("text_v1", "proto_v1"):
        raise ValueError(f"unknown protocol: {protocol}")

def _protobuf_codec():
    if __package__:
        from . import protobuf_codec
    else:
        import protobuf_codec
    return protobuf_codec

async def read_message(reader: asyncio.StreamReader, protocol: str = "text_v1") -> Dict[str, Any]:
    _validate_protocol(protocol)
    raw = await reader.readexactly(4)
    n = struct.unpack(">I", raw)[0]
    if n < 2 or n > MAX_FRAME: raise ValueError("invalid frame length")
    message = parse_message(await reader.readexactly(n))
    if protocol == "proto_v1":
        if message["protocol"] != "ProtoV1":
            raise ValueError("expected ProtoV1 frame after negotiation")
        message["payload"] = _protobuf_codec().decode_payload(message["type"], message["payload_bytes"])
    return message

async def send_message(writer: asyncio.StreamWriter, msg_type: str,
                       payload: Dict[str, Any] | str | None = None,
                       protocol: str = "text_v1") -> None:
    _validate_protocol(protocol)
    if protocol == "proto_v1":
        frame = make_proto_message(msg_type, _protobuf_codec().encode_payload(msg_type, payload))
    else:
        frame = make_message(msg_type, payload)
    writer.write(frame)
    await writer.drain()

async def negotiate_protocol(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                             protocol: str = "proto_v1") -> Dict[str, Any]:
    """Select ProtoV1 through TextV1 before login; never silently fall back."""
    _validate_protocol(protocol)
    if protocol == "text_v1":
        return {"selected": "text_v1"}
    _protobuf_codec()
    await send_message(writer, "ProtocolHelloReq", {"protocol": "proto_v1", "version": 1})
    response = await asyncio.wait_for(read_message(reader), 5.0)
    if response["type"] != "ProtocolHelloResp" or response["protocol"] != "TextV1":
        raise ValueError("expected TextV1 ProtocolHelloResp")
    fields = response["payload"]
    if fields.get("selected") != "proto_v1" or fields.get("proto_v1_runtime") != "1":
        raise ValueError("server did not enable/select ProtoV1 runtime")
    if "version" in fields and fields["version"] != "1":
        raise ValueError("unsupported ProtoV1 version")
    return fields

encode_message = make_message
decode_message = parse_message

def decode_frame(frame: bytes) -> Dict[str, Any]:
    """Decode one complete wire frame including its four-byte length prefix."""
    if len(frame) < 6:
        raise ValueError("incomplete frame")
    n = struct.unpack(">I", frame[:4])[0]
    if n > MAX_FRAME or len(frame) != n + 4:
        raise ValueError("invalid frame length")
    return parse_message(frame[4:])
