"""FastAPI facade tests; skipped when optional HTTP dependencies are absent."""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURE_COUNTERS = (
    "settlement_outbox_load_failures", "settlement_outbox_mark_failures",
    "settlement_outbox_record_failures", "mysql_connection_attempts",
    "mysql_connection_successes", "mysql_connection_failures",
    "mysql_connection_losses", "redis_connection_failures", "redis_apply_failures",
)
CHECKPOINT_COUNTERS = (
    "recovery_checkpoint_attempts", "recovery_checkpoint_successes", "recovery_checkpoint_failures",
    "recovery_checkpoint_bytes_total", "recovery_checkpoint_bytes_max",
    "recovery_checkpoint_serialize_us_total", "recovery_checkpoint_write_us_total", "recovery_checkpoint_write_us_max",
    "recovery_checkpoint_lock_wait_us_total", "recovery_checkpoint_connection_us_total",
    "recovery_checkpoint_sql_us_total", "recovery_checkpoint_commit_us_total",
    "recovery_checkpoint_queue_wait_us_total", "recovery_checkpoint_batches", "recovery_checkpoint_batch_items_max",
)
NETWORK_COUNTERS = ("connections_rejected", "requests_rate_limited", "heartbeat_timeouts")


class AdminApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.module = importlib.import_module("tools.admin_api.app")
            from fastapi.testclient import TestClient
            cls.TestClient = TestClient
        except ImportError as exc:
            raise unittest.SkipTest(f"FastAPI dependencies unavailable: {exc}")

    def test_command_allowlist_rejects_game_messages(self):
        with self.TestClient(self.module.app) as client:
            response = client.post("/command", json={"type": "LoginReq", "payload": {}})
        self.assertEqual(response.status_code, 400)

    def test_metrics_normalizes_admin_response(self):
        async def fake_command(msg_type, payload):
            return {"type": "AdminRoomsResp", "payload": {"active_rooms": "2", "active_sessions": "4"}}

        with patch.object(self.module, "command", new=AsyncMock(side_effect=fake_command)):
            with self.TestClient(self.module.app) as client:
                response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active_rooms"], 2)
        self.assertEqual(response.json()["active_sessions"], 4)
        for name in FAILURE_COUNTERS + CHECKPOINT_COUNTERS + NETWORK_COUNTERS:
            self.assertNotIn(name, response.json())

    def test_metrics_exposes_runtime_counters(self):
        async def fake_command(msg_type, payload):
            return {
                "type": "AdminRoomsResp",
                "payload": {
                    "active_rooms": "1", "active_sessions": "2",
                    "room_commands_enqueued": "9",
                    "room_commands_processed": "8",
                    "send_frames_dropped": "1",
                    "settlement_latency_ms_max": "14",
                    "settlement_outbox_applied": "3",
                    "settlement_outbox_failures": "1",
                    "settlement_outbox_pending": "2",
                    "replays_saved": "7",
                    "replay_save_failures": "1",
                },
        }

        with patch.object(self.module, "command", new=AsyncMock(side_effect=fake_command)):
            with self.TestClient(self.module.app) as client:
                response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["room_commands_enqueued"], 9)
        self.assertEqual(response.json()["send_frames_dropped"], 1)
        self.assertEqual(response.json()["settlement_latency_ms_max"], 14)
        self.assertEqual(response.json()["settlement_outbox_applied"], 3)
        self.assertEqual(response.json()["settlement_outbox_failures"], 1)
        self.assertEqual(response.json()["settlement_outbox_pending"], 2)
        self.assertEqual(response.json()["replays_saved"], 7)
        self.assertEqual(response.json()["replay_save_failures"], 1)

    def test_metrics_exposes_failure_classification_without_integer_loss(self):
        values = {name: index for index, name in enumerate(FAILURE_COUNTERS, 1)}
        values["mysql_connection_attempts"] = 2**64 - 1
        values["redis_apply_failures"] = 2**63
        payload = {"active_rooms": "0", "active_sessions": "1"}
        payload.update({name: str(value) for name, value in values.items()})
        with patch.object(self.module, "command", new=AsyncMock(return_value={
                "type": "AdminRoomsResp", "payload": payload})):
            with self.TestClient(self.module.app) as client:
                response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        for name, value in values.items():
            with self.subTest(metric=name):
                self.assertEqual(response.json()[name], value)
                self.assertIsInstance(response.json()[name], int)

    def test_metrics_rejects_invalid_failure_classification_values(self):
        for name in FAILURE_COUNTERS:
            with self.subTest(metric=name):
                with patch.object(self.module, "command", new=AsyncMock(return_value={
                        "type": "AdminRoomsResp", "payload": {name: "not-an-integer"}})):
                    with self.TestClient(self.module.app) as client:
                        response = client.get("/metrics")
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"], f"invalid metric: {name}")

    def test_metrics_reports_unexpected_backend_response(self):
        with patch.object(self.module, "command", new=AsyncMock(return_value={"type": "Pong", "payload": {}})):
            with self.TestClient(self.module.app) as client:
                response = client.get("/metrics")
        self.assertEqual(response.status_code, 502)

    def test_network_metrics_are_optional_lossless_unsigned_counters(self):
        for name in NETWORK_COUNTERS:
            for value in (0, 2**63, 2**64 - 1):
                with self.subTest(metric=name, value=value):
                    with patch.object(self.module, "command", new=AsyncMock(return_value={
                            "type": "AdminRoomsResp", "payload": {name: str(value)}})):
                        with self.TestClient(self.module.app) as client:
                            response = client.get("/metrics")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()[name], value)
                    self.assertIsInstance(response.json()[name], int)
            for value in ("invalid", "-1", str(2**64)):
                with self.subTest(metric=name, value=value):
                    with patch.object(self.module, "command", new=AsyncMock(return_value={
                            "type": "AdminRoomsResp", "payload": {name: value}})):
                        with self.TestClient(self.module.app) as client:
                            response = client.get("/metrics")
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(response.json()["detail"], f"invalid metric: {name}")

    def test_checkpoint_metrics_are_lossless_and_validate_numbers(self):
        payload = {name: str(2**64 - 1) for name in CHECKPOINT_COUNTERS}
        with patch.object(self.module, "command", new=AsyncMock(return_value={
                "type": "AdminRoomsResp", "payload": payload})):
            with self.TestClient(self.module.app) as client:
                response = client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        for name in CHECKPOINT_COUNTERS:
            self.assertEqual(response.json()[name], 2**64 - 1)
        for name in CHECKPOINT_COUNTERS:
            with self.subTest(metric=name):
                with patch.object(self.module, "command", new=AsyncMock(return_value={
                        "type": "AdminRoomsResp", "payload": {name: "invalid"}})):
                    with self.TestClient(self.module.app) as client:
                        response = client.get("/metrics")
                self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
