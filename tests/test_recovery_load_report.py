import unittest

from tools.bot.recovery_load_test import completed_matches, resource_summary


class RecoveryLoadReportTest(unittest.TestCase):
    def test_player_rounds_are_not_match_throughput(self):
        report = {"clients": 20, "rounds_per_client": 1, "completed_rounds": 20,
                  "results": [{"errors": 0}] * 20}
        self.assertEqual(completed_matches(report), 10)
        report["completed_rounds"] = 19
        with self.assertRaises(AssertionError):
            completed_matches(report)

    def test_missing_bot_cannot_be_hidden_by_success_rate(self):
        report = {"clients": 20, "rounds_per_client": 1, "completed_rounds": 20,
                  "results": [{"errors": 0}] * 19, "success_rate": 1.0}
        with self.assertRaises(AssertionError):
            completed_matches(report)
        report["results"].append({"errors": 1})
        with self.assertRaises(AssertionError):
            completed_matches(report)

    def test_process_cpu_uses_elapsed_time_and_retains_rss_units(self):
        samples = [{"at": 1.0, "cpu_seconds": 0.4, "rss_bytes": 4096},
                   {"at": 1.2, "cpu_seconds": 0.6, "rss_bytes": 8192}]
        summary = resource_summary(samples)
        self.assertEqual(summary["peak_cpu_percent"], 100.0)
        self.assertEqual(summary["peak_rss_bytes"], 8192)
        self.assertEqual(summary["sample_count"], 2)
        self.assertIsNone(resource_summary([])["peak_cpu_percent"])


if __name__ == "__main__":
    unittest.main()
