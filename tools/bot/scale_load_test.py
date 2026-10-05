#!/usr/bin/env python3
"""Run the P6 Bot matrix and collect service-container resource evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LOAD_TEST = Path(__file__).with_name("load_test.py")
DEFAULT_CONTAINERS = ("arena-cards-server", "arena-cards-mysql", "arena-cards-redis")


def percentile(values: Iterable[float], ratio: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def parse_bytes(raw: str) -> int | None:
    text = raw.strip().replace(",", "")
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3,
             "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}
    for suffix, multiplier in sorted(units.items(), key=lambda item: -len(item[0])):
        if text.upper().endswith(suffix):
            try:
                return int(float(text[:-len(suffix)].strip()) * multiplier)
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_percent(raw: str) -> float | None:
    try:
        return float(raw.strip().rstrip("%"))
    except (AttributeError, ValueError):
        return None


def sample_docker_stats(containers: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    command = ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return {"_error": {"error": str(error)}}
    if completed.returncode != 0:
        return {"_error": {"error": completed.stderr.strip() or "docker stats failed"}}
    result: dict[str, dict[str, Any]] = {}
    for line in completed.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = raw.get("Name") or raw.get("Container") or "unknown"
        usage = str(raw.get("MemUsage", "")).split(" / ", 1)[0]
        result[name] = {
            "cpu_percent": parse_percent(raw.get("CPUPerc", "")),
            "memory_bytes": parse_bytes(usage),
            "memory_usage": raw.get("MemUsage", ""),
        }
    return result


def summarize_samples(samples: list[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    if not samples:
        return {"available": False, "sample_count": 0, "containers": {}}
    containers: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for sample in samples:
        if "_error" in sample:
            errors.append(str(sample["_error"].get("error", "docker stats unavailable")))
        for name, values in sample.items():
            if name == "_error":
                continue
            entry = containers.setdefault(name, {"cpu_percent": [], "memory_bytes": []})
            if values.get("cpu_percent") is not None:
                entry["cpu_percent"].append(values["cpu_percent"])
            if values.get("memory_bytes") is not None:
                entry["memory_bytes"].append(values["memory_bytes"])
    output: dict[str, Any] = {"available": bool(containers), "sample_count": len(samples), "containers": {}}
    if errors:
        output["errors"] = sorted(set(errors))
    for name, values in containers.items():
        output["containers"][name] = {
            "peak_cpu_percent": max(values["cpu_percent"], default=None),
            "peak_memory_bytes": max(values["memory_bytes"], default=None),
            "samples": len(values["cpu_percent"]),
        }
    return output


def extract_report(stdout: str, clients: int, error: str | None = None) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and "clients" in report:
            return report
    return {
        "clients": clients, "rounds_per_client": 0, "attempted_rounds": 0,
        "completed_rounds": 0, "success_rate": 0.0,
        "throughput_rounds_per_sec": 0.0, "latency": {},
        "results": [], "error": error or "load_test produced no JSON report",
    }


def run_scale(args: argparse.Namespace, clients: int) -> tuple[dict[str, Any], str, str, int]:
    command = [sys.executable, str(LOAD_TEST), "--host", args.host, "--port", str(args.port),
               "--count", str(clients), "--rounds", str(args.rounds),
               "--timeout", str(args.timeout)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    samples: list[dict[str, dict[str, Any]]] = []
    stop = threading.Event()

    def collect() -> None:
        while process.poll() is None:
            samples.append(sample_docker_stats(args.containers))
            stop.wait(args.stats_interval)

    sampler = threading.Thread(target=collect, name="docker-stats", daemon=True)
    sampler.start()
    stdout, stderr = process.communicate()
    stop.set()
    sampler.join(timeout=2)
    report = extract_report(stdout, clients, stderr.strip() or None)
    report["docker"] = summarize_samples(samples)
    report["exit_code"] = process.returncode
    return report, stdout, stderr, process.returncode


def markdown(report: dict[str, Any]) -> str:
    lines = ["# P6 规模压测报告", "",
             f"生成时间：{report['generated_at']}",
             f"目标：{report['host']}:{report['port']}，每个规模每个 Bot {report['rounds_per_client']} 局。", "",
             "本报告只记录实际执行结果；未执行或 Docker 指标不可用的字段保留为待验收。", "",
             "| Bot | 房间 | 成功率 | 吞吐（玩家轮次/s） | ActionAck P50/P95/P99 ms | 完整流程 P50/P95/P99 ms | Docker CPU 峰值 | Docker容器内存峰值 | 结果 |",
             "|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for item in report["scales"]:
        latency = item.get("latency", {})
        action = latency.get("action_ack", {})
        rounds = latency.get("completed_round", {})
        docker = item.get("docker", {})
        cpu: list[float] = []
        rss: list[int] = []
        for values in docker.get("containers", {}).values():
            if values.get("peak_cpu_percent") is not None: cpu.append(values["peak_cpu_percent"])
            if values.get("peak_memory_bytes") is not None: rss.append(values["peak_memory_bytes"])
        result = "通过" if item.get("success_rate") == 1.0 and item.get("exit_code") == 0 else "失败/待验收"
        def triple(values: dict[str, Any]) -> str:
            return "/".join(str(values.get(key, "-")) for key in ("p50_ms", "p95_ms", "p99_ms"))
        lines.append(f"| {item.get('clients', '-')} | {int(item.get('clients', 0)) // 2} | {item.get('success_rate', '-')} | {item.get('throughput_rounds_per_sec', '-')} | {triple(action)} | {triple(rounds)} | {(max(cpu) if cpu else '-') }% | {(max(rss) if rss else '-')} bytes | {result} |")
    lines += ["", "资源采集容器：" + ", ".join(report["containers"]),
              "瓶颈判断应结合服务端日志、MySQL/Redis 指标和这张表；该脚本没有把压测数据解释成生产容量承诺。", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 20/100/200 Bot P6 scale matrix")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--scales", default="20,100,200", help="comma-separated even Bot counts")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--stats-interval", type=float, default=1.0)
    parser.add_argument("--out-dir", type=Path, default=Path("docs/load-tests"))
    parser.add_argument("--containers", default=",".join(DEFAULT_CONTAINERS))
    args = parser.parse_args()
    if args.rounds <= 0 or args.timeout <= 0 or args.stats_interval <= 0:
        parser.error("rounds, timeout and stats-interval must be positive")
    try:
        scales = [int(value) for value in args.scales.split(",") if value.strip()]
    except ValueError:
        parser.error("scales must be comma-separated integers")
    if not scales or any(value <= 0 or value % 2 for value in scales):
        parser.error("scales must contain positive even Bot counts")
    args.containers = tuple(value.strip() for value in args.containers.split(",") if value.strip())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    matrix: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": args.host, "port": args.port, "rounds_per_client": args.rounds,
        "containers": list(args.containers), "scales": [],
    }
    exit_code = 0
    for clients in scales:
        report, stdout, stderr, result_code = run_scale(args, clients)
        matrix["scales"].append(report)
        (args.out_dir / f"{clients}-bot.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if result_code != 0: exit_code = 1
    (args.out_dir / "matrix.json").write_text(
        json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path("docs/load-test-report.md").write_text(markdown(matrix), encoding="utf-8")
    print(json.dumps(matrix, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
