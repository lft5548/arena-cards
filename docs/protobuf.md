# Protobuf Runtime Design

TextV1 and ProtoV1 share the TCP length/type envelope but use disjoint IDs.
The default build enables the full ProtoV1 runtime. Text clients need no
negotiation; binary clients select ProtoV1 before login. See
[protocol-versioning.md](protocol-versioning.md) for negotiation and rejection rules.

## Schema and State Ownership

`proto/arena_cards.proto` is the common C++/Python/C# schema. Login, heartbeat,
match join/cancel, match found, play/end turn, ACK, result, reconnect, errors
and admin counters use typed messages. LeaderboardRequest has an optional uint32
limit (absent=10, explicit 0 rejected); LeaderboardResponse contains typed rank,
player_id, int64 rating and uint64 wins/losses, plus source/error/request_id.
It queries MySQL through C++, not from Unity directly. Battle snapshots contain public player
HP, energy, shield, statuses and bonuses, plus only the viewer's hand/discard.
The opponent's cards are represented by counts. Events are structured fields,
not text hidden in a `bytes state` or `bytes event` field.

Gateway Session decodes requests through `server/gateway/protobuf_codec.cpp`
and delivers a SessionHandler callback. The application Session submits the
existing room commands. The room actor remains the sole battle
writer. Responses pass through the same adapter; Python and C# normalize typed
fields into their existing authoritative view models. This keeps one rules
implementation and allows TextV1 and ProtoV1 players in the same room.
The internal text adapter is a compatibility boundary, not a second battle engine.

Business routing lives in `server/app/session.cpp`, pairing in `server/match/matchmaker.cpp`
and actor/command execution in `server/room/room.cpp`. Shared services and counters are
explicit Runtime dependencies; none of these business modules live inside gateway.

`match_id`, `turn_id` and `action_id` identify the battle action. `request_id`
correlates responses; it is not the idempotency key. Retrying a successful action
returns the original cached ACK even with a different request ID. Proto context
`revision` identifies the replay event position; `state.snapshot_revision` retains
the legacy turn-based snapshot revision. A request's revision is advisory;
authoritative turn/action validation decides whether it is accepted.

Proto snapshots include `last_action_id` so a reconnected client can continue its
sequence. A new transport negotiates independently before sending its resume token;
the room identity and state remain unchanged. All uint64 values remain lossless.

## Dependencies and Generation

- C++17; protoc/C++ runtime and Google.Protobuf: `3.21.12`.
- Python runtime: `protobuf==4.21.12` (the same release family).
- Unity scripts target .NET Standard 2.1; the console transport tests use .NET 9
  with Unity stubs and do not replace Unity Editor validation.
- CMake defaults `ARENA_PROTOBUF_ENABLED=ON`, finds the exact C++ version and
  generates C++ into the build directory. Use `ARENA_PROTOBUF_ROOT` for a local
  installation. Explicit `OFF` builds TextV1 only and advertises runtime=0.
- `tools/protocol/generate.ps1` regenerates checked-in Python/C# code from the
  schema using pinned protoc and relative paths, including Windows Unicode paths.
- `tools/protocol/requirements.txt` pins Python; `install_unity_protobuf.py`
  installs official pinned NuGet DLLs into `Assets/Plugins/Protobuf`. Generated
  source is tracked; downloaded DLLs and build output are ignored.

The transport's socket/framing/FIFO/lifetime behavior is exercised by `gateway_test.cpp`.
Protocol behavior is exercised by `protobuf_codec_test.cpp`, `test_protobuf_codec.py`,
`protobuf_runtime_test.py`, `bot_protocol_test.py` and `csharp_protocol_test.py`.
They cover malformed frames, negotiation, private state, mixed rooms, ACK/reconnect
and terminal offline replay. Replay format and game rules are protocol-independent.
