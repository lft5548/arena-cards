#include <atomic>
#include <algorithm>
#include <chrono>
#include <charconv>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>

#include "server/app/runtime.h"
#include "server/app/identifiers.h"
#include "server/app/session.h"
#include "server/config/card_catalog.h"
#include "server/gateway/tcp_server.h"
#include "server/match/matchmaker.h"
#include "server/persistence/mysql_store.h"
#include "server/ranking/redis_leaderboard.h"
#include "server/room/room.h"
#include "server/room/recovery_tail.h"

namespace {

volatile std::sig_atomic_t stop_requested = 0;
void stop_signal(int) { stop_requested = 1; }

unsigned int shutdown_timeout_ms() {
  const auto value = arena::persistence::environment("ARENA_SHUTDOWN_TIMEOUT_MS", "10000");
  unsigned int parsed = 0;
  const auto result = std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (result.ec != std::errc() || result.ptr != value.data() + value.size() || parsed < 100 || parsed > 60000)
    throw std::invalid_argument("ARENA_SHUTDOWN_TIMEOUT_MS must be in 100..60000");
  return parsed;
}

// A blocked SQL callback cannot be safely cancelled or rolled back from another
// thread. Fail the process at the shared deadline; durable recovery resolves
// any uncertain commit after restart. Never report that as a graceful success.
class ShutdownDeadline {
 public:
  explicit ShutdownDeadline(std::chrono::steady_clock::time_point deadline)
      : worker_([this, deadline]() {
          std::unique_lock<std::mutex> lock(mutex_);
          if (!changed_.wait_until(lock, deadline, [this] { return completed_; })) std::_Exit(4);
        }) {}
  ~ShutdownDeadline() { complete(); }
  void complete() {
    { std::lock_guard<std::mutex> lock(mutex_); completed_ = true; }
    changed_.notify_all();
    if (worker_.joinable()) worker_.join();
  }
 private:
  std::mutex mutex_;
  std::condition_variable changed_;
  bool completed_ = false;
  std::thread worker_;
};

void refresh_outbox_pending(const std::shared_ptr<arena::persistence::MysqlStore>& store,
                            arena::Runtime& runtime) {
  unsigned long long pending = 0;
  std::string error;
  if (store->pending_outbox_count(pending, error)) {
    runtime.counters.settlement_outbox_pending.store(pending, std::memory_order_relaxed);
  } else {
    runtime.counters.settlement_outbox_failures.fetch_add(1, std::memory_order_relaxed);
    runtime.counters.settlement_outbox_load_failures.fetch_add(1, std::memory_order_relaxed);
    std::cerr << "settlement outbox count failed: " << error << "\n";
  }
}

void consume_pending_outbox(const std::shared_ptr<arena::persistence::MysqlStore>& store,
                            const std::shared_ptr<arena::ranking::RedisLeaderboard>& redis,
                            arena::Runtime& runtime) {
  std::vector<arena::persistence::PendingSettlement> entries;
  std::string error;
  if (!store->list_pending_outbox(entries, 256, error)) {
    runtime.counters.settlement_outbox_failures.fetch_add(1, std::memory_order_relaxed);
    runtime.counters.settlement_outbox_load_failures.fetch_add(1, std::memory_order_relaxed);
    std::cerr << "settlement outbox load failed: " << error << "\n";
    return;
  }
  for (const auto& entry : entries) {
    const bool no_rating_update = entry.winner_id.empty() && entry.loser_id.empty() &&
                                  entry.winner_rating_delta == 0 && entry.loser_rating_delta == 0;
    bool applied = no_rating_update;
    std::string processing_error;
    // Draws need no cache operation, including when Redis is disabled/offline.
    if (!applied && (!redis || !redis->connected())) continue;
    if (!applied && !redis->apply(entry.match_id, entry.winner_id, entry.loser_id,
                                  entry.winner_rating_delta, entry.loser_rating_delta, processing_error)) {
      runtime.counters.settlement_outbox_failures.fetch_add(1, std::memory_order_relaxed);
      runtime.counters.redis_apply_failures.fetch_add(1, std::memory_order_relaxed);
      std::string record_error;
      if (!store->record_outbox_failure(entry.match_id, processing_error, record_error)) {
        runtime.counters.settlement_outbox_record_failures.fetch_add(1, std::memory_order_relaxed);
        std::cerr << "settlement outbox failure record failed: " << record_error << "\n";
      }
      // A timeout invalidates the connection. Reconnect on the next poll
      // instead of attempting the rest of this batch on a failed socket.
      if (!redis->connected()) break;
      continue;
    }
    applied = store->mark_outbox_applied(entry.match_id, processing_error);
    if (applied) {
      runtime.counters.settlement_outbox_applied.fetch_add(1, std::memory_order_relaxed);
    } else {
      runtime.counters.settlement_outbox_failures.fetch_add(1, std::memory_order_relaxed);
      std::string record_error;
      runtime.counters.settlement_outbox_mark_failures.fetch_add(1, std::memory_order_relaxed);
      if (!store->record_outbox_failure(entry.match_id, processing_error, record_error)) {
        runtime.counters.settlement_outbox_record_failures.fetch_add(1, std::memory_order_relaxed);
        std::cerr << "settlement outbox failure record failed: " << record_error << "\n";
      }
    }
  }
  refresh_outbox_pending(store, runtime);
}

unsigned int outbox_poll_seconds() {
  const auto value = arena::persistence::environment("ARENA_OUTBOX_POLL_SECONDS", "2");
  char* end = nullptr;
  const auto parsed = std::strtoul(value.c_str(), &end, 10);
  if (end == value.c_str() || *end != '\0') return 2;
  return static_cast<unsigned int>(std::max<unsigned long>(1, std::min<unsigned long>(parsed, 60)));
}

void run_outbox_worker(const std::shared_ptr<arena::persistence::MysqlStore>& store,
                       const std::shared_ptr<arena::ranking::RedisLeaderboard>& redis,
                       arena::Runtime& runtime, const std::string& host, unsigned short port,
                       unsigned int poll_seconds, const std::atomic<bool>& stop) {
  while (!stop.load(std::memory_order_acquire)) {
    std::string mysql_error;
    if (!store->reconnect(mysql_error) && !mysql_error.empty()) {
      std::cerr << "MySQL pool maintenance failed: " << mysql_error << "\n";
    }
    if (redis && !redis->connected()) {
      std::string error;
      if (!redis->connect(host, port, error)) {
        runtime.counters.settlement_outbox_failures.fetch_add(1, std::memory_order_relaxed);
        runtime.counters.redis_connection_failures.fetch_add(1, std::memory_order_relaxed);
        std::cerr << "settlement outbox Redis reconnect failed: " << error << "\n";
      }
    }
    consume_pending_outbox(store, redis, runtime);
    for (unsigned int tick = 0; tick < poll_seconds * 10; ++tick) {
      if (stop.load(std::memory_order_acquire)) return;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
  }
}

std::string resolve_card_config(int argc, char** argv) {
  if (argc > 2) return argv[2];
  const auto explicit_path = arena::persistence::environment("ARENA_CARD_CONFIG");
  if (!explicit_path.empty()) return explicit_path;
  const auto config_dir = arena::persistence::environment("ARENA_CONFIG_DIR");
  if (!config_dir.empty()) return config_dir + "/cards.csv";
  const std::vector<std::string> candidates{
      "server/config/cards.csv", "config/cards.csv", "../server/config/cards.csv", "../config/cards.csv"};
  for (const auto& candidate : candidates) {
    std::ifstream input(candidate);
    if (input.good()) return candidate;
  }
  return candidates.front();
}

}

int main(int argc, char** argv) {
  if (argc > 1 && std::string(argv[1]) == "--help") {
    std::cout << "Usage: arena_server [port] [card_config.csv]\n";
    return 0;
  }
  arena::gateway::SessionOptions network_options;
  unsigned int shutdown_budget = 10000;
  try {
    network_options = arena::gateway::SessionOptions::from_environment();
    shutdown_budget = shutdown_timeout_ms();
  } catch (const std::exception& error) {
    std::cerr << "network config error: " << error.what() << "\n";
    return 2;
  }
  std::signal(SIGINT, stop_signal);
  std::signal(SIGTERM, stop_signal);
#ifdef _WIN32
  std::signal(SIGBREAK, stop_signal);
#endif
  std::thread outbox_worker;
  std::atomic<bool> stop_outbox_worker{false};
  std::string redis_host;
  unsigned short redis_port = 6379;
  const auto port = argc > 1 ? static_cast<std::uint16_t>(std::atoi(argv[1])) : std::uint16_t{9000};
  const auto bind_address = arena::persistence::environment("ARENA_BIND_ADDRESS", "0.0.0.0");
  const auto config_path = resolve_card_config(argc, argv);
  auto catalog = std::make_shared<arena::config::CardCatalog>();
  std::string error;
  if (!catalog->load_csv(config_path, error)) {
    std::cerr << "card config error: " << error << "\n";
    return 2;
  }
  std::cout << "loaded card config: " << config_path << "\n";
  auto runtime = std::make_shared<arena::Runtime>();
  std::vector<std::shared_ptr<arena::Room>> recovered_rooms;
  const bool mysql_enabled = arena::persistence::truthy(arena::persistence::environment("ARENA_MYSQL_ENABLED"));
  const bool mysql_required = arena::persistence::truthy(arena::persistence::environment("ARENA_MYSQL_REQUIRED"));
  runtime->room_recovery_enabled = arena::persistence::truthy(
      arena::persistence::environment("ARENA_ROOM_RECOVERY"));
  const auto recovery_mode = arena::persistence::environment("ARENA_ROOM_RECOVERY_MODE", "full");
  if (recovery_mode != "full" && recovery_mode != "tail") {
    std::cerr << "ARENA_ROOM_RECOVERY_MODE must be full or tail\n";
    return 3;
  }
  runtime->room_recovery_tail = recovery_mode == "tail";
  const auto interval_text = arena::persistence::environment("ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL", "16");
  std::uint64_t interval = 0;
  const auto parsed_interval = std::from_chars(interval_text.data(), interval_text.data() + interval_text.size(), interval);
  if (parsed_interval.ec != std::errc() || parsed_interval.ptr != interval_text.data() + interval_text.size() ||
      interval < 1 || interval > 128) {
    std::cerr << "ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL must be an integer from 1 to 128\n";
    return 3;
  }
  runtime->recovery_snapshot_interval = interval;
  const bool cleanup_running = arena::persistence::truthy(
      arena::persistence::environment("ARENA_MYSQL_CLEANUP_RUNNING"));
  if (runtime->room_recovery_enabled && (!mysql_required || cleanup_running)) {
    std::cerr << "room recovery requires ARENA_MYSQL_REQUIRED=1 and ARENA_MYSQL_CLEANUP_RUNNING=0\n";
    return 3;
  }
  if (mysql_enabled || mysql_required) {
    auto store = std::make_shared<arena::persistence::MysqlStore>();
    if (runtime->room_recovery_enabled) {
      store->set_room_recovery_owner(arena::app::make_token());
      const auto batch_text = arena::persistence::environment("ARENA_CHECKPOINT_BATCH_SIZE", "16");
      char* end = nullptr;
      const auto count = std::strtoul(batch_text.c_str(), &end, 10);
      if (end == batch_text.c_str() || *end != '\0' || count < 1 || count > 32) {
        std::cerr << "ARENA_CHECKPOINT_BATCH_SIZE must be an integer from 1 to 32\n";
        return 3;
      }
      store->configure_checkpoint_batching(static_cast<unsigned int>(count));
    }
    const auto database_name = arena::persistence::environment("ARENA_MYSQL_DATABASE");
    const auto port_text = arena::persistence::environment("ARENA_MYSQL_PORT", "3306");
    const auto database_port = static_cast<unsigned int>(std::strtoul(port_text.c_str(), nullptr, 10));
    const auto pool_size_text = arena::persistence::environment("ARENA_MYSQL_POOL_SIZE", "1");
    const auto pool_size = arena::persistence::MysqlStore::normalize_pool_size(
        static_cast<unsigned int>(std::strtoul(pool_size_text.c_str(), nullptr, 10)));
    if (!store->connect(arena::persistence::environment("ARENA_MYSQL_HOST", "127.0.0.1"), database_port,
                        arena::persistence::environment("ARENA_MYSQL_USER"),
                        arena::persistence::environment("ARENA_MYSQL_PASSWORD"), database_name, error, pool_size)) {
      std::cerr << "MySQL startup error: " << error << "\n";
      return 3;
    }
    if (!store->ensure_outbox_schema(error)) {
      std::cerr << "MySQL outbox schema error: " << error << "\n";
      return 3;
    }
    const auto lock_override = arena::persistence::environment("ARENA_MYSQL_LOCK_NAME");
    const auto lock_name = lock_override.empty() ? "arena_cards_server:" + database_name : lock_override;
    if (!store->acquire_instance_lock(lock_name, error)) {
      std::cerr << "MySQL instance lock error: " << error << "\n";
      return 3;
    }
    if (!store->initialize_room_recovery(error)) {
      std::cerr << "MySQL room recovery schema error: " << error << "\n";
      return 3;
    }
    bool has_checkpoints = false;
    if (!store->has_room_checkpoints(has_checkpoints, error)) {
      std::cerr << "MySQL room recovery check error: " << error << "\n";
      return 3;
    }
    if (has_checkpoints && !runtime->room_recovery_enabled) {
      std::cerr << "durable room checkpoints require ARENA_ROOM_RECOVERY=1; refusing to discard rooms\n";
      return 3;
    }
    if (cleanup_running) {
      unsigned long long cleaned_count = 0;
      if (!store->cleanup_running_matches(cleaned_count, error)) {
        std::cerr << "MySQL startup cleanup error: " << error << "\n";
        return 3;
      }
      if (cleaned_count > 0)
        std::cout << "MySQL startup cleanup marked " << cleaned_count << " running match(es) as aborted\n";
    }
    runtime->mysql_store = store;
    if (runtime->room_recovery_enabled) {
      if (!store->validate_room_recovery_coverage(error)) {
        std::cerr << "MySQL room recovery coverage error: " << error << "\n";
        return 3;
      }
      std::vector<arena::persistence::RoomRecoveryRow> rows;
      if (!store->load_room_checkpoints(rows, error)) {
        std::cerr << "MySQL room recovery load error: " << error << "\n";
        return 3;
      }
      std::unordered_set<std::string> tokens;
      for (const auto& row : rows) {
        arena::room::RecoveryCheckpoint checkpoint;
        if (!arena::room::RecoveryCodec::decode(row.checkpoint, checkpoint, error)) {
          std::cerr << "room recovery snapshot failed for " << row.match_id << ": " << error << "\n";
          return 3;
        }
        auto sequence = row.snapshot_sequence;
        for (const auto& tail : row.tails) {
          arena::room::RecoveryCheckpoint next;
          if (sequence == std::numeric_limits<std::uint64_t>::max() || tail.first != sequence + 1 ||
              !arena::room::RecoveryTailCodec::apply(checkpoint, tail.second, sequence, next, error)) {
            std::cerr << "room recovery tail failed for " << row.match_id << ": " << error << "\n";
            return 3;
          }
          checkpoint = std::move(next);
          sequence = tail.first;
        }
        if ((row.recovery_format != "full" && row.recovery_format != "tail") ||
            sequence != row.current_sequence ||
            (row.recovery_format == "full" && !row.tails.empty()) ||
            checkpoint.match_id != row.match_id || checkpoint.users[0] != row.player_a ||
            checkpoint.users[1] != row.player_b ||
            (row.status != "running" && checkpoint.pending_result.empty()) ||
            !tokens.insert(checkpoint.tokens[0]).second || !tokens.insert(checkpoint.tokens[1]).second) {
          std::cerr << "room recovery validation failed for " << row.match_id << ": " << error << "\n";
          return 3;
        }
        auto target = std::make_shared<arena::Room>(nullptr, nullptr, catalog, runtime);
        if (!target->restore_checkpoint(checkpoint, error, row.current_sequence, row.snapshot_sequence,
                                        row.recovery_format == "tail")) {
          std::cerr << "room recovery restore failed for " << row.match_id << ": " << error << "\n";
          return 3;
        }
        recovered_rooms.push_back(std::move(target));
      }
      if (!store->claim_room_checkpoints(error)) {
        std::cerr << "room recovery ownership claim failed: " << error << "\n";
        return 3;
      }
      for (const auto& target : recovered_rooms) if (!target->save_checkpoint(error)) {
        std::cerr << "room recovery checkpoint update failed: " << error << "\n";
        return 3;
      }
      std::cout << "loaded " << recovered_rooms.size() << " durable room(s)\n";
    }
    refresh_outbox_pending(store, *runtime);
    std::cout << "MySQL settlement persistence enabled\n";
  }
  if (arena::persistence::truthy(arena::persistence::environment("ARENA_REDIS_ENABLED"))) {
    auto redis = std::make_shared<arena::ranking::RedisLeaderboard>();
    const auto port_text = arena::persistence::environment("ARENA_REDIS_PORT", "6379");
    redis_host = arena::persistence::environment("ARENA_REDIS_HOST", "127.0.0.1");
    redis_port = static_cast<unsigned short>(std::strtoul(port_text.c_str(), nullptr, 10));
    runtime->redis_leaderboard = redis;
    if (!redis->connect(redis_host, redis_port, error)) {
      runtime->counters.redis_connection_failures.fetch_add(1, std::memory_order_relaxed);
      if (arena::persistence::truthy(arena::persistence::environment("ARENA_REDIS_REQUIRED"))) {
        std::cerr << "Redis startup error: " << error << "\n";
        return 3;
      }
      std::cerr << "Redis unavailable at startup; outbox worker will retry: " << error << "\n";
    } else {
      std::cout << "Redis leaderboard cache enabled\n";
      if (runtime->mysql_store) consume_pending_outbox(runtime->mysql_store, redis, *runtime);
    }
  }
  if (runtime->mysql_store) {
    outbox_worker = std::thread(run_outbox_worker, runtime->mysql_store, runtime->redis_leaderboard,
                                std::ref(*runtime), redis_host, redis_port, outbox_poll_seconds(),
                                std::ref(stop_outbox_worker));
  }
  for (const auto& target : recovered_rooms) {
    // Registry bindings are attached by the room only after all documents have
    // passed validation, so a corrupt room cannot start partial recovery.
    target->attach_recovered_tokens();
    runtime->counters.active_rooms.fetch_add(1, std::memory_order_relaxed);
    target->launch_actor();
  }
  recovered_rooms.clear();
  auto matchmaker = std::make_shared<arena::Matchmaker>(catalog, runtime);
  arena::gateway::TcpServer server(port, bind_address, [matchmaker, runtime]() {
    return std::make_shared<arena::Session>(matchmaker, runtime);
  }, runtime->gateway_metrics, network_options);
  std::unique_ptr<ShutdownDeadline> shutdown_guard;
  std::chrono::steady_clock::time_point deadline;
  const auto begin_shutdown = [&](std::chrono::steady_clock::time_point limit) {
    deadline = limit;
    shutdown_guard = std::make_unique<ShutdownDeadline>(deadline);
    runtime->shutdown.begin();
    stop_outbox_worker.store(true, std::memory_order_release);
    matchmaker->stop();
  };
  const int result = server.run([] { return stop_requested != 0; }, begin_shutdown,
                                std::chrono::milliseconds(shutdown_budget));
  if (!shutdown_guard) begin_shutdown(std::chrono::steady_clock::now() + std::chrono::milliseconds(shutdown_budget));
  if (result != 0) {
    std::cerr << "shutdown: transport drain failed\n";
    std::_Exit(4);
  }
  auto rooms = runtime->shutdown.rooms();
  for (const auto& room : rooms) room->request_shutdown();
  rooms.clear();
  if (!runtime->shutdown.drain_until(deadline)) {
    std::cerr << "shutdown: application drain failed\n";
    std::_Exit(4);
  }
  stop_outbox_worker.store(true, std::memory_order_release);
  if (outbox_worker.joinable()) outbox_worker.join();
  runtime->redis_leaderboard.reset();
  runtime->mysql_store.reset();
  std::cout << "shutdown: complete\n" << std::flush;
  shutdown_guard->complete();
  return 0;
}
