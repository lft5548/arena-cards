"""Matching cancellation, duplicate joins and rematching across application modules."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
from protocol import read_message, send_message, negotiate_protocol
from reconnect_test import wait_for_server


class Peer:
    def __init__(self, port: int, protocol: str):
        self.port = port
        self.protocol = protocol
        self.writer = None

    async def connect(self, name: str) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        if self.protocol == "proto_v1":
            await negotiate_protocol(self.reader, self.writer, self.protocol)
        await self.send("LoginReq", name)
        login = await self.receive("LoginResp")
        if login.get("ok") != "1":
            raise AssertionError("login failed")
        self.token = login["token"]

    async def send(self, kind: str, payload="") -> None:
        await send_message(self.writer, kind, payload, protocol=self.protocol)

    async def receive(self, kind: str) -> dict:
        message = await asyncio.wait_for(read_message(self.reader, protocol=self.protocol), 3)
        if message["type"] != kind:
            raise AssertionError(f"expected {kind}, got {message}")
        return message["payload"]

    async def join(self) -> None:
        await self.send("MatchJoinReq")
        if (await self.receive("MatchJoinReq")).get("queued") != "1":
            raise AssertionError("join receipt missing")

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.writer = None


async def metrics(peer: Peer) -> dict:
    await peer.send("AdminRoomsReq")
    return await peer.receive("AdminRoomsResp")


async def initial_match(first: Peer, second: Peer) -> str:
    found_first = await first.receive("MatchFound")
    found_second = await second.receive("MatchFound")
    snapshot_first = await first.receive("BattleSnapshot")
    snapshot_second = await second.receive("BattleSnapshot")
    if (found_first["match_id"] != found_second["match_id"] or
            snapshot_first["match_id"] != found_first["match_id"] or
            snapshot_second["match_id"] != found_first["match_id"] or
            {snapshot_first["player_index"], snapshot_second["player_index"]} != {"0", "1"}):
        raise AssertionError("matching modules disagreed on room assignment")
    first.snapshot, second.snapshot = snapshot_first, snapshot_second
    return found_first["match_id"]


async def finish_match(first: Peer, second: Peer) -> None:
    peers = {int(first.snapshot["player_index"]): first, int(second.snapshot["player_index"]): second}
    for turn_id in range(1, 41):
        actor = peers[(turn_id - 1) % 2]
        await actor.send("EndTurnReq", {"match_id": actor.snapshot["match_id"],
                                       "turn_id": turn_id, "action_id": (turn_id + 1) // 2})
        await actor.receive("ActionAck")
        for peer in (first, second):
            await peer.receive("BattleEvent")
            if turn_id < 40:
                peer.snapshot = await peer.receive("BattleSnapshot")
            else:
                result = await peer.receive("MatchResult")
                if result.get("winner") != "-1" or result.get("reason") != "max_turns":
                    raise AssertionError("matching-only battle changed game rules")


async def run(server: str, port: int) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        environment = os.environ.copy()
        environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0", ARENA_REDIS_ENABLED="0",
                           ARENA_REPLAY_DIR=temporary)
        process = subprocess.Popen([server, str(port)], cwd=ROOT, env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        first, second, third = Peer(port, "text_v1"), Peer(port, "proto_v1"), Peer(port, "text_v1")
        peers = (first, second, third)
        try:
            await wait_for_server(process, port)
            for index, peer in enumerate(peers):
                await peer.connect("matching-" + str(index))
            await first.join()
            await first.join()
            await first.send("MatchCancelReq")
            if (await first.receive("MatchCancelReq")).get("cancelled") != "1":
                raise AssertionError("cancel receipt missing")
            await second.join()
            await first.send("Heartbeat")
            await first.receive("Pong")
            if (await metrics(first)).get("active_rooms") != "0":
                raise AssertionError("cancelled or duplicate queue entry formed a room")
            await third.join()
            match_id = await initial_match(second, third)
            await first.send("Heartbeat")
            await first.receive("Pong")
            await second.send("MatchJoinReq")
            if (await second.receive("Error")).get("code") != "already_in_room":
                raise AssertionError("matched session reentered the queue")
            await finish_match(second, third)
            if (await metrics(first)).get("active_rooms") != "0":
                raise AssertionError("finished room remained active")
            await second.join()
            await first.join()
            new_match_id = await initial_match(second, first)
            if new_match_id == match_id:
                raise AssertionError("rematch reused a finished room")
            await finish_match(second, first)
            await third.join()
            await third.close()
            deadline = asyncio.get_running_loop().time() + 3
            while True:
                report = await metrics(first)
                if report.get("active_sessions") == "2":
                    break
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("disconnected queued session retained")
                await asyncio.sleep(0.01)
            await first.join()
            await first.send("Heartbeat")
            await first.receive("Pong")
            if (await metrics(first)).get("active_rooms") != "0":
                raise AssertionError("closed queue entry was paired with a new join")
            await second.join()
            await initial_match(first, second)
            if (await metrics(first)).get("active_rooms") != "1":
                raise AssertionError("duplicate match creation")
            print("matching cancel/duplicate/finished-room rematch/queued-disconnect/mixed protocols passed")
        finally:
            for peer in peers:
                await peer.close()
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19158)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))


if __name__ == "__main__":
    main()
