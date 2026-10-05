"""Bounded socket heartbeat and POSIX graceful-stop acceptance."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import make_message, make_proto_message, negotiate_protocol, read_message, send_message
from network_limits_test import Server, close, connect, environment, exchange

HEARTBEAT_MS = 600


async def eof(reader, timeout=1.3):
    try:
        while await asyncio.wait_for(reader.read(65536), timeout):
            pass
    except (ConnectionResetError, BrokenPipeError):
        pass


async def peer(port, protocol):
    connection = await connect(port)
    if protocol == "proto_v1":
        await negotiate_protocol(*connection, protocol)
    return connection


async def timeout_cases(server, protocol):
    peers = []
    try:
        # All transports, including ones that have never logged in, expire.
        peers = [await peer(server.port, protocol) for _ in range(4)]
        frame = make_message("Heartbeat") if protocol == "text_v1" else make_proto_message("Heartbeat", b"")
        peers[1][1].write(frame[:2])                 # incomplete length header
        peers[2][1].write((100).to_bytes(4, "big") + frame[4:5])  # incomplete body
        peers[3][1].write((100).to_bytes(4, "big"))
        await asyncio.gather(*(writer.drain() for _, writer in peers))
        started = time.monotonic()
        async def trickle():
            try:
                for _ in range(12):
                    await asyncio.sleep(0.08)
                    peers[3][1].write(b"x")
                    await peers[3][1].drain()
            except (ConnectionError, OSError):
                pass
        trickling = asyncio.create_task(trickle())
        try:
            await asyncio.wait_for(asyncio.gather(*(eof(reader) for reader, _ in peers)), 1.3)
        finally:
            trickling.cancel()
            await asyncio.gather(trickling, return_exceptions=True)
        elapsed = time.monotonic() - started
        if elapsed > 1.3:
            raise AssertionError("partial/trickled bytes extended heartbeat lifetime")
        print(f"{protocol}: idle/pre-login, partial header/body, trickle expired in {elapsed:.3f}s")
    finally:
        await asyncio.gather(*(close(writer) for _, writer in peers))


async def activity_and_invalid(server, protocol):
    good = await peer(server.port, protocol)
    bad = await peer(server.port, protocol)
    try:
        invalid = make_message(999, "invalid") if protocol == "text_v1" else make_proto_message("Heartbeat", b"\xff")
        started = time.monotonic()
        async def invalid_requests():
            try:
                for _ in range(12):
                    bad[1].write(invalid)
                    await bad[1].drain()
                    await asyncio.sleep(0.08)
            except (ConnectionError, OSError):
                pass
        noisy = asyncio.create_task(invalid_requests())
        try:
            await asyncio.wait_for(eof(bad[0]), 1.3)
        finally:
            noisy.cancel()
            await asyncio.gather(noisy, return_exceptions=True)
        if time.monotonic() - started > 1.3:
            raise AssertionError("invalid frames or outbound errors refreshed heartbeat")
        # good was idle while validating bad; open a fresh connection to verify
        # genuine full frames keep it alive through several complete deadlines.
        await close(good[1])
        good = await peer(server.port, protocol)
        for index in range(5):
            await asyncio.sleep(0.3)
            kind, response = ("Heartbeat", "Pong") if index % 2 == 0 else ("AdminRoomsReq", "AdminRoomsResp")
            await exchange(good, kind, response, protocol)
        await eof(good[0])
        print(f"{protocol}: Heartbeat/Admin inbound reset; invalid input/outbound errors did not reset")
    finally:
        await close(good[1])
        await close(bad[1])


async def unsolicited_output(server, protocol):
    first = await peer(server.port, protocol)
    second = None
    try:
        await exchange(first, "LoginReq", "LoginResp", protocol, "outbound-first-" + protocol)
        await exchange(first, "MatchJoinReq", "MatchJoinReq", protocol)
        last_input = time.monotonic()
        await asyncio.sleep(0.45)
        second = await peer(server.port, protocol)
        await exchange(second, "LoginReq", "LoginResp", protocol, "outbound-second-" + protocol)
        await send_message(second[1], "MatchJoinReq", "", protocol=protocol)
        message = await asyncio.wait_for(read_message(first[0], protocol=protocol), 0.15)
        if message["type"] != "MatchFound":
            raise AssertionError("did not exercise unsolicited outbound match notification")
        await asyncio.wait_for(eof(first[0]), max(0.01, last_input + 0.85 - time.monotonic()))
        if time.monotonic() - last_input > 0.85:
            raise AssertionError("unsolicited MatchFound/snapshot refreshed inbound deadline")
        print(f"{protocol}: unsolicited MatchFound/BattleSnapshot did not reset heartbeat")
    finally:
        await close(first[1])
        if second:
            await close(second[1])


async def admission_release(executable, port):
    server = Server(executable, port, ARENA_MAX_CONNECTIONS="1", ARENA_HEARTBEAT_TIMEOUT_MS=str(HEARTBEAT_MS))
    first = await server.first_peer()
    replacement = None
    try:
        rejected = await connect(port)
        await eof(rejected[0])
        await close(rejected[1])
        await eof(first[0])
        replacement = await connect(port)
        current = await exchange(replacement, "AdminRoomsReq", "AdminRoomsResp")
        if (current.get("active_sessions") != "1" or int(current.get("heartbeat_timeouts", "0")) < 1 or
                int(current.get("connections_rejected", "0")) < 1):
            raise AssertionError("heartbeat close did not release admission slot/count timeout: " + str(current))
        print("heartbeat expiry released the only admission slot and preserved metrics")
    finally:
        await close(first[1])
        if replacement:
            await close(replacement[1])
        server.stop()


async def graceful_signals(executable, port):
    if os.name == "nt":
        print("POSIX SIGTERM/SIGINT acceptance: Linux-only; Windows process termination is not counted as graceful")
        return
    for offset, signum in enumerate((signal.SIGTERM, signal.SIGINT)):
        server = Server(executable, port + offset, ARENA_HEARTBEAT_TIMEOUT_MS="30000", ARENA_SHUTDOWN_TIMEOUT_MS="1000")
        first = await server.first_peer()
        partial = await connect(server.port)
        partial[1].write(b"\x00")
        await partial[1].drain()
        try:
            await exchange(first, "Heartbeat", "Pong")
            started = time.monotonic()
            server.process.send_signal(signum)
            code = await asyncio.to_thread(server.process.wait, 2)
            if code != 0 or time.monotonic() - started > 2:
                raise AssertionError(f"{signum.name} did not exit gracefully/bounded: {code}")
            await asyncio.gather(eof(first[0]), eof(partial[0]))
            with contextlib.suppress(ConnectionRefusedError):
                unexpected = await connect(server.port)
                await close(unexpected[1])
                raise AssertionError("listener accepted after graceful shutdown")
            print(f"{signum.name}: listener and idle/partial peers closed, exit 0")
        finally:
            await close(first[1])
            await close(partial[1])
            server.stop()


def invalid_configuration(executable, port):
    for name, values in {
        "ARENA_HEARTBEAT_TIMEOUT_MS": ("0", "99", "300001", "-1", "6x"),
        "ARENA_SHUTDOWN_TIMEOUT_MS": ("0", "99", "60001", "-1", "6x"),
    }.items():
        for value in values:
            result = subprocess.run([executable, str(port)], cwd=ROOT, env=environment(**{name: value}),
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode != 2 or name not in result.stderr:
                raise AssertionError(f"{name}={value}: configuration not rejected: {result.returncode} {result.stderr}")


async def run(args):
    server = Server(args.server, args.port, ARENA_HEARTBEAT_TIMEOUT_MS=str(HEARTBEAT_MS), ARENA_MAX_CONNECTIONS="16")
    initial = await server.first_peer()
    await close(initial[1])
    try:
        for protocol in ("text_v1",) if args.text_only else ("text_v1", "proto_v1"):
            await timeout_cases(server, protocol)
            await activity_and_invalid(server, protocol)
            await unsolicited_output(server, protocol)
    finally:
        server.stop()
    await admission_release(args.server, args.port + 1)
    await graceful_signals(args.server, args.port + 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19170)
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()
    args.server = os.path.abspath(args.server)
    invalid_configuration(args.server, args.port)
    asyncio.run(asyncio.wait_for(run(args), 25))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
