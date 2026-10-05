"""Verify the fault fixture discards a real OK response only for armed COMMIT."""
from __future__ import annotations

import asyncio
import contextlib
import unittest

from tools.bot.mysql_commit_proxy import MysqlCommitReplyProxy, read_packet


def packet(payload, sequence=0):
    return len(payload).to_bytes(3, "little") + bytes([sequence]) + payload


class MysqlCommitProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.response = b"\x00\x00\x00\x02\x00\x00\x00"
        self.commands = []
        self.writers = set()

        async def handle(reader, writer):
            self.writers.add(writer)
            try:
                while True:
                    request = await read_packet(reader)
                    self.commands.append(request[4:])
                    writer.write(packet(self.response, 1))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                self.writers.discard(writer)

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.proxy = MysqlCommitReplyProxy("127.0.0.1", self.server.sockets[0].getsockname()[1])
        await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.close()
        for writer in tuple(self.writers):
            writer.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_armed_commit_ok_is_lost_and_next_connection_is_transparent(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.proxy.arm()
        writer.write(packet(b"\x03COMMIT"))
        await writer.drain()
        await asyncio.wait_for(self.proxy.dropped.wait(), 2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertEqual(self.commands, [b"\x03COMMIT"])
        self.assertEqual(self.proxy.observations, [{"commit_ok": True, "reply_sequence": 1, "discarded_reply_bytes": 11}])
        self.assertFalse(self.proxy.errors)
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(packet(b"\x03COMMIT"))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(read_packet(reader), 2), packet(self.response, 1))
        writer.close()
        await writer.wait_closed()

    async def test_sql_containing_commit_does_not_consume_arm(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.proxy.arm()
        writer.write(packet(b"\x03SELECT 'COMMIT'"))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(read_packet(reader), 2), packet(self.response, 1))
        self.assertTrue(self.proxy.armed)
        self.assertFalse(self.proxy.dropped.is_set())
        writer.write(packet(b"\x03 commit;  "))
        await writer.drain()
        await asyncio.wait_for(self.proxy.dropped.wait(), 2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        writer.close()
        await writer.wait_closed()

    async def test_commit_error_cannot_be_reported_as_lost_success(self):
        self.response = b"\xff\x01\x00#HY000failed"
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.proxy.arm()
        writer.write(packet(b"\x03COMMIT"))
        await writer.drain()
        await asyncio.wait_for(self.proxy.dropped.wait(), 2)
        self.assertFalse(self.proxy.observations[0]["commit_ok"])
        self.assertTrue(self.proxy.errors)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        writer.close()
        await writer.wait_closed()

    async def test_negotiated_mysql8_zero_query_attributes_are_recognized(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(packet((1 << 27).to_bytes(4, "little") + bytes(28), 1))
        await writer.drain()
        await asyncio.wait_for(read_packet(reader), 2)
        self.proxy.arm()
        writer.write(packet(b"\x03\x00\x01COMMIT"))
        await writer.drain()
        await asyncio.wait_for(self.proxy.dropped.wait(), 2)
        self.assertTrue(self.proxy.observations[0]["commit_ok"])
        self.assertEqual(self.commands[-1], b"\x03\x00\x01COMMIT")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
