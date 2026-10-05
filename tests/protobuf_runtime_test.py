"""Full ProtoV1 and mixed TextV1 battle acceptance, including persisted replay."""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from status_effects_integration_test import MatchProbe, replay
from discard_integration_test import check_private, discard_action, finish
from protocol import make_message, make_proto_message, read_message, send_message, negotiate_protocol
from generated import arena_cards_pb2 as messages


async def security(port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(make_proto_message("LoginReq", messages.LoginRequest(user="before").SerializeToString()))
        await writer.drain()
        response = await read_message(reader)
        if response["payload"].get("code") != "protocol_not_negotiated":
            raise AssertionError("binary login bypassed negotiation")
        await send_message(writer, "ProtocolHelloReq", "protocol=proto_v1;version=2")
        response = await read_message(reader)
        if response["payload"].get("code") != "unsupported_protocol_version":
            raise AssertionError("unsupported version accepted")
        for payload in ("protocol=", "version=", "protocol=text_v1;protocol=proto_v1"):
            await send_message(writer, "ProtocolHelloReq", payload)
            response = await read_message(reader)
            if response["payload"].get("code") != "invalid_protocol_hello":
                raise AssertionError("ambiguous protocol selection accepted")
        await negotiate_protocol(reader, writer, "proto_v1")
        for frame, expected in (
                (make_message("LoginReq", "wrong"), "protocol_mismatch"),
                (make_proto_message("LoginReq", b"\x80"), "malformed_protobuf"),
                (make_proto_message(0x4fff), "unknown_message"),
                (make_proto_message("BattleSnapshot"), "unexpected_message_direction"),
                (make_proto_message("EndTurnReq"), "invalid_request_context")):
            writer.write(frame)
            await writer.drain()
            response = await read_message(reader, protocol="proto_v1")
            if response["payload"].get("code") != expected:
                raise AssertionError(f"expected {expected}: {response}")
        await send_message(writer, "ProtocolHelloReq", "protocol=text_v1;version=1")
        response = await read_message(reader, protocol="proto_v1")
        if response["payload"].get("code") != "protocol_locked":
            raise AssertionError("mid-session downgrade accepted")
        await send_message(writer, "Heartbeat", {"request_id": "after-errors"}, protocol="proto_v1")
        pong = await read_message(reader, protocol="proto_v1")
        if pong["type"] != "Pong" or pong["payload"].get("request_id") != "after-errors":
            raise AssertionError("malformed frames changed protocol or broke heartbeat")
        await send_message(writer, "AdminRoomsReq", {"request_id": "metrics"}, protocol="proto_v1")
        metrics = await read_message(reader, protocol="proto_v1")
        if metrics["type"] != "AdminRoomsResp" or "active_rooms" not in metrics["payload"]:
            raise AssertionError("binary admin metrics missing")
        if metrics["payload"].get("request_id") != "metrics":
            raise AssertionError("binary admin metrics lost request correlation")
        for name in (
            "active_rooms", "active_sessions",
            "room_commands_enqueued", "room_commands_processed",
            "room_command_queue_high_watermark", "send_frames_enqueued",
            "send_frames_dropped", "send_queue_high_watermark_bytes",
            "settlements_started", "settlements_succeeded", "settlements_failed",
            "settlement_retries", "settlement_latency_ms_total", "settlement_latency_ms_max",
            "settlement_outbox_applied", "settlement_outbox_failures", "settlement_outbox_pending",
            "settlement_outbox_load_failures", "settlement_outbox_mark_failures",
            "settlement_outbox_record_failures", "mysql_connection_attempts",
            "mysql_connection_successes", "mysql_connection_failures", "mysql_connection_losses",
            "redis_connection_failures", "redis_apply_failures",
            "replays_saved", "replay_save_failures",
        ):
            if name not in metrics["payload"] or not 0 <= int(metrics["payload"][name]) <= 2**64 - 1:
                raise AssertionError(f"missing or invalid binary admin metric {name}: {metrics}")
        await send_message(writer, "LoginReq", {"user": "binary-validation", "request_id": "login-1"}, protocol="proto_v1")
        login = await read_message(reader, protocol="proto_v1")
        if login["payload"].get("request_id") != "login-1" or not login["payload"].get("token"):
            raise AssertionError("binary login context or token missing")
        await send_message(writer, "MatchJoinReq", {"request_id": "join-1"}, protocol="proto_v1")
        joined = await read_message(reader, protocol="proto_v1")
        if joined["payload"].get("queued") != "1" or joined["payload"].get("request_id") != "join-1":
            raise AssertionError("binary match join acknowledgement missing")
        await send_message(writer, "MatchCancelReq", {"request_id": "cancel-1"}, protocol="proto_v1")
        cancelled = await read_message(reader, protocol="proto_v1")
        if cancelled["payload"].get("cancelled") != "1" or cancelled["payload"].get("request_id") != "cancel-1":
            raise AssertionError("binary match cancel failed")
    finally:
        writer.close()
        await writer.wait_closed()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_message(writer, "LoginReq", "locked-text")
        await read_message(reader)
        await send_message(writer, "ProtocolHelloReq", "protocol=proto_v1;version=1")
        response = await read_message(reader)
        if response["payload"].get("code") != "protocol_locked":
            raise AssertionError("logged-in TextV1 upgraded without reconnect")
    finally:
        writer.close()
        await writer.wait_closed()
    print("ProtoV1 negotiation/version/malformed/direction/switch/login/heartbeat/admin/cancel passed")


async def rejected_action(match: MatchProbe) -> None:
    player = int(match.peers[0]["snapshot"]["turn"])
    peer = match.peers[player]
    await send_message(peer["writer"], "PlayCardReq", {
        "match_id": match.match_id, "turn_id": match.turn_id - 1, "action_id": 999,
        "card": 9, "request_id": "stale-check"}, protocol=peer["protocol"])
    error = await match.read_until(peer, "Error")
    if error.get("code") != "stale_turn":
        raise AssertionError(f"stale protobuf action accepted: {error}")
    if peer["protocol"] == "proto_v1" and error.get("request_id") != "stale-check":
        raise AssertionError("actor error lost request correlation")


async def battle(server: str, port: int, directory: Path, rows: str,
                 protocols: tuple[str, str], container: str = "") -> None:
    async with MatchProbe(server, port, directory, rows, protocols) as match:
        if not container:
            await security(port)
        check_private(match)
        for card in (3, 2, 2, 3, 7, 8):
            await match.action(card)
            await match.action()
        await rejected_action(match)
        await discard_action(match, 9, 2)
        await match.retry(0)
        await match.retry(0, conflict=True)
        await match.reconnect(0)
        await match.retry(0)
        match.peers[1]["protocol"] = "text_v1" if protocols[1] == "proto_v1" else "proto_v1"
        await match.reconnect(1)
        check_private(match)
        final = await finish(match, container)
        if final["players"][1]["discard"] != [1, 2] or final["turn_id"] != 41:
            raise AssertionError("new states or maximum-turn result changed across protocols")
    print("full battle/retry/conflict/reconnect/private state/replay passed", protocols)


async def lethal(server: str, port: int, directory: Path) -> None:
    rows = "1,Venom,1,poison,30,2\n2,Renew,1,regen,30,2\n3,Barrier,1,shield,6,0\n"
    async with MatchProbe(server, port, directory, rows, ("proto_v1", "proto_v1")) as match:
        await match.action(1, terminal=True)
        final, _ = await match.verify_replay()
        if final["winner"] != 0 or final["reason"] != "poison" or final["players"][1]["hp"] != 0:
            raise AssertionError("binary lethal status settlement wrong")
    print("binary lethal poison and offline reconstruction passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="")
    parser.add_argument("--existing-server", action="store_true")
    parser.add_argument("--server-container", default="arena-cards-server")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if not args.existing_server and not args.server:
        parser.error("--server is required unless --existing-server is set")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        if args.existing_server:
            config = root / "cards.csv"
            subprocess.run(["docker", "cp", args.server_container + ":/app/config/cards.csv", str(config)], check=True)
            replay.load_cards(config)
            rows = config.read_text(encoding="utf-8").split("\n", 1)[1]
            for index, protocols in enumerate((("proto_v1", "proto_v1"), ("text_v1", "proto_v1"))):
                asyncio.run(battle("", args.port or 9000, root / str(index), rows, protocols, args.server_container))
        else:
            rows = (Path(__file__).resolve().parents[1] / "server/config/cards.csv").read_text(encoding="utf-8").split("\n", 1)[1]
            port = args.port or 19124
            server = os.path.abspath(args.server)
            for index, protocols in enumerate((("proto_v1", "proto_v1"), ("text_v1", "proto_v1"), ("proto_v1", "text_v1"))):
                asyncio.run(battle(server, port + index, root / str(index), rows, protocols))
            asyncio.run(lethal(server, port + 3, root / "lethal"))
    print("ProtoV1 full-runtime acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
