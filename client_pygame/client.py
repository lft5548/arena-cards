"""Thin card-game client; pygame is optional and CLI is suitable for CI."""
from __future__ import annotations
import argparse, asyncio, json, queue, threading
try:
    from .protocol import read_message, send_message
    from .state import CARDS, ClientState
except ImportError:
    from protocol import read_message, send_message
    from state import CARDS, ClientState

class GameClient:
    def __init__(self, host: str, port: int, user: str):
        self.host, self.port, self.user = host, port, user
        self.state, self.events = ClientState(), queue.Queue()
        self.writer, self.loop, self.running = None, None, True
        self.network_ready = threading.Event()
        self.reconnect_window = 15.0
        self.reconnect_interval = 0.5

    def observe(self, event):
        self.state.apply(event); self.events.put(event); return event

    async def _heartbeat(self):
        while self.running and self.writer and not self.writer.is_closing():
            await asyncio.sleep(5)
            try: await send_message(self.writer, "Heartbeat", "")
            except (ConnectionError, OSError): return

    async def _close_writer(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _connect_and_auth(self):
        """Open one transport and finish login or token reconnect."""
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self.writer = writer
        self.observe({"type": "Connected", "payload": {}})
        self.network_ready.set()

        token = self.state.view().get("session_token", "")
        if token:
            await send_message(writer, "ReconnectReq", token)
            response = await asyncio.wait_for(read_message(reader), 5.0)
            self.observe(response)
            if response.get("type") == "ReconnectResp" and response.get("payload", {}).get("ok") == "1":
                return reader
            # A rejected token is retryable only while an unfinished room is
            # active. Once a result is received, the server has intentionally
            # invalidated the room token and we should start a fresh login.
            if self.state.active():
                raise ConnectionError("reconnect rejected")
            self.state.clear_session_token()

        await send_message(writer, "LoginReq", self.user)
        response = await asyncio.wait_for(read_message(reader), 5.0)
        self.observe(response)
        if response.get("type") != "LoginResp" or response.get("payload", {}).get("ok") != "1":
            raise ConnectionError("login failed")
        return reader

    async def network(self):
        """Keep the session alive and retry transport loss for 15 seconds."""
        while self.running:
            heartbeat = None
            try:
                reader = await self._connect_and_auth()
                heartbeat = asyncio.create_task(self._heartbeat())
                while self.running:
                    self.observe(await read_message(reader))
                break
            except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                    ConnectionError, OSError, ValueError) as exc:
                if not self.running:
                    break
                self.observe({"type": "Disconnected", "payload": {
                    "error": str(exc), "reconnecting": "1"}})
                await self._close_writer()
                deadline = asyncio.get_running_loop().time() + self.reconnect_window
                reconnected = False
                while self.running and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(self.reconnect_interval)
                    try:
                        reader = await self._connect_and_auth()
                        heartbeat = asyncio.create_task(self._heartbeat())
                        reconnected = True
                        break
                    except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                            ConnectionError, OSError, ValueError):
                        await self._close_writer()
                if not reconnected:
                    self.observe({"type": "Disconnected", "payload": {
                        "error": "reconnect timeout", "reconnecting": "0"}})
                    break
                try:
                    while self.running:
                        self.observe(await read_message(reader))
                    break
                except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                        ConnectionError, OSError, ValueError) as retry_exc:
                    self.observe({"type": "Disconnected", "payload": {
                        "error": str(retry_exc), "reconnecting": "1"}})
                    await self._close_writer()
                    continue
            finally:
                if heartbeat:
                    heartbeat.cancel(); await asyncio.gather(heartbeat, return_exceptions=True)
                if self.writer and self.writer.is_closing():
                    await self._close_writer()
        self.network_ready.set()
        if self.state.view().get("connected"):
            self.observe({"type": "Disconnected", "payload": {"reconnecting": "0"}})
    def start_network(self):
        self.loop = asyncio.new_event_loop()
        def runner():
            asyncio.set_event_loop(self.loop); self.loop.run_until_complete(self.network()); self.loop.close()
        threading.Thread(target=runner, name="arena-network", daemon=True).start()
    def send(self, message_type, payload=None):
        if not self.loop or not self.writer or self.writer.is_closing(): return False
        asyncio.run_coroutine_threadsafe(send_message(self.writer, message_type, payload or {}), self.loop); return True
    def queue_match(self):
        try: self.state.reserve_queue(); self.send("MatchJoinReq", {})
        except ValueError as exc: self.events.put({"type": "LocalError", "payload": {"code": str(exc)}})
    def action(self, slot=None):
        try:
            typ, payload = self.state.reserve_action(slot); self.send(typ, payload)
        except ValueError as exc: self.events.put({"type": "LocalError", "payload": {"code": str(exc)}})
    def drain_events(self):
        result = []
        while True:
            try: result.append(self.events.get_nowait())
            except queue.Empty: return result
    def cli(self):
        self.start_network(); printing = threading.Event(); printing.set()
        def printer():
            while printing.is_set():
                try: event = self.events.get(timeout=0.2)
                except queue.Empty: continue
                print("\n<", json.dumps(event, ensure_ascii=False), flush=True)
        thread = threading.Thread(target=printer, name="arena-cli-log", daemon=True); thread.start()
        print("Arena Cards CLI | match | play <hand slot 1-9> | end | cancel | quit")
        while self.running:
            try: command = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt): command = "quit"
            if command in ("quit", "exit"): self.running = False; break
            if command == "match": self.queue_match()
            elif command == "cancel": self.send("MatchCancelReq", {})
            elif command in ("end", "endturn"): self.action(None)
            elif command.startswith("play "):
                try: self.action(int(command.split()[1]) - 1)
                except (IndexError, ValueError): print("Use: play 1")
        printing.clear(); self.stop(); thread.join(timeout=1)
    def pygame(self):
        try: import pygame
        except ImportError: print("pygame is not installed; starting CLI fallback"); return self.cli()
        pygame.init(); screen = pygame.display.set_mode((1120, 720)); pygame.display.set_caption("Arena Cards")
        font, small, clock = pygame.font.Font(None, 30), pygame.font.Font(None, 22), pygame.time.Clock(); logs=[]
        self.start_network()
        while self.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT: self.running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_m: self.queue_match()
                    elif event.key == pygame.K_e: self.action(None)
                    elif pygame.K_1 <= event.key <= pygame.K_9: self.action(event.key - pygame.K_1)
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    x, y = event.pos
                    if y > 560 and x < 900: self.action(min(8, max(0, (x - 20) // 110)))
                    elif y > 560 and 930 < x < 1080: self.action(None)
                    elif y < 80 and x < 180: self.queue_match()
            for event in self.drain_events(): logs.append(f"{event.get('type')}: {event.get('payload', {})}")
            logs = logs[-5:]; view = self.state.view(); snap = view["snapshot"]; screen.fill((19, 25, 42))
            def txt(value, pos, colour=(230, 235, 245), f=font): screen.blit(f.render(str(value), True, colour), pos)
            txt(f"Arena Cards  [{view['user'] or self.user}]", (20, 16))
            pygame.draw.rect(screen, (56, 105, 130), (780, 10, 150, 42), border_radius=8)
            txt("MATCH [M]", (808, 24), (255, 255, 255), small)
            if view["queued"]: txt("MATCHMAKING...", (20, 70), (250, 210, 90))
            elif view["result"]: txt(f"RESULT: {view['result']}", (20, 70), (250, 210, 90))
            elif snap:
                me, op = view["player_index"], 1 - view["player_index"]
                txt(f"You  HP {snap[f'p{me}_hp']}/30  Shield {snap[f'p{me}_shield']}  Energy {snap[f'p{me}_energy']}", (20, 80))
                txt(f"Opponent  HP {snap[f'p{op}_hp']}/30  Shield {snap[f'p{op}_shield']}", (20, 120), (220, 170, 170))
                txt(("YOUR TURN" if snap["turn"] == me else "OPPONENT TURN") + f"  |  {snap['remaining_ms']} ms", (20, 170), (110, 220, 190))
                for index, card_id in enumerate(snap["hand"][:9]):
                    card = CARDS.get(card_id, {"name": f"Card {card_id}", "cost": "?", "text": ""}); x, y = 20 + index * 110, 570
                    pygame.draw.rect(screen, (45, 63, 98), (x, y, 98, 125), border_radius=8)
                    txt(f"{index+1}. {card['name']}", (x+6, y+8), f=small); txt(f"Cost {card['cost']}", (x+6, y+35), (250, 210, 90), small); txt(card["text"], (x+6, y+64), (220, 225, 235), small)
                pygame.draw.rect(screen, (75, 130, 105), (935, 570, 150, 52), border_radius=8); txt("END TURN [E]", (950, 587), (255,255,255), small)
            if view["last_error"]: txt(f"Error: {view['last_error']}", (20, 220), (255, 110, 110), small)
            for index, line in enumerate(logs): txt(line[:120], (20, 260 + index*27), (150, 160, 180), small)
            pygame.display.flip(); clock.tick(30)
        pygame.quit(); self.stop()
    def stop(self):
        self.running = False
        if self.loop and self.writer: self.loop.call_soon_threadsafe(self.writer.close)

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=9000); parser.add_argument("--user", default="player1"); parser.add_argument("--cli", action="store_true")
    args = parser.parse_args(); client = GameClient(args.host, args.port, args.user); client.cli() if args.cli else client.pygame()
if __name__ == "__main__": main()
