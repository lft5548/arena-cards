from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.ranking.rebuild_leaderboard import PlayerRating, RedisClient, rebuild


class RedisLeaderboardTest(unittest.TestCase):
    def test_rebuild_orders_by_rating_and_replaces_old_values(self):
        host = os.getenv("ARENA_REDIS_HOST", "127.0.0.1")
        port = int(os.getenv("ARENA_REDIS_PORT", "6379"))
        key = "arena:test:leaderboard:" + uuid.uuid4().hex
        try:
            client = RedisClient(host, port)
        except OSError as exc:
            self.skipTest(f"Redis unavailable: {exc}")
        try:
            client.command("ZADD", key, "999", "stale")
            count = rebuild([
                PlayerRating("alice", 1200),
                PlayerRating("bob", 1100),
            ], host, port, key)
            self.assertEqual(count, 2)
            self.assertEqual(client.command("ZRANGE", key, "0", "-1"), ["bob", "alice"])
            self.assertEqual(client.command("ZSCORE", key, "stale"), None)
        finally:
            client.command("DEL", key)
            client.close()


if __name__ == "__main__":
    unittest.main()
