import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.bot.recovery_report import collect_records, publish_runs


class RecoveryReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def create_run(self, name, rows, **metadata):
        run = self.root / name
        run.mkdir()
        matrix = []
        for index, values in enumerate(rows):
            row = {"passed": True, "scenario": "baseline", "protocol": "text_v1",
                   "clients": 20, "recovery": True, "completed_matches": 10, "success_rate": 1.0,
                   "throughput_matches_per_second": 1.5, "checkpoint_mean_write_ms": 100,
                   "checkpoint_mean_bytes": 10000,
                   "latency": {"action_ack": {"p50_ms": 1, "p95_ms": 2, "p99_ms": 3}},
                   "resources": {"peak_cpu_percent": 99, "peak_rss_bytes": 1048576},
                   "metrics": {}, **values}
            raw = run / f"case{index}.json"
            raw.write_text(json.dumps({"report": row}), encoding="utf-8")
            matrix.append({**row, "raw_file": raw.name,
                           "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest()})
        summary = {"passed": True, "fixtures_cleaned": True, "server_sha256": name,
                   "matrix": matrix, **metadata}
        (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return run

    def update_summary(self, run, edit):
        path = run / "summary.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        edit(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_publish_mixed_generations_uses_measured_dates_scales_and_batch(self):
        legacy = self.create_run("legacy", [{"metrics": {"recovery_checkpoint_lock_wait_us_total": 58364829}}],
                                 completed_at="2026-10-03T17:00:00+00:00")
        current = self.create_run("current", [
            {"clients": 100, "completed_matches": 50, "checkpoint_batch_size": 16},
            {"clients": 200, "completed_matches": 100, "checkpoint_batch_size": 16,
             "scenario": "redis-partition", "protocol": "proto_v1"},
        ], completed_at="2026-10-05T02:00:00+00:00", server_path="build-linux-release/arena_server")
        output = self.root / "report.md"
        self.assertEqual(publish_runs([legacy, current], output), 3)
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("2026-10-04 至 2026-10-05", markdown)
        self.assertIn("| redis-partition | proto_v1 | 200/100 | 1 | 16 |", markdown)
        self.assertIn("--checkpoint-batch-size 1 --scenarios baseline --scales 20", markdown)
        self.assertIn("--checkpoint-batch-size 16 --scenarios baseline --scales 100", markdown)
        self.assertIn("--checkpoint-batch-size 16 --scenarios redis-partition --scenario-count 200", markdown)
        self.assertIn("58.365", markdown)
        self.assertIn("累计秒", markdown)
        self.assertNotIn("58.365ms", markdown)
        self.assertNotIn("均为20 Bot", markdown)
        records = [json.loads(line) for line in output.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["schema_version"], 2)
        self.assertEqual(records[1]["checkpoint_batch_size"], 1)
        self.assertEqual(records[1]["checkpoint_batch_size_source"], "legacy_unbatched_inferred")
        self.assertEqual(records[3]["checkpoint_batch_size"], 16)
        self.assertEqual(records[2]["recorded_at"], "2026-10-05T10:00:00+08:00")

    def test_missing_legacy_time_is_explicitly_inferred_from_raw_file(self):
        run = self.create_run("legacy", [{}])
        os.utime(run / "case0.json", (0, 0))
        records, sources = collect_records([run])
        self.assertEqual(records[0]["recorded_at"], "1970-01-01T08:00:00+08:00")
        self.assertEqual(sources[0]["recorded_at_sources"], ["raw_file_mtime_inferred"])

    def test_reproduce_disabled_comparison_and_multiple_redis_scenarios(self):
        run = self.create_run("new", [
            {"checkpoint_batch_size": 16, "recovery": False},
            {"checkpoint_batch_size": 16},
            {"checkpoint_batch_size": 16, "scenario": "redis-outage", "clients": 100, "completed_matches": 50},
            {"checkpoint_batch_size": 16, "scenario": "redis-reply-loss", "clients": 100, "completed_matches": 50},
        ], completed_at="2026-10-05")
        output = self.root / "report.md"
        publish_runs([run], output)
        markdown = output.read_text(encoding="utf-8")
        self.assertIn("--scenarios baseline --scales 20 --compare-disabled", markdown)
        self.assertIn("--scenarios redis-outage redis-reply-loss --scenario-count 100", markdown)

    def test_reject_incomplete_unclean_empty_or_failed_evidence(self):
        changes = (
            lambda summary: summary.update(passed=False),
            lambda summary: summary.update(fixtures_cleaned=False),
            lambda summary: summary.update(matrix=[]),
            lambda summary: summary["matrix"][0].update(passed=False),
        )
        for index, edit in enumerate(changes):
            with self.subTest(index=index):
                run = self.create_run(str(index), [{}])
                self.update_summary(run, edit)
                with self.assertRaises(ValueError):
                    publish_runs([run], self.root / f"bad{index}.md")
                self.assertFalse((self.root / f"bad{index}.md").exists())

    def test_reject_changed_raw_file_and_summary_measurements(self):
        run = self.create_run("bad-hash", [{}])
        with (run / "case0.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            collect_records([run])
        for key, altered in (
            ("clients", 200),
            ("resources", {"peak_cpu_percent": 1, "peak_rss_bytes": 1}),
            ("throughput_matches_per_second", 999),
            ("checkpoint_mean_write_ms", 1),
            ("injected_unmeasured_key", None),
        ):
            with self.subTest(key=key):
                run = self.create_run("bad-summary-" + key, [{}])
                self.update_summary(run, lambda summary: summary["matrix"][0].update({key: altered}))
                with self.assertRaisesRegex(ValueError, "summary differs"):
                    collect_records([run])

    def test_batch_configuration_cannot_be_inferred_for_batched_metrics(self):
        run = self.create_run("batched-without-config", [
            {"metrics": {"recovery_checkpoint_batches": 80}},
        ])
        with self.assertRaisesRegex(ValueError, "missing checkpoint_batch_size"):
            collect_records([run])
        for invalid in (0, 33, True):
            run = self.create_run("invalid-" + str(invalid), [{"checkpoint_batch_size": invalid}])
            with self.assertRaisesRegex(ValueError, "invalid checkpoint_batch_size"):
                collect_records([run])


if __name__ == "__main__":
    unittest.main()
