"""Local RESP proxy for isolated Redis fault injection during load acceptance.

The upstream Redis process and its data remain untouched. A lost reply is
injected only after the upstream has returned a complete RESP response.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
import time


async def _read_resp(reader, depth=0):
    """Read one RESP2 frame, retaining its exact bytes and command value."""
    if depth > 32:
        raise ValueError("RESP nesting exceeds proxy limit")
    marker = await reader.readexactly(1)
    line = await reader.readuntil(b"\r\n")
    head = marker + line
    value = line[:-2]
    if marker in (b"+", b"-", b":"):
        return head, value
    if marker == b"$":
        size = int(value)
        if size == -1:
            return head, None
        if size < 0 or size > 16 * 1024 * 1024:
            raise ValueError("RESP bulk length exceeds proxy limit")
        data = await reader.readexactly(size + 2)
        if data[-2:] != b"\r\n":
            raise ValueError("invalid RESP bulk terminator")
        return head + data, data[:-2]
    if marker == b"*":
        count = int(value)
        if count == -1:
            return head, None
        if count < 0 or count > 65536:
            raise ValueError("RESP array length exceeds proxy limit")
        frames, values = [head], []
        for _ in range(count):
            frame, item = await _read_resp(reader, depth + 1)
            frames.append(frame)
            values.append(item)
        return b"".join(frames), values
    raise ValueError("fault proxy expects RESP2 frames")


@dataclass(eq=False)
class _Connection:
    number: int
    writer: asyncio.StreamWriter
    mode: str
    task: asyncio.Task
    upstream: asyncio.StreamWriter | None = None


class RedisFaultProxy:
    """Async local proxy with pass, refuse, blackhole and one-shot drop-reply."""

    MODES = frozenset(("pass", "refuse", "blackhole", "drop-reply"))

    def __init__(self, target_host: str, target_port: int):
        self.target_host = target_host
        self.target_port = target_port
        self.mode = "pass"
        self._drop_command = None
        self._server = None
        self._closed = False
        self._connections = set()
        self._events = deque(maxlen=1024)
        self._counters = dict.fromkeys((
            "accepted_connections", "upstream_connections", "blocked_connections",
            "refused_connections", "blocked_bytes", "upstream_request_bytes",
            "upstream_reply_bytes", "lost_replies", "upstream_failures",
        ), 0)

    @property
    def port(self):
        if self._server is None:
            raise RuntimeError("proxy is not listening")
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host="127.0.0.1", port=0):
        if self._closed or self._server is not None:
            raise RuntimeError("proxy already started or closed")
        if host != "127.0.0.1":
            raise ValueError("fault proxy must bind to loopback")
        self._server = await asyncio.start_server(self._handle, host, port)
        self._event("started", port=self.port)
        return self

    def stats(self):
        return {**self._counters, "mode": self.mode,
                "active_connections": len(self._connections),
                "events": [dict(event) for event in self._events]}

    def _event(self, event, **fields):
        self._events.append({"event": event, "at": time.monotonic(),
                             "mode": self.mode, **fields})

    @staticmethod
    async def _stop_connections(connections):
        for connection in connections:
            connection.writer.close()
            if connection.upstream is not None:
                connection.upstream.close()
            connection.task.cancel()
        await asyncio.gather(*(connection.task for connection in connections), return_exceptions=True)

    async def set_mode(self, mode, *, command=None):
        """Close old sockets on outage/recovery; optionally drop only EVAL replies."""
        if mode not in self.MODES:
            raise ValueError(f"unknown proxy mode: {mode}")
        if self._closed:
            raise RuntimeError("proxy is closed")
        if command is not None and mode != "drop-reply":
            raise ValueError("command filter applies only to drop-reply")
        previous = self.mode
        self.mode = mode
        self._drop_command = command.upper() if command is not None else None
        self._event("mode-changed", previous=previous, command=self._drop_command)
        connections = tuple(self._connections)
        # Keep established upstream sockets when arming a lost reply. Other
        # transitions close old sockets so the caller exercises reconnect.
        if mode in ("refuse", "blackhole") or previous in ("refuse", "blackhole"):
            if mode == "refuse":
                self._counters["refused_connections"] += len(connections)
            await self._stop_connections(connections)

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.close()
        # Python 3.12 Server.wait_closed also waits for active connections.
        # Stop those handlers before waiting for the listener to finish closing.
        await self._stop_connections(tuple(self._connections))
        if self._server is not None:
            await self._server.wait_closed()
        self._server = None
        self._event("closed")

    async def _handle(self, reader, writer):
        self._counters["accepted_connections"] += 1
        connection = _Connection(self._counters["accepted_connections"], writer,
                                 self.mode, asyncio.current_task())
        self._connections.add(connection)
        pumps = []
        try:
            if self._closed or connection.mode == "refuse":
                self._counters["refused_connections"] += 1
                self._event("connection-refused", connection=connection.number)
                return
            if connection.mode == "blackhole":
                self._counters["blocked_connections"] += 1
                self._event("connection-blocked", connection=connection.number)
                while data := await reader.read(65536):
                    self._counters["blocked_bytes"] += len(data)
                return
            try:
                upstream_reader, connection.upstream = await asyncio.open_connection(
                    self.target_host, self.target_port)
            except (ConnectionError, OSError):
                self._counters["upstream_failures"] += 1
                self._event("upstream-connect-failed", connection=connection.number)
                raise
            if writer.is_closing() or self._closed:
                return
            self._counters["upstream_connections"] += 1
            requests = deque()

            async def request_pump():
                while True:
                    frame, values = await _read_resp(reader)
                    command = (values[0].decode("ascii", errors="replace").upper()
                               if isinstance(values, list) and values and isinstance(values[0], bytes)
                               else "")
                    requests.append((command, len(frame)))
                    self._counters["upstream_request_bytes"] += len(frame)
                    connection.upstream.write(frame)
                    await connection.upstream.drain()

            async def reply_pump():
                while True:
                    frame, value = await _read_resp(upstream_reader)
                    self._counters["upstream_reply_bytes"] += len(frame)
                    command, request_bytes = requests.popleft() if requests else ("", 0)
                    reply_integer = int(value) if frame[:1] == b":" else None
                    if command == "EVAL":
                        self._event("eval-reply", connection=connection.number,
                                    reply_integer=reply_integer)
                    if self.mode == "drop-reply" and (
                            self._drop_command is None or self._drop_command == command):
                        self._counters["lost_replies"] += 1
                        self.mode = "pass"
                        self._drop_command = None
                        self._event("reply-dropped", connection=connection.number,
                                    command=command, request_bytes=request_bytes,
                                    reply_bytes=len(frame), reply_type=frame[:1].decode("ascii"),
                                    reply_integer=reply_integer)
                        return
                    writer.write(frame)
                    await writer.drain()

            pumps = [asyncio.create_task(request_pump()), asyncio.create_task(reply_pump())]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            # EOF and reset are expected both from the client and during faults.
            pass
        except (ValueError, asyncio.LimitOverrunError) as exc:
            self._counters["upstream_failures"] += 1
            self._event("protocol-error", connection=connection.number, error=str(exc))
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if connection.upstream is not None:
                connection.upstream.close()
            for stream in (writer, connection.upstream):
                if stream is not None:
                    with contextlib.suppress(ConnectionError, OSError):
                        await stream.wait_closed()
            self._connections.discard(connection)
