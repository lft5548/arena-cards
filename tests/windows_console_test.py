"""Real Windows Ctrl+C/Ctrl+Break acceptance in isolated hidden consoles."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from network_limits_test import ROOT, close, connect, environment, exchange
from protocol import make_message, make_proto_message, negotiate_protocol

SHUTDOWN_MS = 1000
SCHEDULING_MARGIN_SECONDS = 0.75
EVENTS = {"CTRL_C_EVENT": 0, "CTRL_BREAK_EVENT": 1}


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def console_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    kernel.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    kernel.GetConsoleProcessList.restype = wintypes.DWORD
    kernel.GetConsoleWindow.restype = wintypes.HWND
    window = kernel.GetConsoleWindow()
    if window:
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.ShowWindow(window, 0)  # SW_HIDE; never expose a helper console window.
    return kernel, handler_type


async def eof(reader) -> None:
    try:
        while await asyncio.wait_for(reader.read(65536), 1.75):
            pass
    except (ConnectionResetError, BrokenPipeError):
        pass


async def ready_peer(process, port):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited before readiness: {process.returncode}")
        try:
            return await connect(port)
        except OSError:
            await asyncio.sleep(0.02)
    raise AssertionError("server did not accept within four seconds")


async def exercise(args, kernel, result):
    peers = []
    process = None
    server_log = Path(args.output_dir) / "server.log"
    try:
        with server_log.open("w", encoding="utf-8") as output:
            # Inherit only this worker's private console. No process group or
            # ignore-Ctrl+C flag: both real console events must reach the CRT.
            process = subprocess.Popen(
                [args.server, str(args.port)], cwd=ROOT,
                env=environment(ARENA_BIND_ADDRESS="127.0.0.1",
                                ARENA_HEARTBEAT_TIMEOUT_MS="30000",
                                ARENA_SHUTDOWN_TIMEOUT_MS=str(SHUTDOWN_MS)),
                stdout=output, stderr=subprocess.STDOUT,
            )
            result["server_pid"] = process.pid
            save(Path(args.output_dir) / "running.json", result)
            peers.append(await ready_peer(process, args.port))
            peers.append(await connect(args.port))
            for peer in peers:
                if args.protocol == "proto_v1":
                    await negotiate_protocol(*peer, args.protocol)
                await exchange(peer, "Heartbeat", "Pong", args.protocol)
            current = await exchange(peers[0], "AdminRoomsReq", "AdminRoomsResp", args.protocol)
            if current.get("active_sessions") != "2":
                raise AssertionError(f"expected two ready transports: {current}")
            # Leave one reader blocked on an incomplete length header while
            # the other transport is idle, with prior protocol/Pong proof.
            frame = (make_message("Heartbeat") if args.protocol == "text_v1"
                     else make_proto_message("Heartbeat", b""))
            peers[1][1].write(frame[:2])
            await peers[1][1].drain()

            attached = (wintypes.DWORD * 16)()
            count = kernel.GetConsoleProcessList(attached, len(attached))
            if not count or count > len(attached):
                raise AssertionError(f"unexpected console process count: {count}")
            ids = list(attached[:count])
            result["console_pids"] = ids
            if os.getpid() not in ids or process.pid not in ids or args.coordinator_pid in ids:
                raise AssertionError(f"console was not isolated from coordinator: {ids}")
            result["console_isolated"] = True
            started = time.monotonic()
            if not kernel.GenerateConsoleCtrlEvent(EVENTS[args.event], 0):
                raise ctypes.WinError(ctypes.get_last_error())
            result["console_event_generated"] = True
            code, *_ = await asyncio.gather(
                asyncio.to_thread(process.wait, SHUTDOWN_MS / 1000 + SCHEDULING_MARGIN_SECONDS),
                *(eof(reader) for reader, _ in peers),
            )
            elapsed = time.monotonic() - started
            result.update(server_exit_code=code, shutdown_seconds=round(elapsed, 6),
                          idle_and_partial_peers_closed=True)
            if code != 0 or elapsed > SHUTDOWN_MS / 1000 + SCHEDULING_MARGIN_SECONDS:
                raise AssertionError(f"console shutdown was not clean/bounded: code={code}, elapsed={elapsed}")
            try:
                unexpected = await connect(args.port)
            except OSError:
                result["listener_closed"] = True
            else:
                await close(unexpected[1])
                raise AssertionError("listener accepted after server exit")
        log = server_log.read_text(encoding="utf-8", errors="replace")
        markers = ["shutdown: accept stopped", "shutdown: transports closed",
                   "shutdown: transports drained", "shutdown: complete"]
        positions = [log.find(marker) for marker in markers]
        if any(index < 0 for index in positions) or positions != sorted(positions):
            raise AssertionError(f"graceful shutdown log sequence missing: {log}")
        result["shutdown_log_sequence"] = markers
    finally:
        await asyncio.gather(*(close(writer) for _, writer in peers), return_exceptions=True)
        if process is not None and process.poll() is None:
            # TerminateProcess is failure cleanup only; it never satisfies a case.
            result["forced_failure_cleanup"] = True
            process.kill()
            await asyncio.to_thread(process.wait, 2)


def worker(args):
    result = {"event": args.event, "protocol": args.protocol, "worker_pid": os.getpid(),
              "coordinator_pid": args.coordinator_pid, "shutdown_budget_ms": SHUTDOWN_MS,
              "scheduling_margin_seconds": SCHEDULING_MARGIN_SECONDS,
              "mysql_enabled": False, "redis_enabled": False, "passed": False}
    kernel, handler_type = console_api()
    received_events = []

    @handler_type
    def keep_worker_alive(event):
        if event in EVENTS.values():
            received_events.append(int(event))
            return True
        return False

    # A custom worker handler is not inherited by the server. Unlike the
    # NULL-handler ignore flag, this cannot accidentally disable its Ctrl+C.
    if not kernel.SetConsoleCtrlHandler(keep_worker_alive, True):
        raise ctypes.WinError(ctypes.get_last_error())
    # The Python/venv launcher may inherit an ignore-Ctrl+C flag from the
    # invoking host. Clear it in this private console before creating the
    # server, so the child has the normal Windows console input semantics.
    if not kernel.SetConsoleCtrlHandler(handler_type(), False):
        raise ctypes.WinError(ctypes.get_last_error())
    result["ctrl_c_ignore_cleared_before_server"] = True
    try:
        asyncio.run(asyncio.wait_for(exercise(args, kernel, result), 10))
        if EVENTS[args.event] not in received_events:
            raise AssertionError("worker did not observe its generated console event")
        result["passed"] = True
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        result["worker_received_events"] = received_events
        save(Path(args.output_dir) / "summary.json", result)
        kernel.SetConsoleCtrlHandler(keep_worker_alive, False)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["passed"] else 1


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--port", type=int, default=19176)
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--output-dir")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--event", choices=tuple(EVENTS), help=argparse.SUPPRESS)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), help=argparse.SUPPRESS)
    parser.add_argument("--coordinator-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("requires Windows real console APIs; no simulated pass")
    args.server = str(Path(args.server).resolve(strict=True))
    if args.worker:
        return worker(args)
    if not args.output_dir:
        args.output_dir = str(ROOT / "build-windows-console" /
                              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]))
    directory = Path(args.output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    summary = {"started_utc": datetime.now(timezone.utc).isoformat(),
               "server": args.server, "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest(),
               "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "cases": [], "passed": False}
    protocols = ("text_v1",) if args.text_only else ("text_v1", "proto_v1")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE; CREATE_NO_WINDOW cannot deliver console events.
    for event in EVENTS:
        for protocol in protocols:
            case_dir = directory / f"{event.lower()}-{protocol}"
            case_dir.mkdir()
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       "--server", args.server, "--port", str(args.port),
                       "--output-dir", str(case_dir), "--event", event,
                       "--protocol", protocol, "--coordinator-pid", str(os.getpid())]
            completed = subprocess.run(command, cwd=ROOT, startupinfo=startup,
                                       creationflags=subprocess.CREATE_NEW_CONSOLE,
                                       capture_output=True, timeout=15, check=False)
            (case_dir / "worker.stdout.log").write_bytes(completed.stdout)
            (case_dir / "worker.stderr.log").write_bytes(completed.stderr)
            result_path = case_dir / "summary.json"
            case = (json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists()
                    else {"event": event, "protocol": protocol, "passed": False,
                          "error": f"worker exit {completed.returncode} without evidence"})
            case["worker_exit_code"] = completed.returncode
            summary["cases"].append(case)
            if completed.returncode != 0:
                case["passed"] = False
            print(f"{event} {protocol}: {'PASS' if case['passed'] else 'FAIL'}; "
                  f"exit={case.get('server_exit_code')}, seconds={case.get('shutdown_seconds')}", flush=True)
    summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
    summary["passed"] = all(case["passed"] for case in summary["cases"])
    save(directory / "summary.json", summary)
    print(f"Windows console: {sum(case['passed'] for case in summary['cases'])}/{len(summary['cases'])}; "
          f"evidence={directory / 'summary.json'}", flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
