import json
from pathlib import Path
import tempfile
import unittest

from tools.build_support.publish_evidence import digest, export, refresh_manifest, sanitize


class EvidencePublicationTests(unittest.TestCase):
    def test_private_metadata_removed_and_measurements_preserved(self):
        value = {"started_at": "2030-01-01", "server_path": "C:/Users/private/server",
                 "token": "secret", "configuration": {"mysql_password": "secret"},
                 "passed": True, "duration_ms": 18.4, "metrics": {"commit_us": 500},
                 "results": [{"run": "private/build", "match_id": "fixture", "original_ack_verified": True}]}
        public = sanitize(value)
        self.assertEqual(public["duration_ms"], 18.4)
        self.assertEqual(public["metrics"], value["metrics"])
        self.assertTrue(public["results"][0]["original_ack_verified"])
        text = json.dumps(public)
        for private in ("secret", "fixture", "C:/Users", "2030-01-01", "private/build"):
            self.assertNotIn(private, text)

    def test_jsonl_sources_remain_correlated(self):
        value = [{"sources": [{"run": "a", "server_sha256": "abc"}]}, {"run": "a", "duration_ms": 100}]
        public = sanitize(value)
        self.assertEqual(public[0]["sources"][0]["run"], public[1]["run"])
        self.assertEqual(public[0]["sources"][0]["server_sha256"], "abc")

    def test_export_keeps_original_and_records_distinct_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.json"
            original.write_text(json.dumps({"completed_at": "2030-01-01", "passed": False}), encoding="utf-8")
            raw = original.read_bytes()
            target = root / "docs/evidence/result.json"
            hashes = export(original, target)
            refresh_manifest(root, [("docs/evidence/result.json", hashes["source_sha256"])])
            self.assertEqual(original.read_bytes(), raw)
            self.assertFalse(json.loads(target.read_text())["passed"])
            manifest = json.loads((root / "docs/evidence/manifest.json").read_text())
            row = manifest["files"][0]
            self.assertEqual(row["source_sha256"], digest(raw))
            self.assertEqual(row["published_sha256"], digest(target.read_bytes()))
            self.assertNotEqual(row["source_sha256"], row["published_sha256"])

    def test_manifest_rejects_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            outside = root.parent / "private.json"
            outside.write_text("{}")
            with self.assertRaises(ValueError):
                refresh_manifest(root, [("../private.json", "hash")])
