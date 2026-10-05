"""Repeatable asyncio load client for Arena Cards.

The script intentionally uses only the Python standard library. It drives real
matches through the same inspectable protocol as ``bot.py`` and emits one JSON
document suitable for archiving in a load-test report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client_pygame"))
from protocol import negotiate_protocol, payload_int, read_message, send_message  # noqa: E402
from state import ClientState, choose_card  # noqa: E402


def percentile(values: list[float], fraction: float) -> float | None:
    """Return nearest-rank percentile in milliseconds, or None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": round(max(values), 3) if values else None,
    }


class MeasuredBot:
    def __init__(self, host: str, port: int, name: str, timeout: float, rounds: int, protocol: str = "text_v1"):
        self.host = host
        self.port = port
        self.name = name
        self.timeout = timeout
        self.rounds = rounds
        self.protocol = protocol
        self.writer: asyncio.StreamWriter | None = None
        self.messages = 0
        self.actions = 0
        self.errors: list[str] = []
        self.action_latencies: list[float] = []
        self.match_latencies: list[float] = []
        self.login_latencies: list[float] = []
        self.round_latencies: list[float] = []

    async def run(self) -> dict[str, Any]:
        rounds: list[dict[str, Any]] = []
        for number in range(1, self.rounds + 1):
            result = await self.run_round(number)
            rounds.append(result)
            if not result["completed"]:
                break
            # Keep the pressure run deterministic while allowing the server's
            # room cleanup to finish before this client queues another match.
            await asyncio.sleep(0.02)
        return {
            "bot": self.name,
            "rounds": rounds,
            "completed_rounds": sum(1 for item in rounds if item["completed"]),
            "errors": sum(len(item["errors"]) for item in rounds),
            "latencies": {
                "login": latency_summary(self.login_latencies),
                "matchmaking": latency_summary(self.match_latencies),
                "action_ack": latency_summary(self.action_latencies),
                "round": latency_summary(self.round_latencies),
            },
        }

    async def run_round(self, number: int) -> dict[str, Any]:
        state = ClientState()
        started = time.perf_counter()
        round_errors: list[str] = []
        action_sent: dict[int, float] = {}
        action_latency_start = len(self.action_latencies)
        action_count_start = self.actions
        seen_revisions: set[int] = set()
        heartbeat: asyncio.Task[None] | None = None
        reader: asyncio.StreamReader | None = None
        self.writer = None
        joined_at = 0.0
        match_found = False

        def failed(detail: str) -> dict[str, Any]:
            error = str(detail)
            round_errors.append(error)
            self.errors.append(error)
            return {
                "round": number,
                "completed": False,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "matchmaking_ms": None,
                "actions": self.actions - action_count_start,
                "action_latency_ms": [
                    round(value, 3) for value in self.action_latencies[action_latency_start:]
                ],
                "errors": round_errors,
            }

        try:
            connect_started = time.perf_counter()
            reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
            state.apply({"type": "Connected", "payload": {}})
            if self.protocol == "proto_v1":
                await negotiate_protocol(reader, self.writer, self.protocol)
            await send_message(self.writer, "LoginReq", self.name, protocol=self.protocol)
            login = await asyncio.wait_for(read_message(reader, protocol=self.protocol), min(self.timeout, 5.0))
            self.messages += 1
            login_elapsed = (time.perf_counter() - connect_started) * 1000
            self.login_latencies.append(login_elapsed)
            state.apply(login)
            if login.get("type") != "LoginResp" or login.get("payload", {}).get("ok") != "1":
                return failed("login_failed")

            joined_at = time.perf_counter()
            await send_message(
                self.writer,
                "MatchJoinReq",
                {"request_id": f"load-{self.name}-{number}"},
                protocol=self.protocol,
            )
            heartbeat = asyncio.create_task(self.heartbeat())
            deadline = time.perf_counter() + self.timeout
            while time.perf_counter() < deadline:
                remaining = max(0.1, deadline - time.perf_counter())
                try:
                    message = await asyncio.wait_for(read_message(reader, protocol=self.protocol), min(remaining, 2.0))
                except asyncio.TimeoutError:
                    continue
                self.messages += 1
                typ = message.get("type")
                data = message.get("payload", {})
                if typ == "ActionAck":
                    action_id = payload_int(data, "action_id", -1)
                    sent = action_sent.pop(action_id, None)
                    if sent is not None:
                        elapsed = (time.perf_counter() - sent) * 1000
                        self.action_latencies.append(elapsed)
                    continue
                accepted = state.apply(message)
                if typ == "Error":
                    code = str(data.get("code", "server_error"))
                    return failed(code)
                if typ == "MatchFound":
                    match_found = True
                    self.match_latencies.append((time.perf_counter() - joined_at) * 1000)
                    continue
                if typ == "BattleSnapshot" and accepted:
                    revision = payload_int(data, "revision", -1)
                    if revision in seen_revisions:
                        continue
                    seen_revisions.add(revision)
                    view = state.view()
                    snapshot = view["snapshot"]
                    if not snapshot or snapshot["done"] or snapshot["turn"] != view["player_index"]:
                        continue
                    try:
                        slot = choose_card(snapshot, view["player_index"])
                        message_type, payload = state.reserve_action(slot)
                        action_id = int(payload["action_id"])
                        action_sent[action_id] = time.perf_counter()
                        await send_message(self.writer, message_type, payload, protocol=self.protocol)
                        self.actions += 1
                    except ValueError:
                        continue
                elif typ == "MatchResult":
                    if not match_found or not state.result:
                        return failed("malformed_result")
                    elapsed = (time.perf_counter() - started) * 1000
                    self.round_latencies.append(elapsed)
                    return {
                        "round": number,
                        "completed": True,
                        "duration_ms": round(elapsed, 3),
                        "matchmaking_ms": round(self.match_latencies[-1], 3),
                        "winner": data.get("winner"),
                        "actions": self.actions - action_count_start,
                        "action_latency_ms": [
                            round(value, 3) for value in self.action_latencies[action_latency_start:]
                        ],
                        "errors": round_errors,
                    }
            return failed("round_timeout")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            return failed(type(exc).__name__)
        except (OSError, ValueError, RuntimeError) as exc:
            return failed(str(exc))
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if self.writer:
                self.writer.close()
                await self.writer.wait_closed()
                self.writer = None

    async def heartbeat(self) -> None:
        while self.writer and not self.writer.is_closing():
            await asyncio.sleep(5)
            try:
                await send_message(self.writer, "Heartbeat", "", protocol=self.protocol)
            except (ConnectionError, OSError):
                return


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    results = await asyncio.gather(
        *(MeasuredBot(args.host, args.port, f"{getattr(args, 'name_prefix', 'load')}{index + 1}", args.timeout,
                      args.rounds, getattr(args, "protocol", "text_v1")).run()
          for index in range(args.count)),
        return_exceptions=True,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    normalized: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if isinstance(result, dict):
            normalized.append(result)
        else:
            normalized.append({
                "bot": f"load{index + 1}", "completed_rounds": 0,
                "errors": 1, "error": str(result), "rounds": [],
            })
    completed = sum(item.get("completed_rounds", 0) for item in normalized)
    attempted = sum(len(item.get("rounds", [])) for item in normalized)
    round_latencies = [
        float(round_result["duration_ms"])
        for item in normalized
        for round_result in item.get("rounds", [])
        if round_result.get("completed") and round_result.get("duration_ms") is not None
    ]
    action_latencies = [
        float(value)
        for item in normalized
        for round_result in item.get("rounds", [])
        for value in round_result.get("action_latency_ms", [])
    ]
    return {
        "host": args.host,
        "port": args.port,
        "clients": args.count,
        "rounds_per_client": args.rounds,
        "duration_ms": round(elapsed_ms, 3),
        "attempted_rounds": attempted,
        "completed_rounds": completed,
        "success_rate": round(completed / attempted, 4) if attempted else 0.0,
        "throughput_rounds_per_sec": round(completed / (elapsed_ms / 1000), 3) if elapsed_ms else 0.0,
        "latency": {
            "completed_round": latency_summary(round_latencies),
            "action_ack": latency_summary(action_latencies),
        },
        "results": normalized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Arena Cards multi-room load client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--count", type=int, default=20, help="even number of concurrent bots")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    parser.add_argument("--name-prefix", default="load")
    parser.add_argument("--json-out", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args()
    if args.count <= 0 or args.count % 2 or args.rounds <= 0 or args.timeout <= 0:
        parser.error("count must be a positive even number; rounds and timeout must be positive")
    report = asyncio.run(run_load(args))
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(encoded)
    if args.json_out:
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report["success_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
