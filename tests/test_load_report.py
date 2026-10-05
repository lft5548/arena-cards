import unittest

from tools.bot.scale_load_test import markdown, parse_bytes, percentile, summarize_samples


class LoadReportTests(unittest.TestCase):
    def test_percentile_and_units(self):
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(parse_bytes("1.5MiB"), 1572864)
        self.assertEqual(parse_bytes("12 MB"), 12000000)

    def test_markdown_uses_load_test_percentile_keys(self):
        report = {
            'generated_at': '2026-10-03T00:00:00+00:00',
            'host': '127.0.0.1', 'port': 9000, 'rounds_per_client': 1,
            'containers': [],
            'scales': [{
                'clients': 20, 'success_rate': 1.0, 'exit_code': 0,
                'throughput_rounds_per_sec': 2.5,
                'latency': {
                    'action_ack': {'p50_ms': 1.0, 'p95_ms': 2.0, 'p99_ms': 3.0},
                    'completed_round': {'p50_ms': 10.0, 'p95_ms': 20.0, 'p99_ms': 30.0},
                },
                'docker': {'containers': {}},
            }],
        }
        rendered = markdown(report)
        self.assertIn('| 20 | 10 | 1.0 | 2.5 | 1.0/2.0/3.0 | 10.0/20.0/30.0 |', rendered)
    def test_docker_summary_uses_peaks(self):
        summary = summarize_samples([
            {"arena-cards-server": {"cpu_percent": 1.5, "memory_bytes": 10}},
            {"arena-cards-server": {"cpu_percent": 4.0, "memory_bytes": 20}},
        ])
        self.assertTrue(summary["available"])
        self.assertEqual(summary["containers"]["arena-cards-server"]["peak_cpu_percent"], 4.0)
        self.assertEqual(summary["containers"]["arena-cards-server"]["peak_memory_bytes"], 20)


if __name__ == "__main__":
    unittest.main()
