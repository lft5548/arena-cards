#pragma once

#include <atomic>
#include <cstdint>
#include <memory>

#include "server/gateway/session.h"
#include "server/metrics/counters.h"
#include "server/room/room_registry.h"
#include "server/app/shutdown.h"

namespace arena {
namespace persistence { class MysqlStore; }
namespace ranking { class RedisLeaderboard; }

struct Runtime {
  ShutdownTracker shutdown;
  bool room_recovery_enabled = false;
  bool room_recovery_tail = false;
  std::uint64_t recovery_snapshot_interval = 16;
  std::shared_ptr<persistence::MysqlStore> mysql_store;
  std::shared_ptr<ranking::RedisLeaderboard> redis_leaderboard;
  std::atomic<unsigned long long> match_sequence{1};
  gateway::Metrics gateway_metrics;
  metrics::Counters counters;
  room::RoomRegistry rooms;
};

}
