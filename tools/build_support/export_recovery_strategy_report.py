"""Publish the finite recovery experiment without local paths or identities.

Reads completed evidence only. It never starts servers or changes deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics


CONFIGURATION_FIELDS = ("timeout", "scales", "workloads", "repeats", "snapshot_interval", "seed", "protocol",
    "recovery_clients", "skip_recovery", "recovery_budget_ms", "adoption_percent")
WORKLOAD_FIELDS = ("mode", "workload", "clients", "repeat", "protocol", "seed", "snapshot_interval", "batch_size",
    "completed_matches", "duration_ms", "prepare_ms", "throughput_matches_per_second", "play_only_matches_per_second",
    "latency", "match_latency_scope", "trace_sha256", "actions", "metrics", "resources", "terminal_turn_min",
    "terminal_turn_max", "replays_verified", "passed", "raw_sha256")
RECOVERY_FIELDS = ("mode", "clients", "rooms", "repeat", "seed", "prepared_mutations_per_room", "startup_to_listening_ms",
    "startup_to_all_reconnected_ms", "startup_to_pair_reconnected", "recovery_budget_ms", "within_budget", "trace_sha256",
    "replays_verified", "passed", "raw_sha256")
FAULT_FIELDS = ("protocol", "window", "old_snapshot_and_tail_visible", "rollback_verified_online", "original_ack_verified",
    "replay_verified", "passed", "proxy", "tls_capability_removed", "private_auth_cache_warmed_via_tls",
    "opponent_disconnected_during_uncertain_commit", "opponent_reconnected_with_original_token")


def selected(value, keys):
    return {key: value[key] for key in keys if key in value}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fault_evidence(path):
    source = read(path)
    return {**selected(source, ("passed", "fixtures_cleaned", "server_sha256", "harness_sha256")),
        "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sanitizer_report_count": len(source.get("sanitizer_reports", [])),
        "results": [selected(item, FAULT_FIELDS) for item in source["results"]]}


def public_evidence(summary_path, release_faults, sanitizer_faults):
    source = read(summary_path)
    result = {**selected(source, ("passed", "fixtures_cleaned", "source_commit", "server_sha256", "harness_sha256")),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        "configuration": selected(source["configuration"], CONFIGURATION_FIELDS),
        "workloads": [selected(item, WORKLOAD_FIELDS) for item in source["workloads"]],
        "recovery": [selected(item, RECOVERY_FIELDS) for item in source["recovery"]],
        "comparison": source.get("comparison"),
        "faults": {"release": fault_evidence(release_faults), "sanitizers": fault_evidence(sanitizer_faults)}}
    if not result.get("passed") or not result.get("fixtures_cleaned") or not result.get("comparison"):
        raise ValueError("comparison must finish successfully with cleaned fixtures before publication")
    for kind, evidence in result["faults"].items():
        if not evidence.get("passed") or not evidence.get("fixtures_cleaned") or evidence["sanitizer_report_count"]:
            raise ValueError(kind + " fault evidence is incomplete or has sanitizer findings")
    if result["server_sha256"] != result["faults"]["release"]["server_sha256"]:
        raise ValueError("performance and Release fault evidence must use the same binary")
    if (not result["comparison"].get("scope_complete") or len(result["workloads"]) != 36 or len(result["recovery"]) != 6
            or any(len(item["results"]) != 8 for item in result["faults"].values())):
        raise ValueError("the agreed 36 performance, six recovery and eight fault samples must be complete")
    paired = [pair for group in result["comparison"]["groups"] for pair in group["pairs"]]
    recovery_paired = result["comparison"]["recovery_pairs"]
    if len(paired) != 18 or len(recovery_paired) != 3 or not all(pair["identical_trace"] for pair in paired + recovery_paired):
        raise ValueError("all paired command traces must have been verified")
    result["paired_trace_verified"] = True
    for row in result["workloads"]:
        row["metric_denominators"] = {"logical_checkpoint_requests": row["metrics"]["recovery_checkpoint_attempts"],
            "initial_match_transactions": row["completed_matches"],
            "checkpoint_update_batches": row["metrics"]["recovery_checkpoint_batches"],
            "total_persistence_transactions": row["metrics"]["recovery_checkpoint_batches"] + row["completed_matches"]}
    return result


def median(items, value):
    return statistics.median(value(item) for item in items)


def number(value, decimals=3):
    return f"{value:.{decimals}f}"


def markdown(data):
    config, compare = data["configuration"], data["comparison"]
    rows = data["workloads"]
    names = {"ordinary": "正常选牌", "long": "长局"}
    def group(workload, count, mode):
        return [row for row in rows if row["workload"] == workload and row["clients"] == count and row["mode"] == mode]
    decision = compare["performance_recommendation"]
    faults_ok = all(len(item["results"]) == 8 and all(row["passed"] for row in item["results"])
                    for item in data["faults"].values())
    adopted = decision == "tail" and faults_ok
    decision_text = ("采用 `tail` 为默认恢复策略。" if adopted else
        "保留 `full` 为默认恢复策略；`tail` 作为可配置方案保留，本次比较在这里收口。")
    verified_matches = sum(row["completed_matches"] for row in rows) + sum(row["replays_verified"] for row in data["recovery"])
    output = ["# 在线恢复策略对照", "", decision_text, "",
        f"本轮完成 {len(rows)} 场性能样本及 {len(data['recovery'])} 场独立重启恢复样本。"
        f"数据来自同一 Linux C++17 Release 二进制、真实 MySQL/Redis，{verified_matches:,} 局逐局核对 MySQL、Outbox、Redis 与完整回放。"
        "公开原始指标见 [recovery-strategy-report.json](recovery-strategy-report.json)。", "",
        "## 比较条件", "",
        f"- 同一二进制 SHA256：`{data['server_sha256']}`。",
        f"- 源码标识 `{data.get('source_commit','unknown')}` 表示实验构建时的基础提交与工作区修改；"
        "二进制哈希与公开原始样本绑定，不将基础提交单独当作本次功能版本。",
        f"- 固定随机种子 `{config['seed']}`，MySQL pool=2、checkpoint batch=16、快照间隔={config['snapshot_interval']}；"
        f"TextV1，20/100/200 Bot，每个模式/规模/负载各 {config['repeats']} 次；配对顺序交替。",
        "- 正常选牌使用现有 Bot `choose_card` 策略；长局先使用一张 Strike，再 EndTurn 至最大回合。"
        "本种子下两类负载均实际到第 40 回合，区别是动作与状态/事件组成；正常选牌样本不能宣称为短对局。",
        "- 每对样本核对固定 seed 与去掉 match_id 的动作轨迹 SHA256；同模式使用相同规则与操作序列。",
        "- 容量样本包含建局与真实结果交付，吞吐按真实对局数计；每局双方都收到 MatchResult 才算完成。"
        "离线回放只用于终局核对，在线恢复由 C++ 加载快照并顺序应用尾部。",
        "- Arena 与 MySQL 的 CPU/RSS 由 `/proc` 每 50ms 采样。MySQL 为共享 mysqld 进程，包含后台及其他用户活动；"
        "100% CPU 表示一个核心。采样期间不并行执行编译或其他验收。",
        "- 测试构建仅为固定 seed/故障屏障显式开启 `ARENA_ENABLE_TEST_FAULTS`；生产镜像关闭。", "",
        "## 性能结果", "",
        "下表是同组 3 次样本的中位数。ACK 为最近秩百分位；对局吞吐从开始创建所有房间至收到所有 MatchResult。"
        "写入字节是恢复 payload 的应用字节，包含快照与尾部，不等于 MySQL redo、binlog、索引或实际磁盘写入量。", "",
        "| 负载 | Bot | full ACK P95/P99 ms | tail ACK P95/P99 ms | full/tail 局/s | full/tail KiB/局 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for workload in config["workloads"]:
        for count in config["scales"]:
            full, tail = (group(workload, count, mode) for mode in ("full", "tail"))
            p = lambda items, field: number(median(items, lambda row: row["latency"]["action_ack"][field]))
            throughput = " / ".join(number(median(items, lambda row: row["throughput_matches_per_second"])) for items in (full, tail))
            bytes_per_match = " / ".join(number(median(items, lambda row: row["metrics"]["recovery_checkpoint_bytes_total"] / row["completed_matches"] / 1024), 2) for items in (full, tail))
            output.append(f"| {names[workload]} | {count} | {p(full,'p95_ms')} / {p(full,'p99_ms')} | {p(tail,'p95_ms')} / {p(tail,'p99_ms')} | {throughput} | {bytes_per_match} |")
    output += ["", "只有 100/200 Bot 的两类负载都在同一指标上连续三对达到至少 "
        f"{config['adoption_percent']:g}% 改善，且 100 房间恢复到全部原 token 可重连在 "
        f"{config['recovery_budget_ms']/1000:g}s 内，并通过故障正确性，才更换默认策略。"
        "负数表示 tail 更慢。", "",
        "| 负载 | Bot | 各对 P99 改善 % | 各对吞吐改善 % | 稳定门槛 |",
        "| --- | ---: | --- | --- | --- |"]
    for item in compare["groups"]:
        p99 = ", ".join(number(pair["p99_improvement_percent"]) for pair in item["pairs"])
        throughput = ", ".join(number(pair["throughput_improvement_percent"]) for pair in item["pairs"])
        output.append(f"| {names[item['workload']]} | {item['clients']} | {p99} | {throughput} | {'达到' if item['stable_threshold_met'] else '未达到'} |")
    output += ["", "## 持久化分段", "",
        "以下仍为每组三次中位数。序列化与批量队列等待按逻辑请求计；存储锁、连接、SQL、COMMIT"
        "按持久化事务计，分母为更新批次数＋开局事务数（每局一次）。更新 batch 只累计一次，"
        "而 SQL/COMMIT 累计还包含串行开局写，不能只除以更新批次或相加成单动作耗时。"
        "完整指标、初始写入数、更新批次数与请求数保留在公开 JSON。", "",
        "| 负载 | Bot | 模式 | serialize ms/请求 | queue ms/请求 | lock ms/事务 | connection ms/事务 | SQL ms/事务 | COMMIT ms/事务 |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for workload in config["workloads"]:
        for count in config["scales"]:
            for mode in ("full", "tail"):
                items = group(workload, count, mode)
                values = []
                for field, denominator in (("serialize", "logical_checkpoint_requests"), ("queue_wait", "logical_checkpoint_requests"),
                        ("lock_wait", "total_persistence_transactions"), ("connection", "total_persistence_transactions"),
                        ("sql", "total_persistence_transactions"), ("commit", "total_persistence_transactions")):
                    values.append(number(median(items, lambda row: row["metrics"]["recovery_checkpoint_" + field + "_us_total"] /
                        row["metric_denominators"][denominator] / 1000)))
                output.append(f"| {names[workload]} | {count} | {mode} | " + " | ".join(values) + " |")
    output += ["", "## 进程资源", "",
        "CPU 为样本区间平均值，RSS 为每轮采样峰值，再取三次中位数；不能当作整机容量或生产部署上限。", "",
        "| 负载 | Bot | 模式 | Arena CPU % | Arena RSS MiB | MySQL CPU % | MySQL RSS MiB |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for workload in config["workloads"]:
        for count in config["scales"]:
            for mode in ("full", "tail"):
                items = group(workload, count, mode)
                values = []
                for process in ("arena", "mysql"):
                    values += [number(median(items, lambda row: row["resources"][process]["mean_cpu_percent"])),
                               number(median(items, lambda row: row["resources"][process]["peak_rss_bytes"] / 2**20), 2)]
                output.append(f"| {names[workload]} | {count} | {mode} | " + " | ".join(values) + " |")
    output += ["", "## 重启恢复", "",
        "每轮预置 100 个房间，每房间 24 次持久化变化：tail 从序号 16 的快照恢复后续 8 条记录。"
        "计时从创建服务进程开始，到 200 个原 token 全部收到恢复后的 BattleSnapshot；"
        "包括启动加载、owner 认领、重连请求和重连元数据提交。继续完成每局并核对回放。", "",
        "| 次数 | full 全部可重连 ms | tail 全部可重连 ms | tail/full | tail ≤5s |",
        "| ---: | ---: | ---: | ---: | --- |"]
    for item in compare["recovery_pairs"]:
        output.append(f"| {item['repeat']} | {number(item['full_ms'])} | {number(item['tail_ms'])} | {number(item['tail_to_full_ratio'])} | {'是' if item['tail_ms'] <= config['recovery_budget_ms'] else '否'} |")
    output += ["", "## 故障与兼容验证", "",
        "同版本 Release 与 ASan/UBSan 各通过双协议 8 项：tail COMMIT 后 ACK 前强杀；"
        "快照 UPDATE 后尾部 DELETE 前、DELETE 后 COMMIT 前两个强杀位置；"
        "MySQL 已成功 COMMIT 但 OK 回复丢失后的重试。最后一项同时在候选待重试期间关闭对手连接，"
        "确认断线用下一序号持久化、保留原始 15 秒绝对期限，再用原 token 重连及重启 C++ 恢复。", "",
        "COMMIT 回复丢失使用私有透明 MySQL 代理，转发真实 COMMIT，读取并丢弃成功 OK 后关闭两端连接；"
        "另一个 MySQL 连接确认 SQL 已提交。代理仅为这条测试连接关闭 TLS 协商，随机测试用户经直接 TLS"
        " 预热认证；该证据覆盖事务回复不确定，不声明覆盖生产 TLS 黑洞。", "",
        "Release 与 sanitizer 两组均清理临时 schema、玩家/幂等缓存数据，保留共享服务和命名卷。"
        "sanitizer 未产生产品报告；精确强杀进程不完成退出时的泄漏扫描。", "",
        f"- Release 故障二进制：`{data['faults']['release']['server_sha256']}`。",
        f"- ASan/UBSan 故障二进制：`{data['faults']['sanitizers']['server_sha256']}`。", "",
        "## 复现", "", "在仓库根目录、已有 MySQL/Redis 的 Compose 网络上，使用项目工具镜像和现有 Linux Release 构建。"
        "性能二进制须显式启用测试开关以固定随机种子，生产部署保持关闭。", "", "```powershell",
        "$arenaWorkspace = (Get-Location).Path",
        "docker run --rm --network deploy_default --pid container:arena-cards-mysql --mount \"type=bind,source=$arenaWorkspace,target=/workspace\" --entrypoint python arena-cards-sanitizers:local tools/bot/recovery_compare.py --server build-linux-release/arena_server --evidence build-recovery-compare",
        "docker run --rm --network deploy_default --mount \"type=bind,source=$arenaWorkspace,target=/workspace\" --entrypoint python arena-cards-sanitizers:local tests/recovery_tail_fault_test.py --server build-linux-release/arena_server --evidence build-recovery-tail-faults",
        "```", "",
        "完整检查点与周期快照＋尾部都在 MySQL COMMIT 成功后 ACK，保持原 token、最近 128 个原 ACK、"
        "完整规则状态/RNG、绝对期限、pending result、owner 防旧写和终局幂等。"
        "事件尾部按持久化序号恢复权威状态增量及事件/元数据；连续性、checksum、规则指纹或状态校验失败时拒绝启动。", ""]
    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--release-faults", type=Path, required=True)
    parser.add_argument("--sanitizer-faults", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, default=Path("docs/recovery-strategy-report.json"))
    parser.add_argument("--markdown-out", type=Path, default=Path("docs/recovery-strategy-report.md"))
    args = parser.parse_args()
    evidence = public_evidence(args.summary, args.release_faults, args.sanitizer_faults)
    args.json_out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(markdown(evidence), encoding="utf-8")
    print(json.dumps({"json_out": str(args.json_out), "markdown_out": str(args.markdown_out),
        "samples": len(evidence["workloads"]), "recovery_samples": len(evidence["recovery"]),
        "performance_recommendation": evidence["comparison"]["performance_recommendation"]}))


if __name__ == "__main__":
    main()
