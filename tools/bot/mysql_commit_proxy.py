"""Private transparent MySQL proxy for a successful COMMIT with a lost reply.

The fixture clears CLIENT_SSL in the initial server greeting so its private test
connection uses the inspectable MySQL packet protocol. It never issues SQL or
changes shared MySQL/Redis service settings; it does not test production TLS.
"""
from __future__ import annotations

import asyncio
import contextlib


async def read_packet(reader):
    header = await reader.readexactly(4)
    size = int.from_bytes(header[:3], "little")
    return header + await reader.readexactly(size)


class MysqlCommitReplyProxy:
    def __init__(self, host, port):
        self.host, self.target_port = host, port
        self.server = None
        self.port = 0
        self.armed = False
        self.dropped = asyncio.Event()
        self.tasks = set()
        self.writers = set()
        self.observations = []
        self.errors = []
        self.plaintext_handshakes = 0
        self.tls_capability_removed = False

    async def start(self):
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    def arm(self):
        if self.armed or self.dropped.is_set():
            raise RuntimeError("each proxy instance drops exactly one COMMIT reply")
        self.armed = True

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        upstream = None
        forwarding = []
        state = {"commit": False, "query_attributes": False}
        try:
            target_reader, upstream = await asyncio.open_connection(self.host, self.target_port)
            self.writers.add(upstream)

            async def client_to_server():
                first_packet = True
                while True:
                    packet = await read_packet(reader)
                    payload = packet[4:]
                    if first_packet and packet[3] == 1 and len(payload) >= 32:
                        state["query_attributes"] = bool(int.from_bytes(payload[:4], "little") & (1 << 27))
                    first_packet = False
                    query = payload[1:]
                    if state["query_attributes"]:
                        # Zero query parameters and one parameter set, both
                        # length-encoded, precede the SQL in MySQL 8 clients.
                        # Other attribute encodings are transparently forwarded
                        # but are never mistaken for the selected COMMIT.
                        query = query[2:] if query[:2] == b"\x00\x01" else b""
                    # Commands start at sequence zero; authentication packets do not.
                    if (self.armed and packet[3] == 0 and payload[:1] == b"\x03"
                            and query.strip().rstrip(b";").upper() == b"COMMIT"):
                        self.armed = False
                        state["commit"] = True
                    upstream.write(packet)
                    await upstream.drain()

            async def server_to_client():
                first_packet = True
                while True:
                    packet = await read_packet(target_reader)
                    if first_packet and packet[4:5] == b"\x0a":
                        version_end = packet.index(b"\x00", 5)
                        low_flags = version_end + 1 + 4 + 8 + 1
                        if len(packet) < low_flags + 2:
                            raise AssertionError("invalid MySQL protocol-10 greeting")
                        original_flags = int.from_bytes(packet[low_flags:low_flags + 2], "little")
                        self.tls_capability_removed |= bool(original_flags & 0x0800)
                        flags = original_flags & ~0x0800
                        packet = packet[:low_flags] + flags.to_bytes(2, "little") + packet[low_flags + 2:]
                        self.plaintext_handshakes += 1
                    first_packet = False
                    if state["commit"]:
                        if packet[4:5] != b"\x00":
                            self.observations.append({"commit_ok": False,
                                                      "response_type": packet[4:5].hex()})
                            raise AssertionError("selected COMMIT did not return an OK packet")
                        self.observations.append({"commit_ok": True, "reply_sequence": packet[3],
                                                  "discarded_reply_bytes": len(packet)})
                        self.dropped.set()
                        return
                    writer.write(packet)
                    await writer.drain()

            forwarding = [asyncio.create_task(client_to_server()), asyncio.create_task(server_to_client())]
            done, _ = await asyncio.wait(forwarding, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                await finished
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception as error:
            self.errors.append(str(error))
            self.dropped.set()
        finally:
            for pending in forwarding:
                pending.cancel()
            await asyncio.gather(*forwarding, return_exceptions=True)
            for connection in (writer, upstream):
                if connection:
                    connection.close()
                    with contextlib.suppress(OSError):
                        await connection.wait_closed()
                    self.writers.discard(connection)
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
        for writer in tuple(self.writers):
            writer.close()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
