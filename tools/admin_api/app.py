"""FastAPI admin/debug facade for the local Arena Cards demo.

The C++ service can be run without an HTTP dependency; this API is deliberately
small and talks to it through the same framed key=value protocol for diagnostics.
"""
from __future__ import annotations
import argparse, asyncio, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client_pygame"))
from protocol import read_message, send_message

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover
    FastAPI = None
    _IMPORT_ERROR = exc

HOST=os.getenv("ARENA_HOST", "127.0.0.1"); PORT=int(os.getenv("ARENA_PORT", "9000"))
ALLOWED_COMMANDS = {"AdminRoomsReq", "Heartbeat"}
app = FastAPI(title="Arena Cards Admin", version="0.1") if FastAPI else None

if FastAPI:
    class Command(BaseModel):
        type: str
        payload: dict = {}

async def command(msg_type: str, payload: dict):
    if msg_type not in ALLOWED_COMMANDS:
        raise HTTPException(400, f"command is not allowed: {msg_type}")
    try:
        reader, writer=await asyncio.open_connection(HOST, PORT)
        await send_message(writer, msg_type, payload)
        reply=await asyncio.wait_for(read_message(reader), timeout=2)
        writer.close(); await writer.wait_closed(); return reply
    except Exception as exc:
        raise HTTPException(503, f"server unavailable: {exc}")

if app:
    @app.get("/health")
    async def health():
        reply = await command("AdminRoomsReq", {})
        return {"ok": True, "ready": reply.get("type") == "AdminRoomsResp", "service": "arena-admin", "server": f"{HOST}:{PORT}"}

    @app.get("/metrics")
    async def metrics():
        reply = await command("AdminRoomsReq", {})
        if reply.get("type") != "AdminRoomsResp":
            raise HTTPException(502, "unexpected response from arena server")
        payload = reply.get("payload", {})
        try:
            active_rooms = int(payload.get("active_rooms", "0"))
            active_sessions = int(payload.get("active_sessions", "0"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(502, "invalid metrics response from arena server") from exc
        result = {
            "timestamp": time.time(),
            "server": f"{HOST}:{PORT}",
            "transport": "tcp-framed-key-value",
            "active_rooms": active_rooms,
            "active_sessions": active_sessions,
        }
        # Counters were added without changing the TextV1 response shape.
        # Older binaries simply omit them and remain valid for this facade.
        counter_names = (
            "connections_rejected", "requests_rate_limited", "heartbeat_timeouts",
            "room_commands_enqueued", "room_commands_processed",
            "room_command_queue_high_watermark", "send_frames_enqueued",
            "send_frames_dropped", "send_queue_high_watermark_bytes",
            "settlements_started", "settlements_succeeded",
            "settlements_failed", "settlement_retries",
            "settlement_latency_ms_total", "settlement_latency_ms_max",
            "settlement_outbox_applied", "settlement_outbox_failures",
            "settlement_outbox_pending", "settlement_outbox_load_failures",
            "settlement_outbox_mark_failures", "settlement_outbox_record_failures",
            "mysql_connection_attempts", "mysql_connection_successes",
            "mysql_connection_failures", "mysql_connection_losses",
            "redis_connection_failures", "redis_apply_failures",
            "replays_saved", "replay_save_failures",
            "recovery_checkpoint_attempts", "recovery_checkpoint_successes", "recovery_checkpoint_failures",
            "recovery_checkpoint_bytes_total", "recovery_checkpoint_bytes_max",
            "recovery_checkpoint_serialize_us_total", "recovery_checkpoint_write_us_total",
            "recovery_checkpoint_write_us_max",
            "recovery_checkpoint_lock_wait_us_total", "recovery_checkpoint_connection_us_total",
            "recovery_checkpoint_sql_us_total", "recovery_checkpoint_commit_us_total",
            "recovery_checkpoint_queue_wait_us_total", "recovery_checkpoint_batches", "recovery_checkpoint_batch_items_max",
        )
        for name in counter_names:
            if name in payload:
                try:
                    result[name] = int(payload[name])
                    if name in ("connections_rejected", "requests_rate_limited", "heartbeat_timeouts") and (
                            not str(payload[name]).isdecimal() or not 0 <= result[name] <= 2**64 - 1):
                        raise ValueError("network counter is not uint64")
                except (TypeError, ValueError) as exc:
                    raise HTTPException(502, f"invalid metric: {name}") from exc
        return result

    @app.get("/rooms")
    async def rooms(): return await command("AdminRoomsReq", {})

    @app.post("/command")
    async def send_command(c: Command): return await command(c.type, c.payload)

def main():
    p=argparse.ArgumentParser(); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8080); a=p.parse_args()
    if not app: raise SystemExit("FastAPI is required: pip install fastapi uvicorn")
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port)
if __name__ == "__main__": main()
