"""Real local sockets verify Redis fault windows without shared services."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.bot.redis_fault_proxy import RedisFaultProxy


def request(*values):
    values = [value if isinstance(value, bytes) else value.encode() for value in values]
    return b"*%d\r\n" % len(values) + b"".join(
        b"$%d\r\n" % len(value) + value + b"\r\n" for value in values)


class RedisFaultProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream_connections = 0
        self.applied = 0
        self.returned = []
        self.fake_tasks = set()
        self.client_writers = []
        self.fake = await asyncio.start_server(self.fake_redis, "127.0.0.1", 0)
        self.proxy = await RedisFaultProxy("127.0.0.1", self.fake.sockets[0].getsockname()[1]).start()

    async def asyncTearDown(self):
        await self.proxy.close()
        for writer in self.client_writers:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        self.fake.close()
        await self.fake.wait_closed()
        tasks = tuple(self.fake_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def fake_redis(self, reader, writer):
        task = asyncio.current_task()
        self.fake_tasks.add(task)
        self.upstream_connections += 1
        try:
            while True:
                header = await reader.readuntil(b"\r\n")
                self.assertTrue(header.startswith(b"*"))
                values = []
                for _ in range(int(header[1:-2])):
                    bulk = await reader.readuntil(b"\r\n")
                    self.assertTrue(bulk.startswith(b"$"))
                    value = await reader.readexactly(int(bulk[1:-2]) + 2)
                    self.assertEqual(value[-2:], b"\r\n")
                    values.append(value[:-2])
                if values[0] == b"PING":
                    reply = b"+PONG\r\n"
                elif values[0] == b"ECHO":
                    reply = b"$%d\r\n" % len(values[1]) + values[1] + b"\r\n"
                elif values[0] == b"EVAL":
                    self.applied += 1
                    reply = b":%d\r\n" % self.applied
                else:
                    reply = b"-ERR unsupported\r\n"
                writer.write(reply)
                await writer.drain()
                self.returned.append((values[0], reply))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self.fake_tasks.discard(task)

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        self.client_writers.append(writer)
        return reader, writer

    async def send(self, writer, *values):
        writer.write(request(*values))
        await writer.drain()

    async def receive(self, reader):
        return await asyncio.wait_for(reader.readuntil(b"\r\n"), 1)

    async def test_pass_preserves_binary_bulk_response(self):
        reader, writer = await self.connect()
        value = b"binary\x00\xff\r\nvalue"
        await self.send(writer, "ECHO", value)
        expected = b"$%d\r\n" % len(value) + value + b"\r\n"
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(expected)), 1), expected)
        stats = self.proxy.stats()
        self.assertEqual(stats["upstream_request_bytes"], len(request("ECHO", value)))
        self.assertEqual(stats["upstream_reply_bytes"], len(expected))
        self.assertEqual(stats["lost_replies"], 0)

    async def test_drop_reply_follows_real_execution_and_is_one_shot(self):
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")
        await self.proxy.set_mode("drop-reply", command="EVAL")
        await self.send(writer, "PING")
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")
        self.assertEqual(self.proxy.mode, "drop-reply")
        await self.send(writer, "EVAL", "return 1", "0")
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        self.assertEqual(self.applied, 1)
        self.assertIn((b"EVAL", b":1\r\n"), self.returned)
        stats = self.proxy.stats()
        self.assertEqual(stats["lost_replies"], 1)
        self.assertEqual(stats["upstream_reply_bytes"], len(b"+PONG\r\n") * 2 + len(b":1\r\n"))
        dropped = [event for event in stats["events"] if event["event"] == "reply-dropped"]
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["command"], "EVAL")
        self.assertEqual(dropped[0]["reply_bytes"], len(b":1\r\n"))
        self.assertEqual(dropped[0]["reply_integer"], 1)
        self.assertEqual([event["reply_integer"] for event in stats["events"]
                          if event["event"] == "eval-reply"], [1])
        self.assertIsInstance(dropped[0]["at"], float)
        self.assertEqual(stats["mode"], "pass")
        reader, writer = await self.connect()
        await self.send(writer, "EVAL", "return 1", "0")
        self.assertEqual(await self.receive(reader), b":2\r\n")
        self.assertEqual(self.proxy.stats()["lost_replies"], 1)

    async def test_blackhole_blocks_then_recovery_requires_reconnect(self):
        old_reader, old_writer = await self.connect()
        await self.send(old_writer, "PING")
        self.assertEqual(await self.receive(old_reader), b"+PONG\r\n")
        await self.proxy.set_mode("blackhole")
        self.assertEqual(await asyncio.wait_for(old_reader.read(1), 1), b"")
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.05)
        self.assertEqual(self.upstream_connections, 1)
        stats = self.proxy.stats()
        self.assertEqual(stats["blocked_connections"], 1)
        self.assertEqual(stats["blocked_bytes"], len(request("PING")))
        await self.proxy.set_mode("pass")
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")

    async def test_pipeline_filter_preserves_prior_reply(self):
        reader, writer = await self.connect()
        await self.proxy.set_mode("drop-reply", command="EVAL")
        writer.write(request("PING") + request("EVAL", "return 1", "0"))
        await writer.drain()
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        self.assertEqual(self.applied, 1)
        self.assertEqual(self.proxy.stats()["lost_replies"], 1)
        dropped = [event for event in self.proxy.stats()["events"] if event["event"] == "reply-dropped"]
        self.assertEqual(dropped[0]["reply_type"], ":")

    async def test_refuse_closes_old_and_new_connections(self):
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")
        await self.proxy.set_mode("refuse")
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        self.assertEqual(self.upstream_connections, 1)
        self.assertEqual(self.proxy.stats()["refused_connections"], 2)
        await self.proxy.set_mode("pass")
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        self.assertEqual(await self.receive(reader), b"+PONG\r\n")

    async def test_close_cancels_blackhole_connections_and_is_idempotent(self):
        await self.proxy.set_mode("blackhole")
        reader, writer = await self.connect()
        await self.send(writer, "PING")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.05)
        await self.proxy.close()
        self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b"")
        self.assertEqual(len(self.proxy._connections), 0)
        await self.proxy.close()


if __name__ == "__main__":
    unittest.main()
