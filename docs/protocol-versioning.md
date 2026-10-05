# Protocol versioning and ID isolation

Arena Cards keeps one TCP envelope for both protocol generations:

```text
uint32_be body_length
uint16_be message_id
bytes    payload
```

`TextV1` is protocol id `1`, version `1`. It owns message ids `1..22`; ids
`1..18` are the existing login, matchmaking and battle messages, while `19`
and `20` are the optional `ProtocolHelloReq` and `ProtocolHelloResp` capability
messages. The default Python, Bot and Unity clients continue to use TextV1.
Ids `21/22` add `LeaderboardReq/LeaderboardResp`; all old IDs are unchanged.
The corresponding binary IDs are `0x4015/0x4016`. Binary `0x4013/0x4014`
remain unused; capability negotiation keeps the existing text IDs `19/20`.

`ProtoV1` is protocol id `2`, version `1`. Its message ids start at `0x4001`
(`0x4000` is the reserved base), so a protobuf payload can never be decoded as
a TextV1 message by accident. The common schema is `proto/arena_cards.proto`;
the default build generates and links the C++ runtime and supports full binary
game dispatch. TextV1 remains available on the same listener.

Leaderboard requests require login, default to 10 entries when limit is absent,
and validate an explicit limit in 1..20. Results come from authoritative MySQL,
ordered by rating descending and player_id ascending. Failures return
`ok=0;code=login_required|invalid_limit|leaderboard_unavailable;count=0`.
Text entries use `entry_N_rank/user/rating/wins/losses`; only leaderboard user
and response request_id fields escape `%` as `%25` and `;` as `%3B`, decoded
in that order of meaning (`%3B` first, then `%25`) exactly once. Other historical
TextV1 fields keep their old escaping behavior. Request IDs are at most 128
printable ASCII bytes, excluding semicolon and equals. ProtoV1 carries typed
entries with int64 rating and uint64 wins/losses, preserving numeric values.

Clients may send a TextV1 `ProtocolHelloReq` to discover capabilities. The
server responds with:

```text
text_v1=1;proto_v1=1;proto_v1_runtime=1;selected=text_v1;version=1;proto_message_base=16384
```

To select ProtoV1, send TextV1 `ProtocolHelloReq` with
`protocol=proto_v1;version=1` before login. The TextV1 reply reports
`selected=proto_v1;proto_v1_runtime=1`; subsequent game frames use binary IDs.
There is no silent fallback. Builds explicitly configured without Protobuf
report runtime=0 and reject selection with `protocol_unavailable`.

The first non-hello request or explicit selection locks the connection's protocol.
Switching afterward returns `protocol_locked`; wrong-namespace frames return
`protocol_mismatch` or `protocol_not_negotiated`. Unsupported versions, ambiguous
hello fields, malformed protobuf, unknown IDs and server-response IDs used as
requests are rejected. Discovery alone does not change a legacy connection.
A fresh reconnect transport may select either protocol before presenting its token.

Snapshot and event schema extensions are additive. `BattleEvent.bonus` is optional:
absence represents an older event contract; explicit zero represents a bonus-aware
action without an active charge. Clients must not drop this distinction.

Never put protobuf bytes behind TextV1 ids `1..18`. That would let an old
client accept a frame while silently misreading its payload.
