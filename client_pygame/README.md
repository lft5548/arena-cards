# Python client

Install optional UI dependency with `pip install pygame`. Without pygame the
same entry point automatically falls back to a terminal client.

```powershell
python -m client_pygame.client --host 127.0.0.1 --port 9000
python -m client_pygame.client --cli
```

Press `M` to match, keys `1..9` to play a hand slot, and `E` to end your turn.
Mouse clicks on the cards and End Turn button work as well. The client does
not calculate damage: all validation and effects come from server snapshots.

The CLI/Pygame entry point uses TextV1: four-byte big-endian body length,
two-byte message ID and UTF-8 semicolon-separated key=value fields. Shared
protocol helpers also support ProtoV1 for Bot and test clients. Set --host
and --port to match the C++ server. The [demo guide](../docs/demo-guide.md)
provides the full Docker and Unity workflow.

After a successful login the client stores the opaque `token` from `LoginResp`.
If the TCP connection drops during matchmaking or a battle, it opens a new
connection every 500 ms for up to 15 seconds and sends that token in
`ReconnectReq`. A successful `ReconnectResp` is followed by the server's
personalized snapshot, so the local hand, match and result state remain
available while transport recovery is in progress. When the window expires,
the client emits a final `Disconnected` event and clears the session.
