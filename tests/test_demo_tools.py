"""Deployment guards and release isolation, without changing Docker services."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from tools.demo import demo


class DemoTests(unittest.TestCase):
    def test_profile_overrides_unsafe_inherited_settings(self):
        inherited = {"ARENA_ROOM_RECOVERY": "0", "ARENA_MYSQL_CLEANUP_RUNNING": "1",
                     "ARENA_REDIS_FAIL_APPLY_COUNT": "9", "ARENA_MYSQL_POOL_SIZE": "1",
                     "ARENA_CHECKPOINT_BATCH_SIZE": "1", "ARENA_ROOM_RECOVERY_MODE": "tail",
                     "ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL": "128", "OTHER_SETTING": "preserved"}
        result = demo.profile_environment(inherited)
        self.assertEqual({key: result[key] for key in demo.PROFILE}, demo.PROFILE)
        self.assertEqual(result["OTHER_SETTING"], "preserved")
        self.assertEqual(inherited["ARENA_ROOM_RECOVERY"], "0")

    def guarded_start(self, state=None, failure=None):
        instance = demo.DockerDemo()
        with patch.object(instance, "container_names", return_value={"arena-cards-mysql"}), \
             patch.object(instance, "inspect", return_value={"State": {"Running": True}}), \
             patch.object(instance, "database_state", return_value=state, side_effect=failure), \
             patch.object(instance, "compose") as compose:
            with self.assertRaises(demo.DemoError):
                instance.start(no_build=False)
            compose.assert_not_called()

    def test_running_match_prevents_any_compose_change(self):
        self.guarded_start({"running": 1, "pending_outbox": 0, "partial_settlements": 0})

    def test_failed_sql_query_prevents_any_compose_change(self):
        self.guarded_start(failure=demo.DemoError("Database state query failed."))

    def test_daemon_failure_is_not_treated_as_first_install(self):
        instance = demo.DockerDemo()
        with patch.object(instance, "container_names", side_effect=demo.DemoError("inventory failed")), \
             patch.object(instance, "compose") as compose:
            with self.assertRaises(demo.DemoError):
                instance.start(no_build=True)
            compose.assert_not_called()

    def test_match_created_during_build_prevents_server_restart(self):
        instance = demo.DockerDemo()
        with patch.object(instance, "container_names", return_value={"arena-cards-mysql"}), \
             patch.object(instance, "inspect", return_value={"State": {"Running": True}}), \
             patch.object(instance, "database_state", side_effect=[
                 {"running": 0, "pending_outbox": 0, "partial_settlements": 0},
                 {"running": 1, "pending_outbox": 0, "partial_settlements": 0}]), \
             patch.object(instance, "compose") as compose:
            with self.assertRaises(demo.DemoError):
                instance.start(no_build=False)
            compose.assert_called_once_with("build", "arena-server", timeout=1200)

    def test_command_failure_does_not_echo_private_diagnostics(self):
        result = subprocess.CompletedProcess([], 1, "private-token-value", "C:/Users/private/password")
        with patch.object(demo.subprocess, "run", return_value=result):
            with self.assertRaises(demo.DemoError) as caught:
                demo.run(["docker", "inspect"], label="Inspection")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))

    def test_source_zip_isolated_from_history_local_data_and_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["README.md", "new_tool.py", "removed.md", ".local/private.md",
                     ".git/config", "build-local/player.exe", "debug.log"]
            for name in names:
                if name == "removed.md":
                    continue
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("public-source" if name in ("README.md", "new_tool.py") else "PRIVATE-DATA")
            with patch.object(demo, "ROOT", root), patch.object(demo, "tracked_paths", return_value=names), \
                 patch.object(demo, "run", return_value="a" * 40):
                summary = demo.package(Path("build-delivery/source.zip"))
            with zipfile.ZipFile(root / summary["package"]) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {"arena_cards/README.md", "arena_cards/new_tool.py", "release-manifest.json"})
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))
                payloads = b"".join(archive.read(name) for name in archive.namelist())
                self.assertNotIn(b"PRIVATE-DATA", payloads)
                self.assertNotIn(str(root).encode(), payloads)
                manifest = json.loads(archive.read("release-manifest.json"))
                self.assertEqual(len(manifest["files"]), 2)
                self.assertEqual(manifest["source_state"], "dirty")
            self.assertFalse(summary["contains_git_history"])

    def test_package_rejects_paths_outside_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            with patch.object(demo, "ROOT", root):
                with self.assertRaises(demo.DemoError):
                    demo.package(Path(temporary) / "outside.zip")

    def test_optional_player_is_explicit_and_omits_logs_and_private_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("source")
            player = root / "build-player" / "ArenaCardsDemo"
            for name in ("ArenaCardsDemo.exe", "UnityPlayer.dll", "ArenaCardsDemo_Data/globalgamemanagers",
                         "client.log", "Logs/private.txt", "AcceptanceEvidence/passed.txt"):
                path = player / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("payload")
            with patch.object(demo, "ROOT", root), \
                 patch.object(demo, "tracked_paths", return_value=["README.md"]), \
                 patch.object(demo, "run", return_value="a" * 40):
                summary = demo.package(Path("build-delivery/player.zip"), player)
            with zipfile.ZipFile(root / summary["package"]) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "arena_cards/README.md", "unity_player/ArenaCardsDemo.exe", "unity_player/UnityPlayer.dll",
                    "unity_player/ArenaCardsDemo_Data/globalgamemanagers", "release-manifest.json"})
            private = root / ".local" / "player"
            private.mkdir(parents=True)
            (private / "player.exe").write_text("private")
            with patch.object(demo, "ROOT", root), \
                 patch.object(demo, "tracked_paths", return_value=["README.md"]):
                with self.assertRaises(demo.DemoError):
                    demo.package(Path("build-delivery/private.zip"), private)

    def test_check_exposes_only_safe_configuration(self):
        instance = demo.DockerDemo()
        inspected = {}
        for service, (container_port, host_port) in {
            "mysql": ("3306/tcp", "3307"), "redis": ("6379/tcp", "6379"),
            "arena-server": ("9000/tcp", "9000")}.items():
            inspected[service] = {"State": {"Running": True, "Health": {"Status": "healthy"}},
                                  "NetworkSettings": {"Ports": {container_port: [{"HostPort": host_port}]}},
                                  "Config": {"Env": []}}
        environment = dict(demo.PROFILE, ARENA_MYSQL_ENABLED="1", ARENA_MYSQL_REQUIRED="1",
                           ARENA_REDIS_ENABLED="1", ARENA_PORT="9000",
                           ARENA_MYSQL_PASSWORD="PRIVATE-PASSWORD", ARENA_RECONNECT_TOKEN="PRIVATE-TOKEN")
        inspected["arena-server"]["Config"]["Env"] = [key + "=" + value for key, value in environment.items()]
        with patch.object(instance, "inspect", side_effect=lambda service: inspected[service]), \
             patch.object(instance, "database_state", return_value={"running": 0, "pending_outbox": 0,
                                                                    "partial_settlements": 0}), \
             patch.object(demo, "check_admin", new=AsyncMock(return_value={"text_v1": "reachable",
                                                                          "proto_v1": "reachable"})):
            result = instance.check()
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("PASSWORD", encoded)
        self.assertNotIn("TOKEN", encoded)
        self.assertTrue(result["ready"])

    def test_database_probe_accepts_legacy_result_without_outbox_but_detects_partial_settlement(self):
        with sqlite3.connect(":memory:") as connection:
            connection.executescript("""
                CREATE TABLE matches(match_id TEXT PRIMARY KEY, status TEXT);
                CREATE TABLE match_results(match_id TEXT PRIMARY KEY);
                CREATE TABLE settlement_outbox(match_id TEXT PRIMARY KEY, status TEXT);
                INSERT INTO matches VALUES ('legacy', 'finished');
                INSERT INTO match_results VALUES ('legacy');
            """)
            instance = demo.DockerDemo()

            def query(*arguments, **keywords):
                values = connection.execute(keywords["input_text"]).fetchone()
                return "\t".join(str(value) for value in values)

            with patch.object(instance, "command", side_effect=query):
                self.assertEqual(instance.database_state(), {"running": 0, "pending_outbox": 0,
                                                             "partial_settlements": 0})
                instance.require_no_running_rooms()
                connection.execute("INSERT INTO matches VALUES ('missing_result', 'finished')")
                self.assertEqual(instance.database_state()["partial_settlements"], 1)
                with self.assertRaises(demo.DemoError):
                    instance.require_no_running_rooms()
                connection.execute("INSERT INTO matches VALUES ('uncommitted', 'running')")
                connection.execute("INSERT INTO settlement_outbox VALUES ('uncommitted', 'pending')")
                self.assertEqual(instance.database_state(), {"running": 1, "pending_outbox": 1,
                                                             "partial_settlements": 2})


if __name__ == "__main__":
    unittest.main()
