# Arena Cards wire protocol

TCP uses a six-byte minimum envelope:

```text
uint32_be body_length    # 2 + payload bytes
uint16_be message_type
bytes     payload       # TextV1 fields or ProtoV1 bytes
```

Frames smaller than two body bytes or larger than 64 KiB are rejected.
TextV1 IDs in messages.h occupy 1..22. ProtoV1 uses disjoint IDs beginning
at 0x4001; 0x4013/0x4014 remain unused, and leaderboard uses 0x4015/0x4016.
Capability negotiation retains text IDs 19/20. See
[protocol versioning](../docs/protocol-versioning.md) before extending messages.

arena_cards.proto is the shared C++/Python/C# schema. The default build
generates C++ and enables the full ProtoV1 runtime; Python and C# generated
sources are tracked. Pinned dependencies and generation commands are documented
in [protobuf.md](../docs/protobuf.md).

Text login uses message type 1 with a player name:

```python
frame = struct.pack("!IH", 2 + len(payload), 1) + payload.encode()
```

PlayCard carries match_id, turn_id, card and action_id. EndTurn uses type 15
with the same identity fields and no card. Successful same-signature retries
return the original ACK. request_id correlates a response and does not replace
the action idempotency key.

The authoritative catalog is server/config/cards.csv, accepting five-column
and six-column duration forms. Effects include damage, heal, shield, draw,
poison, regen, discard, burn, attack_boost and heal_boost. The server validates
IDs, costs, ranges, effect-specific durations and required starter cards 1–3
before listening. Rules and private snapshot visibility are described in
[protocol.md](../docs/protocol.md) and the battle design documents.
