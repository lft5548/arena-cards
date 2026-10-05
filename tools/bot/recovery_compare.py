"""Paired full-checkpoint / snapshot-tail experiment on one Linux Release binary.

The finite default is 36 workload runs plus six independent 100-room recovery
runs. Every match is checked against real MySQL, Redis, Outbox and its replay.
Run in the MySQL container's PID namespace to sample the actual mysqld process.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from room_recovery_integration_test import connect, stable_snapshot
from tools.bot.recovery_load_test import CHECKPOINT_FIELDS, Matrix, process_sample, resource_summary
from tools.bot.load_test import latency_summary
from tools.replay.replay_battle import load_cards, load_replay, reconstruct
from client_pygame.state import choose_card
from tools.ranking.rebuild_leaderboard import RedisClient


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mysql_pid(value):
    if value != "auto":
        return int(value)
    candidates = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        with contextlib.suppress(OSError):
            if path.read_text().strip() in ("mysqld", "mariadbd"):
                candidates.append(int(path.parent.name))
    if len(candidates) != 1:
        raise RuntimeError("--mysql-pid auto requires exactly one visible mysqld; use --pid container:arena-cards-mysql")
    return candidates[0]


def resources(samples, source):
    result = resource_summary(samples)
    result["source"] = source
    result["cpu_seconds"] = round(samples[-1]["cpu_seconds"] - samples[0]["cpu_seconds"], 6) if len(samples) > 1 else 0
    result["mean_cpu_percent"] = round(result["cpu_seconds"] / (samples[-1]["at"] - samples[0]["at"]) * 100, 3) if len(samples) > 1 else None
    return result


def comparison(rows, recovery, args):
    groups = []
    for workload in args.workloads:
        for count in args.scales:
            pairs = []
            for repeat in range(1, args.repeats + 1):
                full, tail = (next(item for item in rows if item["mode"] == mode and item["workload"] == workload
                                   and item["clients"] == count and item["repeat"] == repeat) for mode in ("full", "tail"))
                if full["trace_sha256"] != tail["trace_sha256"]:
                    raise AssertionError("paired modes executed different commands")
                full_p99, tail_p99 = (item["latency"]["action_ack"]["p99_ms"] for item in (full, tail))
                pairs.append({"repeat": repeat, "identical_trace": True,
                    "p99_improvement_percent": round((full_p99 - tail_p99) / full_p99 * 100, 3),
                    "throughput_improvement_percent": round((tail["throughput_matches_per_second"] /
                        full["throughput_matches_per_second"] - 1) * 100, 3),
                    "checkpoint_bytes_reduction_percent": round((1 - tail["metrics"]["recovery_checkpoint_bytes_total"] /
                        full["metrics"]["recovery_checkpoint_bytes_total"]) * 100, 3)})
            # The same metric must cross the threshold in every paired repetition.
            stable = (all(item["p99_improvement_percent"] >= args.adoption_percent for item in pairs)
                      or all(item["throughput_improvement_percent"] >= args.adoption_percent for item in pairs))
            groups.append({"workload": workload, "clients": count, "pairs": pairs,
                           "stable_threshold_met": stable,
                           "median_p99_improvement_percent": round(statistics.median(item["p99_improvement_percent"] for item in pairs), 3),
                           "median_throughput_improvement_percent": round(statistics.median(item["throughput_improvement_percent"] for item in pairs), 3)})
    complete_scope = (set(args.scales) >= {20, 100, 200} and set(args.workloads) == {"ordinary", "long"}
                      and args.repeats >= 3 and args.recovery_clients == 200)
    high_scale_pass = all(item["stable_threshold_met"] for item in groups if item["clients"] in (100, 200))
    recovery_pass = bool(recovery) and all(item["startup_to_all_reconnected_ms"] <= args.recovery_budget_ms
                                        for item in recovery if item["mode"] == "tail")
    recovery_pairs = []
    for repeat in range(1, args.repeats + 1):
        matched = {item["mode"]: item for item in recovery if item["repeat"] == repeat}
        if set(matched) == {"full", "tail"}:
            full, tail = matched["full"], matched["tail"]
            if full["trace_sha256"] != tail["trace_sha256"]:
                raise AssertionError("paired recovery modes executed different commands")
            recovery_pairs.append({"repeat": repeat, "identical_trace": True,
                "full_ms": full["startup_to_all_reconnected_ms"], "tail_ms": tail["startup_to_all_reconnected_ms"],
                "tail_to_full_ratio": round(tail["startup_to_all_reconnected_ms"] / full["startup_to_all_reconnected_ms"], 6)})
    return {"groups": groups, "scope_complete": complete_scope, "adoption_threshold_percent": args.adoption_percent,
            "recovery_budget_ms": args.recovery_budget_ms, "all_high_scale_groups_stable": high_scale_pass,
            "tail_recovery_within_budget": recovery_pass, "recovery_pairs": recovery_pairs,
            "performance_recommendation": "tail" if complete_scope and high_scale_pass and recovery_pass else "full",
            "recommendation_requires_separate_fault_acceptance": True}


class Experiment(Matrix):
    def __init__(self, args):
        super().__init__(args)
        self.rows, self.recovery_rows = [], []
        self.db_pid = mysql_pid(args.mysql_pid)
        process_sample(self.db_pid)
        self.environment.update(ARENA_MYSQL_POOL_SIZE="2", ARENA_CHECKPOINT_BATCH_SIZE="16",
            ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL=str(args.snapshot_interval), ARENA_TEST_REPLAY_SEED=str(args.seed))

    async def start_case(self, label, mode):
        self.environment["ARENA_ROOM_RECOVERY_MODE"] = mode
        os.environ.update(self.environment)
        server = self.spawn(label, self.args.port)
        await self.ready(server, self.args.port)
        self.active_names = []
        return server

    async def pairs(self, count, label):
        args = self.client_args(self.args.protocol, self.args.port)
        pairs, names = [], []
        for number in range(count // 2):
            pair, pair_names = await self.new_match(args, label + "_" + str(number))
            if any(int(player.snapshot["replay_seed"]) != self.args.seed for player in pair):
                raise AssertionError("fixed replay seed is inactive; build Release with explicit test faults enabled")
            pairs.append(pair)
            names.append(pair_names)
            self.active_names.extend(pair_names)
        return pairs, names

    async def step(self, pair, workload, trace, latencies):
        index = int(pair[0].snapshot["turn"])
        current = pair[index]
        raw = current.snapshot
        view = {key: int(value) for key, value in raw.items() if key.startswith("p") and str(value).isdigit()}
        view["hand"] = [int(card) for card in str(raw.get("hand", "")).split(",") if card]
        slot = choose_card(view, index) if workload == "ordinary" else None
        # Give long matches a non-tied authoritative result while reaching turn 40.
        if workload == "long" and not trace:
            slot = view["hand"].index(1)
        request = {"match_id": raw["match_id"], "turn_id": int(raw["turn_id"]),
                   "action_id": int(raw["last_action_id"]) + 1}
        kind = "EndTurnReq" if slot is None else "PlayCardReq"
        if slot is not None:
            request["card"] = view["hand"][slot]
        trace.append([index, kind, request["turn_id"], request["action_id"], request.get("card")])
        started = time.perf_counter()
        await current.send(kind, request)
        await current.receive("ActionAck", timeout=self.args.timeout)
        latencies.append((time.perf_counter() - started) * 1000)
        updates = await asyncio.gather(*(player.receive(("BattleSnapshot", "MatchResult"), timeout=self.args.timeout) for player in pair))
        if updates[0]["type"] != updates[1]["type"] or (updates[0]["type"] == "MatchResult"
                and updates[0]["payload"] != updates[1]["payload"]):
            raise AssertionError("players disagree on authoritative outcome")
        if updates[0]["type"] == "MatchResult":
            return updates[0]["payload"]
        return None

    async def finish_workload(self, pairs, workload, traces, latencies, started):
        async def finish(index):
            for _ in range(1000):
                result = await self.step(pairs[index], workload, traces[index], latencies)
                if result:
                    if workload == "long" and traces[index][-1][2] != 40:
                        raise AssertionError("long workload finished before the maximum-turn boundary")
                    return result, (time.perf_counter() - started) * 1000
            raise AssertionError("finite action budget exceeded")
        return await asyncio.gather(*(finish(index) for index in range(len(pairs))))

    async def verify(self, count):
        rows = await self.wait_until(lambda: self.verify_database(count // 2),
                                    "MySQL/Redis/Outbox settlement incomplete", timeout=30)
        cards = load_cards(ROOT / "server/config/cards.csv")
        for match_id, first, _, winner, _ in rows:
            replay = self.evidence / "replays" / (match_id + ".replay")
            await self.wait_until(replay.exists, "replay did not persist")
            loaded_id, _, _, events = load_replay(replay)
            state = reconstruct(events, cards, loaded_id)
            if loaded_id != match_id or state["winner"] != (0 if winner == first else 1):
                raise AssertionError("C++ result / MySQL / replay disagree")
        return rows

    async def clean_case(self, server, pairs, rows):
        self.kill(server)
        await asyncio.gather(*(member.close() for pair in pairs for member in pair))
        redis = RedisClient(self.args.redis_host, self.args.redis_port)
        try:
            redis.command("ZREM", "arena:leaderboard:rating", *self.active_names)
            redis.command("DEL", *("arena:match:rank:" + row[0] for row in rows))
        finally:
            redis.close()
        # matches owns all per-room recovery rows via ON DELETE CASCADE.
        self.clear_matches()

    def save(self, label, row, raw):
        path = self.evidence / (label + ".json")
        path.write_text(json.dumps({"report": row, **raw}, separators=(",", ":")) + "\n", encoding="utf-8")
        row.update(raw_file=path.name, raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        print(json.dumps(row, separators=(",", ":")), flush=True)

    async def workload(self, mode, count, workload, repeat):
        label = f"{workload}-{count}-r{repeat}-{mode}"
        server = await self.start_case(label, mode)
        arena_samples, db_samples = [], []
        stop = asyncio.Event()
        async def sample():
            while True:
                arena_samples.append(process_sample(server.process.pid))
                db_samples.append(process_sample(self.db_pid))
                if stop.is_set():
                    return
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), 0.05)
        sampler = asyncio.create_task(sample())
        started = time.perf_counter()
        try:
            pairs, _ = await self.pairs(count, f"cmp_{workload}_{count}_{repeat}")
            prepared = time.perf_counter()
            traces, latencies = [[] for _ in pairs], []
            results = await self.finish_workload(pairs, workload, traces, latencies, started)
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            await sampler
        rows = await self.verify(count)
        metrics = await self.metrics()
        proto_metrics = await self.metrics("proto_v1")
        if any(proto_metrics[key] != metrics[key] for key in CHECKPOINT_FIELDS):
            raise AssertionError("Admin metrics differ between protocols")
        if not metrics["recovery_checkpoint_successes"] or metrics["recovery_checkpoint_failures"]:
            raise AssertionError("recovery persistence failed during performance workload")
        row = {"mode": mode, "workload": workload, "clients": count, "repeat": repeat, "protocol": self.args.protocol,
            "seed": self.args.seed, "snapshot_interval": self.args.snapshot_interval, "batch_size": 16,
            "completed_matches": len(rows), "duration_ms": round(elapsed * 1000, 3),
            "prepare_ms": round((prepared - started) * 1000, 3),
            "throughput_matches_per_second": round(len(rows) / elapsed, 6),
            "play_only_matches_per_second": round(len(rows) / (elapsed - (prepared - started)), 6),
            "latency": {"action_ack": latency_summary(latencies),
                        "completed_match": latency_summary([item[1] for item in results])},
            "match_latency_scope": "all-pair creation begins to each MatchResult",
            "trace_sha256": digest(traces), "actions": sum(map(len, traces)), "metrics": metrics,
            "terminal_turn_min": min(trace[-1][2] for trace in traces),
            "terminal_turn_max": max(trace[-1][2] for trace in traces),
            "resources": {"arena": resources(arena_samples, "/proc Arena child process"),
                          "mysql": resources(db_samples, "/proc shared mysqld; includes background and other users")},
            "replays_verified": len(rows), "passed": True}
        self.save(label, row, {"action_latency_ms": latencies, "traces": traces,
                              "arena_resource_samples": arena_samples, "mysql_resource_samples": db_samples})
        self.rows.append(row)
        await self.clean_case(server, pairs, rows)

    async def recovery(self, mode, repeat):
        count = self.args.recovery_clients
        label = f"recovery-{count}-r{repeat}-{mode}"
        server = await self.start_case(label, mode)
        pairs, _ = await self.pairs(count, f"cmp_recovery_{count}_{repeat}")
        traces, latencies = [[] for _ in pairs], []
        # 24 persistent mutations: after snapshot 16 there are eight tail rows.
        for _ in range(24):
            results = await asyncio.gather(*(self.step(pair, "long", trace, latencies) for pair, trace in zip(pairs, traces)))
            if any(results):
                raise AssertionError("recovery fixture ended before restart")
        originals = [[stable_snapshot(member.snapshot) for member in pair] for pair in pairs]
        self.kill(server)
        restarted_at = time.perf_counter()
        restarted = self.spawn(label + "-restart", self.args.port)
        await self.ready(restarted, self.args.port)
        listening_at = time.perf_counter()
        args = self.client_args(self.args.protocol, self.args.port)
        async def resume_pair(index):
            resumed = []
            for previous in pairs[index]:
                member = await connect(args, token=previous.token)
                self.connections.append(member)
                await member.receive("BattleSnapshot", timeout=self.args.timeout)
                resumed.append(member)
            # The first player receives a second view when its opponent resumes.
            await resumed[0].receive("BattleSnapshot", timeout=self.args.timeout)
            if [stable_snapshot(member.snapshot) for member in resumed] != originals[index]:
                raise AssertionError("online recovery changed state/token ACK high-water/replay metadata")
            return resumed, (time.perf_counter() - restarted_at) * 1000
        restored = await asyncio.gather(*(resume_pair(index) for index in range(len(pairs))))
        elapsed_ms = (time.perf_counter() - restarted_at) * 1000
        for pair in pairs:
            await asyncio.gather(*(member.close() for member in pair))
        pairs = [item[0] for item in restored]
        await self.finish_workload(pairs, "long", traces, latencies, time.perf_counter())
        rows = await self.verify(count)
        row = {"mode": mode, "clients": count, "rooms": count // 2, "repeat": repeat,
            "seed": self.args.seed, "prepared_mutations_per_room": 24,
            "startup_to_listening_ms": round((listening_at - restarted_at) * 1000, 3),
            "startup_to_all_reconnected_ms": round(elapsed_ms, 3),
            "startup_to_pair_reconnected": latency_summary([item[1] for item in restored]),
            "recovery_budget_ms": self.args.recovery_budget_ms,
            "within_budget": elapsed_ms <= self.args.recovery_budget_ms,
            "trace_sha256": digest(traces), "replays_verified": len(rows), "passed": True}
        self.save(label, row, {"traces": traces})
        self.recovery_rows.append(row)
        await self.clean_case(restarted, pairs, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--mysql-client", default="mysql")
    parser.add_argument("--mysql-host", default="arena-cards-mysql")
    parser.add_argument("--mysql-port", type=int, default=3306)
    parser.add_argument("--mysql-pid", default="auto")
    parser.add_argument("--redis-host", default="arena-cards-redis")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--port", type=int, default=19400)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--scales", default="20,100,200")
    parser.add_argument("--workloads", nargs="+", choices=("ordinary", "long"), default=("ordinary", "long"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--snapshot-interval", type=int, default=16)
    parser.add_argument("--seed", type=int, default=424242)
    parser.add_argument("--protocol", choices=("text_v1", "proto_v1"), default="text_v1")
    parser.add_argument("--recovery-clients", type=int, default=200)
    parser.add_argument("--skip-recovery", action="store_true")
    parser.add_argument("--recovery-budget-ms", type=float, default=5000)
    parser.add_argument("--adoption-percent", type=float, default=20)
    parser.add_argument("--evidence", default="build-recovery-compare")
    args = parser.parse_args()
    args.scales = [int(value) for value in args.scales.split(",")]
    if (not sys.platform.startswith("linux") or any(value <= 0 or value % 2 for value in args.scales + [args.recovery_clients])
            or args.repeats <= 0 or args.timeout <= 0 or not 1 <= args.snapshot_interval <= 128
            or not 0 < args.seed < 2**64 or args.recovery_budget_ms <= 0 or args.adoption_percent <= 0):
        parser.error("Linux, even positive client counts, positive bounds and a uint64 seed are required")
    args.checkpoint_batch_size = 16
    experiment = Experiment(args)
    original = os.environ.copy()
    summary = {"passed": False, "workloads": experiment.rows, "recovery": experiment.recovery_rows,
        "source_commit": os.getenv("ARENA_SOURCE_COMMIT", "unknown"),
        "server_path": str(Path(args.server).resolve()), "server_sha256": hashlib.sha256(Path(args.server).read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "configuration": vars(args), "started_at": datetime.now(timezone.utc).isoformat()}
    async def run():
        for workload in args.workloads:
            for count in args.scales:
                for repeat in range(1, args.repeats + 1):
                    # Alternate paired ordering to reduce temperature/order bias.
                    for mode in (("full", "tail") if repeat % 2 else ("tail", "full")):
                        await experiment.workload(mode, count, workload, repeat)
        if not args.skip_recovery:
            for repeat in range(1, args.repeats + 1):
                for mode in (("full", "tail") if repeat % 2 else ("tail", "full")):
                    await experiment.recovery(mode, repeat)
    try:
        experiment.setup()
        asyncio.run(run())
        summary["comparison"] = comparison(experiment.rows, experiment.recovery_rows, args)
        summary["passed"] = True
        return 0
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        try:
            with contextlib.suppress(Exception):
                experiment.matches.extend(experiment.sql("SELECT match_id FROM matches").splitlines())
            experiment.cleanup()
            summary["fixtures_cleaned"] = True
        except Exception as error:
            summary.update(passed=False, fixtures_cleaned=False, cleanup_error=str(error))
            if not primary_error:
                raise
        finally:
            os.environ.clear()
            os.environ.update(original)
            summary["completed_at"] = datetime.now(timezone.utc).isoformat()
            (experiment.evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print("Evidence: " + str(experiment.evidence), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
