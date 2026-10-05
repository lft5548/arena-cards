"""Exercise deterministic statuses through TextV1, reconnect, and persisted replays."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client_pygame"))
sys.path.insert(0, str(ROOT / "tools" / "replay"))
from protocol import read_message, send_message, negotiate_protocol
from reconnect_test import wait_for_server
import replay_battle as replay


class MatchProbe:
    def __init__(self, server: str, port: int, directory: Path, rows: str,
                 protocols: tuple[str, str] = ("text_v1", "text_v1")):
        self.server, self.port, self.directory = server, port, directory
        self.protocols = protocols
        self.config = directory / "cards.csv"
        directory.mkdir()
        self.config.write_text("id,name,cost,effect,value,duration\n" + rows, encoding="utf-8")
        self.peers = []
        self.action_ids = [0, 0]
        self.requests = [None, None]
        self.results = []
        self.process = None

    async def __aenter__(self):
        environment = os.environ.copy()
        environment.update(ARENA_MYSQL_ENABLED="0", ARENA_MYSQL_REQUIRED="0",
                           ARENA_REDIS_ENABLED="0", ARENA_REPLAY_DIR=str(self.directory / "replays"))
        if self.server:
            self.process = subprocess.Popen([self.server, str(self.port), str(self.config)], cwd=ROOT,
                                            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if self.process is not None:
                await wait_for_server(self.process, self.port)
            suffix = uuid.uuid4().hex[:12]
            for index, name in enumerate(("rules_a_" + suffix, "rules_b_" + suffix)):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                peer = {"reader": reader, "writer": writer, "events": [], "snapshot": {},
                        "protocol": self.protocols[index]}
                self.peers.append(peer)
                if peer["protocol"] == "proto_v1":
                    await negotiate_protocol(reader, writer, "proto_v1")
                await send_message(writer, "LoginReq", name, protocol=peer["protocol"])
                login = await self.read_until(peer, "LoginResp")
                peer["token"] = login["token"]
                await send_message(writer, "MatchJoinReq", "", protocol=peer["protocol"])
            for peer in self.peers:
                await self.read_until(peer, "MatchFound")
                await self.read_until(peer, "BattleSnapshot")
            self.peers.sort(key=lambda peer: int(peer["snapshot"]["player_index"]))
            self.match_id = self.peers[0]["snapshot"]["match_id"]
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        for peer in self.peers:
            peer["writer"].close()
            await peer["writer"].wait_closed()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    async def read_until(self, peer: dict, message_type: str) -> dict:
        while True:
            message = await asyncio.wait_for(read_message(peer["reader"], protocol=peer["protocol"]), 3)
            payload = message["payload"]
            if message["type"] == "BattleEvent":
                peer["events"].append({key: value for key, value in payload.items()
                                       if key not in {"revision", "request_id"}})
            elif message["type"] == "BattleSnapshot":
                peer["snapshot"] = payload
            elif message["type"] == "Error" and message_type != "Error":
                raise AssertionError(f"unexpected server error: {payload}")
            if message["type"] == message_type:
                return payload

    @property
    def turn_id(self) -> int:
        return int(self.peers[0]["snapshot"]["turn_id"])

    async def action(self, card: int | None = None, terminal: bool = False) -> list[dict]:
        player = int(self.peers[0]["snapshot"]["turn"])
        self.action_ids[player] += 1
        request = {"match_id": self.match_id, "turn_id": self.turn_id,
                   "action_id": self.action_ids[player]}
        message_type = "EndTurnReq" if card is None else "PlayCardReq"
        if card is not None:
            request["card"] = card
        before = len(self.peers[0]["events"])
        await send_message(self.peers[player]["writer"], message_type, request,
                           protocol=self.peers[player]["protocol"])
        ack = await self.read_until(self.peers[player], "ActionAck")
        self.requests[player] = (message_type, request, ack)
        for peer in self.peers:
            payload = await self.read_until(peer, "MatchResult" if terminal else "BattleSnapshot")
            if terminal:
                self.results.append(payload)
        if self.peers[0]["events"] != self.peers[1]["events"]:
            raise AssertionError("players observed different event order")
        return self.peers[0]["events"][before:]

    def check(self, **expected) -> None:
        for peer in self.peers:
            for key, value in expected.items():
                actual = int(peer["snapshot"][key])
                if actual != value:
                    raise AssertionError(f"snapshot {key}: expected {value}, got {actual}")

    async def retry(self, player: int, conflict: bool = False) -> None:
        message_type, request, original_ack = self.requests[player]
        request = dict(request)
        if conflict:
            request["card"] = 2 if request.get("card") == 1 else 1
        counts = [len(peer["events"]) for peer in self.peers]
        revision = self.peers[0]["snapshot"]["replay_revision"]
        await send_message(self.peers[player]["writer"], message_type, request,
                           protocol=self.peers[player]["protocol"])
        response = await self.read_until(self.peers[player], "Error" if conflict else "ActionAck")
        if conflict:
            if response.get("code") != "action_id_conflict":
                raise AssertionError(f"wrong conflict response: {response}")
        elif response != original_ack:
            raise AssertionError("retry changed the successful action receipt")
        for peer in self.peers:
            await send_message(peer["writer"], "Heartbeat", "", protocol=peer["protocol"])
            await self.read_until(peer, "Pong")
        if counts != [len(peer["events"]) for peer in self.peers] or \
                self.peers[0]["snapshot"]["replay_revision"] != revision:
            raise AssertionError("retry reapplied a status or added an event")

    async def reconnect(self, player: int) -> None:
        peer = self.peers[player]
        before = dict(peer["snapshot"])
        peer["writer"].close()
        await peer["writer"].wait_closed()
        await asyncio.sleep(0.05)
        peer["reader"], peer["writer"] = await asyncio.open_connection("127.0.0.1", self.port)
        if peer["protocol"] == "proto_v1":
            await negotiate_protocol(peer["reader"], peer["writer"], "proto_v1")
        await send_message(peer["writer"], "ReconnectReq", peer["token"], protocol=peer["protocol"])
        response_seen = snapshot_seen = False
        while not (response_seen and snapshot_seen):
            message = await asyncio.wait_for(read_message(peer["reader"], protocol=peer["protocol"]), 3)
            if message["type"] == "ReconnectResp":
                if message["payload"].get("ok") != "1":
                    raise AssertionError(f"reconnect rejected: {message}")
                response_seen = True
            elif message["type"] == "BattleSnapshot":
                peer["snapshot"] = message["payload"]
                snapshot_seen = True
            else:
                raise AssertionError(f"unexpected reconnect message: {message}")
        await self.read_until(self.peers[1 - player], "BattleSnapshot")
        for key in before:
            if key not in {"remaining_ms", "action_id", "request_id"} and peer["snapshot"].get(key) != before[key]:
                raise AssertionError(f"reconnect changed {key}")

    async def verify_replay(self) -> tuple[dict, list[replay.Event]]:
        path = self.directory / "replays" / (self.match_id + ".replay")
        deadline = asyncio.get_running_loop().time() + 3
        while not path.is_file() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        match_id, seed, expected_digest, events = replay.load_replay(path)
        for result in self.results:
            if (result.get("replay_valid") != "1" or int(result["replay_seed"]) != seed or
                    int(result["replay_revision"]) != len(events) or
                    int(result["replay_digest"]) != expected_digest):
                raise AssertionError("persisted replay disagrees with MatchResult")
        recorded = [replay.parse_fields(event.payload) for event in events if event.kind == "battle_event"]
        if recorded != self.peers[0]["events"]:
            raise AssertionError("persisted replay disagrees with TextV1 event stream")
        state = replay.reconstruct(events, replay.load_cards(self.config), match_id)
        if any(state["winner"] != int(result["winner"]) for result in self.results):
            raise AssertionError("reconstructed winner disagrees with MatchResult")
        return state, events

    def compare_final_snapshot(self, state: dict) -> None:
        for peer in self.peers:
            snapshot = peer["snapshot"]
            for player, rebuilt in enumerate(state["players"]):
                for field in ("hp", "energy", "shield"):
                    if rebuilt[field] != int(snapshot[f"p{player}_{field}"]):
                        raise AssertionError(f"replay {field} differs from live player {player}")
                for kind in rebuilt["statuses"]:
                    for field in ("value", "turns"):
                        if rebuilt["statuses"][kind][field] != int(snapshot[f"p{player}_{kind}_{field}"]):
                            raise AssertionError("replay status differs from live snapshot")
                for kind, bonus in rebuilt.get("boosts", {}).items():
                    for field in ("value", "uses"):
                        if bonus[field] != int(snapshot[f"p{player}_{kind}_{field}"]):
                            raise AssertionError("replay bonus differs from live snapshot")
            viewer = int(snapshot["player_index"])
            hand = [int(card) for card in snapshot["hand"].split(",") if card]
            if hand != state["players"][viewer]["hand"] or \
                    int(snapshot["deck_count"]) != len(state["players"][viewer]["deck"]):
                raise AssertionError("replay private cards differ from live snapshot")
            discarded = [int(card) for card in snapshot.get("discard", "").split(",") if card]
            if (discarded != state["players"][viewer]["discard"] or
                    int(snapshot["discard_count"]) != len(discarded) or
                    int(snapshot.get("opponent_discard_count", "0")) !=
                    len(state["players"][1 - viewer]["discard"])):
                raise AssertionError("replay discard history differs from live snapshot")


async def run(server: str, port: int) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        rows = "# deterministic status regression\n1,Venom,01,poison,+004,+02\n2,Renew,1,regen,3,2\n3,Barrier,1,shield,6,0\n"
        async with MatchProbe(server, port, directory / "normal", rows) as match:
            await match.action(2)
            await match.action()
            match.check(turn_id=3, p0_hp=30, p0_regen_turns=1)
            await send_message(match.peers[1]["writer"], "PlayCardReq", {
                "match_id": match.match_id, "turn_id": 3, "action_id": 99, "card": 1})
            error = await match.read_until(match.peers[1], "Error")
            if error.get("code") != "not_your_turn":
                raise AssertionError(f"invalid status action accepted: {error}")
            await match.action(3)
            await match.action(1)
            match.check(turn_id=5, p0_hp=29, p0_shield=6, p0_poison_turns=1, p0_regen_value=0)
            await match.action(2)
            await match.action(1)
            match.check(turn_id=7, p0_hp=28, p0_poison_turns=1, p0_regen_turns=1)
            await match.retry(1)
            await match.retry(1, conflict=True)
            await match.reconnect(0)
            await match.retry(0)
            match.check(turn_id=7, p0_hp=28, p0_poison_turns=1, p0_regen_turns=1)
            await match.action()
            await match.action()
            match.check(turn_id=9, p0_hp=27, p0_shield=6, p0_poison_value=0,
                        p0_poison_turns=0, p0_regen_value=0, p0_regen_turns=0)
            while match.turn_id < 40:
                await match.action()
            await match.action(terminal=True)
            state, events = await match.verify_replay()
            match.compare_final_snapshot(state)
            if state["winner"] != 1 or len(events) != 49:
                raise AssertionError(f"unexpected finite-status terminal state: {state}")
            print("status refresh/expiry/idempotency/reconnect/replay passed; revision=49")

        rows = "1,Barrier,1,shield,6,0\n2,Renew,1,regen,30,2\n3,Venom,1,poison,30,2\n"
        async with MatchProbe(server, port + 1, directory / "lethal", rows) as match:
            await match.action(1)
            await match.action()
            await match.action(2)
            observed = await match.action(3, terminal=True)
            if [event["type"] for event in observed] != ["poison", "status_tick"]:
                raise AssertionError("regen triggered after lethal poison")
            state, _ = await match.verify_replay()
            dead = state["players"][0]
            if (state["winner"] != 1 or state["reason"] != "poison" or state["turn_id"] != 5 or
                    dead["hp"] != 0 or dead["shield"] != 6 or dead["energy"] != 3 or
                    dead["statuses"]["regen"] != {"value": 30, "turns": 2}):
                raise AssertionError(f"invalid poison knockout: {state}")
            print("lethal poison/shield bypass/regen stop/replay passed; revision=7")

        rows = "1,Venom,1,poison,30,5\n2,Renew,1,regen,3,2\n3,Barrier,1,shield,6,0\n"
        async with MatchProbe(server, port + 2, directory / "max-turn", rows) as match:
            while match.turn_id < 40:
                await match.action()
            await match.action(1, terminal=True)
            state, events = await match.verify_replay()
            if (state["winner"] != -1 or state["reason"] != "max_turns" or len(events) != 42 or
                    state["players"][0]["hp"] != 30 or
                    state["players"][0]["statuses"]["poison"] != {"value": 30, "turns": 5} or
                    any(event["type"] == "status_tick" for event in match.peers[0]["events"])):
                raise AssertionError(f"turn 41 triggered a status: {state}")
            print("max-turn cutoff before status trigger/replay passed; revision=42")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19113)
    args = parser.parse_args()
    asyncio.run(run(os.path.abspath(args.server), args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
