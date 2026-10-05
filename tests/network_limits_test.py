"""Bounded acceptance for connection admission and per-connection request budgets."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import (make_message, make_proto_message, negotiate_protocol,
                      read_message, send_message)


def environment(**limits: str) -> dict[str, str]:
    values = os.environ.copy()
    values.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0",
                  ARENA_REDIS_ENABLED="0", ARENA_REDIS_REQUIRED="0",
                  ARENA_ROOM_RECOVERY="0", ARENA_MAX_CONNECTIONS="8",
                  ARENA_REQUEST_RATE_PER_SECOND="200", ARENA_REQUEST_BURST="400")
    values.update(limits)
    return values


async def connect(port: int):
    return await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)


async def close(writer) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1)


async def exchange(peer, kind: str, expected: str, protocol="text_v1", payload=""):
    reader, writer = peer
    await asyncio.wait_for(send_message(writer, kind, payload, protocol=protocol), 2)
    message = await asyncio.wait_for(read_message(reader, protocol=protocol), 2)
    if message["type"] != expected:
        raise AssertionError(f"{protocol} {kind}: expected {expected}, received {message}")
    return message["payload"]


async def disconnected(reader, *, no_data=False) -> None:
    """Overload may purge responses already queued, so only EOF is mandatory."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), 2)
            if not data:
                return
            if no_data:
                raise AssertionError("rejected connection received application bytes")
    except ConnectionResetError:
        return


async def metrics(port: int) -> dict[str, str]:
    peer = await connect(port)
    try:
        return await exchange(peer, "AdminRoomsReq", "AdminRoomsResp")
    finally:
        await close(peer[1])


class Server:
    def __init__(self, executable: str, port: int, **limits: str):
        self.port = port
        self.process = subprocess.Popen([executable, str(port)], cwd=ROOT,
                                        env=environment(**limits),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def first_peer(self):
        deadline = asyncio.get_running_loop().time() + 4
        while asyncio.get_running_loop().time() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"network-limits server exited: {self.process.returncode}")
            try:
                return await connect(self.port)
            except OSError:
                await asyncio.sleep(0.02)
        raise AssertionError("network-limits server did not start within four seconds")

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


async def connection_cap(executable: str, port: int, text_only: bool) -> None:
    server = Server(executable, port, ARENA_MAX_CONNECTIONS="3")
    peers = []
    try:
        peers.append(await server.first_peer())
        await exchange(peers[0], "Heartbeat", "Pong")
        peers.append(await connect(port))
        second_protocol = "text_v1" if text_only else "proto_v1"
        await negotiate_protocol(*peers[1], second_protocol)
        await exchange(peers[1], "Heartbeat", "Pong", second_protocol)
        peers.append(await connect(port))
        await exchange(peers[2], "Heartbeat", "Pong")
        # No login was sent: idle pre-login transports also consume admission slots.
        for _ in range(6):
            extra = await connect(port)
            try:
                await disconnected(extra[0], no_data=True)
            finally:
                await close(extra[1])
        payload = await exchange(peers[0], "AdminRoomsReq", "AdminRoomsResp")
        if payload.get("active_sessions") != "3" or int(payload.get("connections_rejected", "-1")) != 6:
            raise AssertionError(f"connection cap/rejections not visible in Admin: {payload}")
        binary = await exchange(peers[1], "AdminRoomsReq", "AdminRoomsResp", second_protocol)
        if binary.get("connections_rejected") != "6":
            raise AssertionError(f"{second_protocol} Admin dropped admission metrics: {binary}")

        async def release_slot():
            await close(peers.pop()[1])
            deadline = asyncio.get_running_loop().time() + 2
            while True:
                current = await exchange(peers[0], "AdminRoomsReq", "AdminRoomsResp")
                if current.get("active_sessions") == "2":
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(f"disconnected transport retained its slot: {current}")
                await asyncio.sleep(0.02)

        for index in range(6):
            await release_slot()
            peers.append(await connect(port))
            login = await exchange(peers[2], "LoginReq", "LoginResp", payload=f"cap-churn-{index}")
            if login.get("ok") != "1":
                raise AssertionError(f"new connection could not login after release: {login}")
            await exchange(peers[2], "Heartbeat", "Pong")
        print("connection cap: pre-login, six rejections, six slot releases/churn, Admin passed")
    finally:
        for _, writer in peers:
            await close(writer)
        server.stop()


async def rate_limits(executable: str, port: int, text_only: bool) -> None:
    server = Server(executable, port, ARENA_REQUEST_RATE_PER_SECOND="1", ARENA_REQUEST_BURST="3")
    limited = 0
    try:
        initial = await server.first_peer()
        await close(initial[1])
        for protocol in ("text_v1",) if text_only else ("text_v1", "proto_v1"):
            peer = await connect(port)
            try:
                # Explicit Hello is charged even when choosing the default TextV1.
                hello = await exchange(peer, "ProtocolHelloReq", "ProtocolHelloResp",
                                       payload={"protocol": protocol, "version": 1})
                if hello.get("selected") != protocol:
                    raise AssertionError(f"protocol selection failed: {hello}")
                await exchange(peer, "LoginReq", "LoginResp", protocol, "rate-client")
                await exchange(peer, "Heartbeat", "Pong", protocol)
                await asyncio.sleep(1.15)
                await exchange(peer, "AdminRoomsReq", "AdminRoomsResp", protocol)
                await send_message(peer[1], "Heartbeat", "", protocol=protocol)
                await disconnected(peer[0])
                limited += 1
            finally:
                await close(peer[1])
            current = await metrics(port)
            if int(current.get("requests_rate_limited", "-1")) != limited:
                raise AssertionError(f"Hello/login/heartbeat/Admin did not share the budget: {current}")

        # Invalid Hello and unnegotiated Proto frames must not bypass admission accounting.
        frames = [make_message("ProtocolHelloReq", "protocol=unsupported;version=1"),
                  make_proto_message("Heartbeat", b"\xff"),
                  make_message(999, "invalid")]
        if not text_only:
            frames.append(None)  # Negotiated malformed protobuf is charged before decoding.
        for frame in frames:
            noisy = await connect(port)
            good = await connect(port)
            try:
                if frame is None:
                    await negotiate_protocol(*noisy, "proto_v1")
                    frame = make_proto_message("Heartbeat", b"\xff")
                noisy[1].write(frame * 20)
                await asyncio.wait_for(noisy[1].drain(), 2)
                await exchange(good, "Heartbeat", "Pong")
                await disconnected(noisy[0])
                limited += 1
                current = await exchange(good, "AdminRoomsReq", "AdminRoomsResp")
                if int(current.get("requests_rate_limited", "-1")) != limited:
                    raise AssertionError(f"bad frames escaped budget or affected another connection: {current}")
            finally:
                await close(noisy[1])
                await close(good[1])
        # The replacement transport starts with its own full bucket after overload.
        replacement = await connect(port)
        try:
            await exchange(replacement, "LoginReq", "LoginResp", payload="rate-replacement")
            await exchange(replacement, "Heartbeat", "Pong")
            current = await exchange(replacement, "AdminRoomsReq", "AdminRoomsResp")
            if int(current.get("requests_rate_limited", "-1")) != limited:
                raise AssertionError("replacement transport inherited the exhausted request budget")
        finally:
            await close(replacement[1])
        print(f"request budget: {limited} overloads, refill, invalid-frame accounting, peer isolation, replacement passed")
    finally:
        server.stop()


def invalid_configuration(executable: str, port: int) -> None:
    cases = {
        "ARENA_MAX_CONNECTIONS": ("0", "4097", "-1", "+3", "3x", "3.5", " 3", "999999999999999999999"),
        "ARENA_REQUEST_RATE_PER_SECOND": ("0", "10001", "-1", "+3", "3x", "3.5", " 3", "999999999999999999999"),
        "ARENA_REQUEST_BURST": ("0", "20001", "-1", "+3", "3x", "3.5", " 3", "999999999999999999999"),
    }
    for name, values in cases.items():
        for value in values:
            result = subprocess.run([executable, str(port)], cwd=ROOT, env=environment(**{name: value}),
                                    capture_output=True, text=True, timeout=2, check=False)
            if result.returncode != 2 or name not in result.stderr:
                raise AssertionError(f"{name}={value!r} was not rejected explicitly: {result.returncode} {result.stderr!r}")
    print("network configuration: 24 malformed/out-of-range values rejected at startup")


async def run(args) -> None:
    await connection_cap(args.server, args.port, args.text_only)
    await rate_limits(args.server, args.port + 1, args.text_only)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19166)
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()
    args.server = os.path.abspath(args.server)
    invalid_configuration(args.server, args.port)
    asyncio.run(asyncio.wait_for(run(args), 25))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
