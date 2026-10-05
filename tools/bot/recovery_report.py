"""Publish measured recovery matrices with checksum-linked, compact evidence."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
TIMING_FIELDS = ("lock_wait", "connection", "sql", "commit", "queue_wait")


def evidence_time(summary, row, raw):
    for owner, document in (("record", row), ("summary", summary)):
        for key in ("completed_at", "recorded_at", "started_at", "run_date"):
            if document.get(key):
                value = datetime.fromisoformat(str(document[key]).replace("Z", "+00:00"))
                if value.tzinfo is None:
                    value = value.replace(tzinfo=SHANGHAI)
                return value.astimezone(SHANGHAI).isoformat(), owner + "." + key
    return (datetime.fromtimestamp(raw.stat().st_mtime, SHANGHAI).isoformat(),
            "raw_file_mtime_inferred")


def collect_records(runs):
    records, sources = [], []
    for run in map(Path, runs):
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        if summary.get("passed") is not True or summary.get("fixtures_cleaned") is not True:
            raise ValueError("run is incomplete or fixtures are not cleaned: " + str(run))
        if not summary.get("matrix"):
            raise ValueError("run contains no measured scenarios: " + str(run))
        source = {"run": run.as_posix(), "server_sha256": summary["server_sha256"]}
        if summary.get("server_path"):
            source["server_path"] = summary["server_path"]
        dates, date_sources = [], set()
        for row in summary["matrix"]:
            if row.get("passed") is not True:
                raise ValueError("a scenario failed: " + str(run))
            raw = (run / row["raw_file"]).resolve()
            if not raw.is_relative_to(run.resolve()):
                raise ValueError("raw evidence is outside its run directory")
            if hashlib.sha256(raw.read_bytes()).hexdigest() != row["raw_sha256"]:
                raise ValueError("raw evidence checksum differs: " + str(raw))
            measured = json.loads(raw.read_text(encoding="utf-8"))["report"]
            for key, value in row.items():
                if key not in ("raw_file", "raw_sha256") and (key not in measured or measured[key] != value):
                    raise ValueError("summary differs from raw evidence for " + key + ": " + str(raw))
            batch_size = row.get("checkpoint_batch_size", summary.get("checkpoint_batch_size"))
            batch_source = "record" if "checkpoint_batch_size" in row else "summary"
            if batch_size is None:
                if "recovery_checkpoint_batches" in row.get("metrics", {}):
                    raise ValueError("batched evidence is missing checkpoint_batch_size: " + str(run))
                batch_size, batch_source = 1, "legacy_unbatched_inferred"
            if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 32:
                raise ValueError("invalid checkpoint_batch_size: " + str(run))
            recorded_at, date_source = evidence_time(summary, row, raw)
            records.append({"run": run.as_posix(), **row, "checkpoint_batch_size": batch_size,
                            "checkpoint_batch_size_source": batch_source,
                            "recorded_at": recorded_at, "recorded_at_source": date_source})
            dates.append(recorded_at)
            date_sources.add(date_source)
        source.update(recorded_at_first=min(dates), recorded_at_last=max(dates),
                      recorded_at_sources=sorted(date_sources))
        sources.append(source)
    return records, sources


def triple(value):
    return "/".join(str(value.get(key, "-")) for key in ("p50_ms", "p95_ms", "p99_ms"))


def number(value, divisor=1):
    return "-" if value is None else str(round(value / divisor, 3))


def reproduction_commands(records, sources):
    commands = []
    for source in sources:
        commands.append("# 来源 " + source["run"] + "；server SHA256 " + source["server_sha256"])
        groups = defaultdict(list)
        for row in records:
            if row["run"] == source["run"]:
                groups[(row["protocol"], row["checkpoint_batch_size"])].append(row)
        server = source.get("server_path", "build-linux-release/arena_server")
        for (protocol, batch), rows in sorted(groups.items()):
            prefix = ('docker run --rm --network deploy_default --mount '
                      '"type=bind,source=$((Get-Location).Path),target=/workspace" --entrypoint python '
                      'arena-cards-sanitizers:local tools/bot/recovery_load_test.py --server "' + server + '" '
                      f'--protocol {protocol} --checkpoint-batch-size {batch}')
            baselines = [row for row in rows if row["scenario"] == "baseline"]
            disabled = sorted({row["clients"] for row in baselines if not row["recovery"]})
            enabled_only = sorted({row["clients"] for row in baselines if row["recovery"]} - set(disabled))
            if disabled:
                commands.append(prefix + " --scenarios baseline --scales " +
                                ",".join(map(str, disabled)) + " --compare-disabled")
            if enabled_only:
                commands.append(prefix + " --scenarios baseline --scales " + ",".join(map(str, enabled_only)))
            faults = defaultdict(set)
            for row in rows:
                if row["scenario"] != "baseline":
                    faults[row["clients"]].add(row["scenario"])
            for count, scenarios in sorted(faults.items()):
                commands.append(prefix + " --scenarios " + " ".join(sorted(scenarios)) +
                                " --scenario-count " + str(count))
    return commands


def render_report(records, sources, evidence):
    dates = sorted({row["recorded_at"][:10] for row in records})
    date_range = dates[0] if len(dates) == 1 else dates[0] + " 至 " + dates[-1]
    lines = ["# 恢复开启后的 P6 证据", "",
             date_range + "（Asia/Shanghai）；来源时间及二进制 SHA256 见证据表。",
             "使用现有 Compose MySQL/Redis，临时 schema/用户/Redis 测试行已清理；恢复采用同步完整检查点。",
             "同表包含不同构建的历史基线和新证据，比较前须核对来源、协议、恢复开关及批量配置。", "",
             "| 场景 | 协议 | Bot/房间 | 恢复 | batch上限 | 成功率 | 完成对局/s | ACK P50/P95/P99 ms | 完整流程 P50/P95/P99 ms | CPU峰值 % | RSS峰值 MiB | 检查点均值 bytes / 写入 ms |",
             "|---|---|---:|---|---:|---:|---:|---|---|---:|---:|---|"]
    for row in records:
        latency, resource = row["latency"], row["resources"]
        full = latency.get("completed_round", latency.get("completed_match", {}))
        inferred = "*" if row["checkpoint_batch_size_source"] == "legacy_unbatched_inferred" else ""
        lines.append(f"| {row['scenario']} | {row['protocol']} | {row['clients']}/{row['completed_matches']} | "
                     f"{int(row['recovery'])} | {row['checkpoint_batch_size']}{inferred} | {row['success_rate']} | "
                     f"{row['throughput_matches_per_second']} | {triple(latency['action_ack'])} | {triple(full)} | "
                     f"{number(resource.get('peak_cpu_percent'))} | {number(resource.get('peak_rss_bytes'), 1024**2)} | "
                     f"{row.get('checkpoint_mean_bytes') or '-'} / {number(row.get('checkpoint_mean_write_ms'))} |")
    coverage = defaultdict(set)
    for row in records:
        coverage[(row["scenario"], row["protocol"], row["recovery"], row["checkpoint_batch_size"])].add(row["clients"])
    lines.extend(["", "## 实际覆盖", "",
                  "| 场景 | 协议 | 恢复 | batch上限 | 实测 Bot 数 |",
                  "|---|---|---|---:|---|"])
    for (scenario, protocol, recovery, batch), counts in sorted(coverage.items()):
        lines.append(f"| {scenario} | {protocol} | {int(recovery)} | {batch} | " +
                     ", ".join(map(str, sorted(counts))) + " |")
    timed = [row for row in records if any("recovery_checkpoint_" + name + "_us_total" in row.get("metrics", {})
                                         for name in TIMING_FIELDS)]
    if timed:
        lines.extend(["", "## 检查点耗时口径", "",
                      "下表是累计秒（原始指标为微秒），不是请求均值。batch=1 的物理锁/连接/SQL/COMMIT按单次写入累计；",
                      "批量路径的物理阶段每个事务只计一次，queue/write仍按每个等待请求累计。两种口径不能直接相减或统一除以请求数。", "",
                      "| 场景/协议/Bot/batch | 尝试数 | 实际事务计数 | 锁等待 s | 连接 s | SQL s | COMMIT s | 队列等待 s | 请求写入 s |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
        for row in timed:
            metrics = row["metrics"]
            label = f"{row['scenario']}/{row['protocol']}/{row['clients']}/{row['checkpoint_batch_size']}"
            totals = [number(metrics.get("recovery_checkpoint_" + name + "_us_total"), 1_000_000)
                      for name in TIMING_FIELDS]
            lines.append(f"| {label} | {metrics.get('recovery_checkpoint_attempts', '-')} | "
                         f"{metrics.get('recovery_checkpoint_batches', '-')} | " + " | ".join(totals) +
                         " | " + number(metrics.get("recovery_checkpoint_write_us_total"), 1_000_000) + " |")
    lines.extend(["", "## 口径与边界", "",
        "- 完成对局/s按实际 MySQL 对局数计算，两个玩家完成轮次只算一局。每行均核对 MySQL 积分/胜负、Redis一致、Outbox applied及每局离线回放；SQL applied是最终依据。",
        "- ACK从客户端发送动作至收到原 ActionAck。基线完整流程包含连接/登录/匹配；受控重连/延迟场景从开局快照开始。缺失的历史分位数标为 '-'，不补造。",
        "- CPU/RSS用50ms采样读取 /proc 的 Arena 进程，100%为一个核，可超过100%；不含 Python/MySQL/Redis，RSS不是容器内存。",
        "- batch上限是配置值，不是每个事务的实际成员数；实际计数/高水位保存在原始指标。带 '*' 的1由历史无批量指标的单写路径推断；新证据须显式记录配置。",
        "- write均值为请求写入累计微秒 / 尝试数 / 1000，包含排队、锁等待、SQL与提交；序列化独立累计。并发请求累计耗时可超过墙钟时间。",
        "- slow-reader使用额外不读响应的TextV1 Admin连接触发默认有界发送队列背压；正常Bot使用表中协议，该场景不等于所有Bot都慢消费。",
        "- reconnect每房间一名玩家断线，用原token续局并重发原action_id，核对原ACK及完整检查点/RNG/截止时间；mysql-delay为真实单检查点行锁注入，不能称为全库网络延迟。",
        "- Redis故障以原始场景观测为准：redis-outage是不可用窗口，redis-reply-loss是响应丢失，redis-partition是隔离后恢复；不代表多实例自动故障转移或完整生产网络分区矩阵。",
        "- 表内只有实际完成且已清理的样本。单次本机短对局没有置信区间，不代表生产容量；性能采样须与编译/CTest分开。", "",
        "## 判断", "",
        "此报告记录恢复持久化开销及故障正确性。性能优化效果须在同规模、同协议、同环境和确认语义下比较，",
        "保留每个来源的二进制及批量配置。当前完整检查点路径不等于周期快照＋事件尾部；报告不会据样本自动宣称后者更快。", "",
        "## 证据", "",
        f"紧凑汇总、时间来源、配置和原始SHA256见 [{evidence.name}]({evidence.name})。",
        "旧记录没有测量时间时采用原始文件mtime并明确标为推断；文件复制可改变该时间，不能当成精确历史时间。", "",
        "| 原始目录 | 时间（Asia/Shanghai） | 时间来源 | server SHA256 |",
        "|---|---|---|---|"])
    for source in sources:
        lines.append(f"| {source['run']} | {source['recorded_at_first']} → {source['recorded_at_last']} | " +
                     ", ".join(source["recorded_at_sources"]) + f" | {source['server_sha256']} |")
    lines.extend(["", "## 复现", "",
                  "在仓库根目录PowerShell执行，保持现有MySQL/Redis healthy及实际Compose网络；不删除数据卷。",
                  "先选择与该来源SHA256相符的Release二进制（关闭sanitizer/测试屏障）。下列命令不会自动切换历史版本；",
                  "旧来源未记录server_path时采用build-linux-release/arena_server，占位路径须指向对应构建。",
                  "所有命令显式指定batch；--scenario-count仅控制故障场景，--scales仅控制baseline。禁用基线复现会同时跑恢复开启对照。", "",
                  "```powershell", *reproduction_commands(records, sources), "```", "",
                  "发布器只接受passed/fixtures_cleaned为true的非空矩阵，并核对原始文件SHA256与关键测量字段。"])
    return "\n".join(lines) + "\n"


def publish_runs(runs, output):
    records, sources = collect_records(runs)
    if not records:
        raise ValueError("no measured scenarios")
    output = Path(output)
    evidence = output.with_suffix(".jsonl")
    markdown = render_report(records, sources, evidence)
    evidence.write_text("\n".join(json.dumps(row, separators=(",", ":"), ensure_ascii=False) for row in
                                 [{"schema_version": 2, "time_zone": "Asia/Shanghai", "sources": sources}, *records]) +
                        "\n", encoding="utf-8")
    output.write_text(markdown, encoding="utf-8")
    return len(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("docs/recovery-load-report.md"))
    args = parser.parse_args()
    try:
        count = publish_runs(args.runs, args.output)
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    print(f"Published {count} measured scenarios to {args.output} and {args.output.with_suffix('.jsonl')}")


if __name__ == "__main__":
    main()
