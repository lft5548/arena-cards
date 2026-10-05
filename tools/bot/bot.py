"""Deterministic asyncio players for smoke tests and pressure runs."""
from __future__ import annotations
import argparse, asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client_pygame"))
from protocol import negotiate_protocol, read_message, send_message, payload_int
from state import ClientState, choose_card

class Bot:
    def __init__(self, host, port, name, timeout=45, protocol="text_v1"):
        if protocol not in ("text_v1", "proto_v1"):
            raise ValueError(f"unknown Bot protocol: {protocol}")
        self.host, self.port, self.name, self.timeout = host, port, name, timeout
        self.protocol = protocol
        self.selected_protocol = "text_v1" if protocol == "text_v1" else "unnegotiated"
        self.protocol_messages = {"text_v1": 0, "proto_v1": 0}
        self.messages = 0; self.actions = 0; self.errors = []; self.revision = -1
        self.state = ClientState(); self.writer = None

    async def _read(self, reader):
        message = await read_message(reader, protocol=self.protocol)
        mode = "proto_v1" if message.get("protocol") == "ProtoV1" else "text_v1"
        self.protocol_messages[mode] += 1
        return message

    async def _send(self, message_type, payload=None):
        await send_message(self.writer, message_type, payload, protocol=self.protocol)

    def _proof(self):
        return {"protocol": self.protocol, "selected_protocol": self.selected_protocol,
                "protocol_messages": dict(self.protocol_messages),
                "match_id": self.state.match_id}

    async def run_round(self, number):
        self.state = ClientState(); self.revision = -1
        self.selected_protocol = "text_v1" if self.protocol == "text_v1" else "unnegotiated"
        reader = None; heartbeat = None
        try:
            reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.state.apply({"type": "Connected", "payload": {}})
            capabilities = await negotiate_protocol(reader, self.writer, self.protocol)
            self.selected_protocol = capabilities["selected"]
            login_payload = self.name if self.protocol == "text_v1" else {
                "user": self.name, "request_id": f"{self.name}-{number}-login"}
            await self._send("LoginReq", login_payload)
            login = await asyncio.wait_for(self._read(reader), 5)
            self.messages += 1; self.state.apply(login)
            if login.get("type") != "LoginResp" or login.get("payload", {}).get("ok") != "1":
                raise RuntimeError("login failed")
            await self._send("MatchJoinReq", {"request_id": f"{self.name}-{number}"})
            heartbeat = asyncio.create_task(self._heartbeat())
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                left = max(0.1, deadline - time.monotonic())
                message = await asyncio.wait_for(self._read(reader), min(left, 5.0))
                self.messages += 1
                message_type = message.get("type"); data = message.get("payload", {})
                accepted = self.state.apply(message)
                if message_type == "Error":
                    self.errors.append(data.get("code", "server_error"))
                    raise RuntimeError(self.errors[-1])
                if message_type == "BattleSnapshot" and accepted:
                    revision = payload_int(data, "revision", -1)
                    if revision <= self.revision: continue
                    self.revision = revision
                    await self.play_if_turn()
                elif message_type == "MatchResult":
                    if not self.state.result: raise RuntimeError("malformed result")
                    return {"round": number, "completed": True, "winner": data.get("winner"),
                            "reason": data.get("reason", ""), "messages": self.messages,
                            "actions": self.actions, "errors": self.errors, **self._proof()}
            raise TimeoutError("round timeout")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
            self.errors.append(type(error).__name__); return self._failed(number, str(error))
        except (OSError, ValueError, RuntimeError, ImportError) as error:
            self.errors.append(str(error)); return self._failed(number, str(error))
        finally:
            if heartbeat:
                heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
            if self.writer:
                self.writer.close(); await self.writer.wait_closed()

    async def _heartbeat(self):
        while self.writer and not self.writer.is_closing():
            await asyncio.sleep(5)
            try: await self._send("Heartbeat", "")
            except (ConnectionError, OSError): return

    async def play_if_turn(self):
        view = self.state.view(); snapshot = view["snapshot"]
        if not snapshot or snapshot["done"] or snapshot["turn"] != view["player_index"]: return
        try:
            slot = choose_card(snapshot, view["player_index"])
            message_type, payload = self.state.reserve_action(slot)
        except ValueError:
            return
        if self.protocol == "proto_v1":
            payload["revision"] = snapshot["revision"]
            payload["request_id"] = f"{self.name}-{self.state.match_id}-{payload['action_id']}"
        await self._send(message_type, payload); self.actions += 1

    def _failed(self, number, detail):
        return {"round": number, "completed": False, "error": detail, "winner": None,
                "reason": "", "messages": self.messages, "actions": self.actions,
                "errors": self.errors, **self._proof()}

def bot_protocol(mode, index):
    if mode == "mixed":
        return "text_v1" if index % 2 == 0 else "proto_v1"
    if mode not in ("text_v1", "proto_v1"):
        raise ValueError(f"unknown Bot protocol mode: {mode}")
    return mode

async def one_bot(args, index):
    protocol = bot_protocol(getattr(args, "protocol", "text_v1"), index)
    bot = Bot(args.host, args.port, f"bot{index+1}", args.timeout, protocol)
    rounds = []
    for number in range(1, args.rounds + 1):
        result = await bot.run_round(number); rounds.append(result)
        if not result["completed"]: break
        await asyncio.sleep(0.02)
    return {"bot": bot.name, "protocol": protocol, "rounds": rounds,
            "completed_rounds": sum(1 for result in rounds if result["completed"]),
            "errors": sum(len(result["errors"]) for result in rounds)}

async def main_async(args):
    mode = getattr(args, "protocol", "text_v1")
    results = await asyncio.gather(*(one_bot(args, index) for index in range(args.count)), return_exceptions=True)
    output = {"count": args.count, "rounds": args.rounds, "protocol": mode,
              "protocol_counts": {"text_v1": 0, "proto_v1": 0}, "results": []}
    for index, result in enumerate(results):
        protocol = bot_protocol(mode, index)
        output["protocol_counts"][protocol] += 1
        output["results"].append(result if isinstance(result, dict) else {
            "bot": f"bot{index+1}", "protocol": protocol,
            "completed_rounds": 0, "errors": 1, "error": str(result)})
    output["ok"] = all(result.get("completed_rounds") == args.rounds and not result.get("errors")
                       for result in output["results"])
    return output

def main():
    parser = argparse.ArgumentParser(description="Arena Cards bot and pressure client")
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--count", type=int, default=2); parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1", "mixed"), default="text_v1")
    args = parser.parse_args()
    if args.count <= 0 or args.count % 2 or args.rounds <= 0 or args.timeout <= 0:
        parser.error("count must be positive and even; rounds and timeout must be positive")
    result = asyncio.run(main_async(args)); print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1
if __name__ == "__main__": raise SystemExit(main())
