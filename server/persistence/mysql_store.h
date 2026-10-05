#pragma once

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "server/persistence/retry_backoff.h"
#include "server/persistence/checkpoint_batcher.h"
#include "server/room/test_fault_barrier.h"

#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
#include <mysql.h>
#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif
#endif

namespace arena::persistence {

struct PendingSettlement {
  std::string match_id;
  std::string winner_id;
  std::string loser_id;
  int winner_rating_delta = 0;
  int loser_rating_delta = 0;
};

struct RoomRecoveryRow {
  std::string match_id;
  std::string checkpoint;
  std::string status;
  std::string player_a;
  std::string player_b;
  std::string recovery_format;
  std::uint64_t snapshot_sequence = 0;
  std::uint64_t current_sequence = 0;
  std::vector<std::pair<std::uint64_t, std::string>> tails;
};

struct LeaderboardEntry {
  std::string player_id;
  std::int64_t rating = 0;
  std::uint64_t wins = 0;
  std::uint64_t losses = 0;
};

struct CheckpointTimings {
  unsigned long long lock_wait_us = 0;
  unsigned long long connection_us = 0;
  unsigned long long sql_us = 0;
  unsigned long long commit_us = 0;
  unsigned long long queue_wait_us = 0;
  unsigned int batch_size = 1;
  bool batch_leader = true;
};

class CheckpointTimer {
 public:
  enum class Phase { Lock, Connection, Sql, Commit };
  explicit CheckpointTimer(CheckpointTimings* output) : output_(output) {
    if (output_) *output_ = {};
  }
  ~CheckpointTimer() { record(); }
  void next(Phase phase) { record(); phase_ = phase; }
 private:
  void record() {
    const auto now = std::chrono::steady_clock::now();
    if (output_) {
      const auto elapsed = static_cast<unsigned long long>(
          std::chrono::duration_cast<std::chrono::microseconds>(now - started_).count());
      switch (phase_) {
        case Phase::Lock: output_->lock_wait_us += elapsed; break;
        case Phase::Connection: output_->connection_us += elapsed; break;
        case Phase::Sql: output_->sql_us += elapsed; break;
        case Phase::Commit: output_->commit_us += elapsed; break;
      }
    }
    started_ = now;
  }
  CheckpointTimings* output_;
  Phase phase_ = Phase::Lock;
  std::chrono::steady_clock::time_point started_ = std::chrono::steady_clock::now();
};

class MysqlStore {
 public:
  struct MetricsSnapshot {
    unsigned long long connection_attempts = 0;
    unsigned long long connection_successes = 0;
    unsigned long long connection_failures = 0;
    unsigned long long connection_losses = 0;
  };

  MysqlStore() = default;
  MysqlStore(const MysqlStore&) = delete;
  MysqlStore& operator=(const MysqlStore&) = delete;

  ~MysqlStore() {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    checkpoint_batcher_.reset();
    release_instance_lock();
    std::lock_guard<std::mutex> lock(mutex_);
    close_connection_locked(true);
#endif
  }

  // The pool is deliberately bounded.  A value of zero falls back to the
  // single-connection behavior retained for existing deployments.
  static unsigned int normalize_pool_size(unsigned int requested) {
    constexpr unsigned int kDefault = 1;
    constexpr unsigned int kMaximum = 8;
    if (requested == 0) return kDefault;
    return std::min(requested, kMaximum);
  }

  unsigned int pool_size() const {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
#endif
    return pool_size_;
  }

  unsigned int active_slots() const {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    return static_cast<unsigned int>(std::count_if(
        connections_.begin(), connections_.end(), [](MYSQL* handle) { return handle != nullptr; }));
#else
    return 0;
#endif
  }

  bool connect(const std::string& host, unsigned int port, const std::string& user,
               const std::string& password, const std::string& database,
               std::string& error, unsigned int pool_size = 1) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    close_connection_locked(true);
    connection_config_ = {host, port, user, password, database};
    configured_ = true;
    pool_size_ = normalize_pool_size(pool_size);
    connections_.assign(pool_size_, nullptr);
    reconnect_backoff_.reset();
    for (active_slot_ = 0; active_slot_ < connections_.size(); ++active_slot_) {
      if (!open_connection_locked(error)) {
        close_connection_locked(false);
        return false;
      }
    }
    active_slot_ = 0;
    next_slot_ = connections_.size() > 1 ? 1 : 0;
    connection_ = connections_.empty() ? nullptr : connections_[active_slot_];
    return connection_ != nullptr;
#else
    (void)host; (void)port; (void)user; (void)password; (void)database;
    (void)pool_size;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool reconnect(std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    // Reconnect is the explicit maintenance path: probe every slot so a
    // server-side KILL is detected even while the client handle is non-null,
    // then make a best-effort pass over missing slots to restore capacity.
    const std::size_t saved_slot = active_slot_;
    std::string first_failure;
    for (std::size_t index = 0; index < connections_.size(); ++index) {
      if (!connections_[index]) continue;
      active_slot_ = index;
      connection_ = connections_[index];
      if (mysql_ping(connection_) != 0) {
        const auto slot_error = mysql_error(connection_);
        if (first_failure.empty()) first_failure = slot_error;
        invalidate_connection_locked();
      }
    }
    for (std::size_t index = 0; index < connections_.size(); ++index) {
      if (connections_[index]) continue;
      if (!reconnect_backoff_.ready(RetryBackoff::Clock::now())) break;
      active_slot_ = index;
      connection_ = nullptr;
      std::string slot_error;
      if (!open_connection_locked(slot_error)) {
        if (first_failure.empty()) first_failure = slot_error;
      }
    }
    if (saved_slot < connections_.size() && connections_[saved_slot]) {
      active_slot_ = saved_slot;
      connection_ = connections_[saved_slot];
    } else {
      connection_ = nullptr;
    }
    if (!first_failure.empty()) error = first_failure;
    if (connection_ && !lock_name_.empty() && !lock_acquired_) {
      if (!acquire_instance_lock_locked(error)) return false;
    }
    return connection_ != nullptr;
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  MetricsSnapshot metrics() const {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    return {
        connection_attempts_.load(std::memory_order_relaxed),
        connection_successes_.load(std::memory_order_relaxed),
        connection_failures_.load(std::memory_order_relaxed),
        connection_losses_.load(std::memory_order_relaxed)};
#else
    return {};
#endif
  }

  // Hold a database-scoped ownership lock for the lifetime of this process.
  // A second arena instance sharing the same database must fail startup rather
  // than allowing two in-memory room owners to settle into one schema.
  bool acquire_instance_lock(const std::string& lock_name, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (lock_name.empty() || lock_name.size() > 64) {
      error = "mysql instance lock name must contain 1 to 64 bytes";
      return false;
    }
    if (!ensure_connection_locked(error)) return false;
    if (lock_acquired_) return true;
    lock_name_ = lock_name;
    return acquire_instance_lock_locked(error);
#else
    (void)lock_name;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  void release_instance_lock() {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    const bool lock_slot_available = lock_slot_ != kNoSlot && lock_slot_ < connections_.size() &&
                                     connections_[lock_slot_];
    if (lock_acquired_ && lock_slot_available) {
      active_slot_ = lock_slot_;
      connection_ = connections_[active_slot_];
    }
    if (!connection_ || !lock_acquired_ || !lock_slot_available) {
      lock_acquired_ = false;
      lock_name_.clear();
      lock_slot_ = kNoSlot;
      return;
    }
    std::string ignored;
    if (query("SELECT RELEASE_LOCK(" + quote(lock_name_) + ")", ignored) && connection_) {
      MYSQL_RES* result = mysql_store_result(connection_);
      if (result) mysql_free_result(result);
    }
    lock_acquired_ = false;
    lock_name_.clear();
    lock_slot_ = kNoSlot;
#endif
  }

  bool leaderboard(unsigned int limit, std::vector<LeaderboardEntry>& entries, std::string& error) {
    entries.clear();
    if (limit < 1 || limit > 20) {
      error = "leaderboard limit must be between 1 and 20";
      return false;
    }
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error, true)) return false;
    if (!query("SELECT player_id,rating,wins,losses FROM players "
               "ORDER BY rating DESC,player_id ASC LIMIT " + std::to_string(limit), error))
      return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    const auto parse = [](const char* text, auto& value) {
      if (!text) return false;
      const char* end = text + std::strlen(text);
      const auto converted = std::from_chars(text, end, value);
      return converted.ec == std::errc() && converted.ptr == end;
    };
    while (MYSQL_ROW row = mysql_fetch_row(result)) {
      LeaderboardEntry entry;
      if (!row[0] || !parse(row[1], entry.rating) || !parse(row[2], entry.wins) || !parse(row[3], entry.losses)) {
        mysql_free_result(result);
        entries.clear();
        error = "mysql leaderboard row is invalid";
        return false;
      }
      entry.player_id = row[0];
      entries.push_back(std::move(entry));
    }
    mysql_free_result(result);
    return true;
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool begin_match(const std::string& match_id, const std::string& player_a,
                   const std::string& player_b, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    if (player_a.empty() || player_b.empty() || player_a.size() > 64 || player_b.size() > 64 || player_a == player_b) {
      error = "player ids must be distinct and contain 1 to 64 bytes";
      return false;
    }
    if (!query("START TRANSACTION", error)) return false;
    if (!ensure_player(player_a, error) || !ensure_player(player_b, error)) return rollback(error);
    const std::string sql = "INSERT INTO matches(match_id,player_a,player_b,status,started_at) VALUES(" +
        quote(match_id) + "," + quote(player_a) + "," + quote(player_b) + ",'running',NOW())";
    if (!query(sql, error)) return rollback(error);
    return commit(error);
#else
    (void)match_id; (void)player_a; (void)player_b;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool ensure_outbox_schema(std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    return query(
        "CREATE TABLE IF NOT EXISTS settlement_outbox ("
        "match_id VARCHAR(64) PRIMARY KEY,"
        "winner_id VARCHAR(64) NULL,"
        "loser_id VARCHAR(64) NULL,"
        "winner_rating_delta INT NOT NULL DEFAULT 0,"
        "loser_rating_delta INT NOT NULL DEFAULT 0,"
        "status VARCHAR(16) NOT NULL DEFAULT 'pending',"
        "attempts INT NOT NULL DEFAULT 0,"
        "last_error VARCHAR(255) NOT NULL DEFAULT '',"
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        "applied_at TIMESTAMP NULL,"
        "KEY idx_settlement_outbox_status (status,created_at),"
        "CONSTRAINT fk_outbox_match FOREIGN KEY (match_id) REFERENCES matches(match_id)"
        ")",
        error);
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  // Non-recovery shutdown cancels only this instance's unfinished room.
  // Finished/uncertain settlements are left intact; no result or rating change.
  bool abort_match(const std::string& match_id, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    return query("UPDATE matches SET status='aborted',result_reason='server_shutdown',ended_at=NOW() "
                 "WHERE match_id=" + quote(match_id) + " AND status='running'", error);
#else
    (void)match_id;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool initialize_room_recovery(std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    return initialize_room_recovery_locked(error);
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  void set_room_recovery_owner(const std::string& owner_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    room_recovery_owner_ = owner_id;
  }

  // Configure before publishing the store to Room actors. One preserves autocommit.
  void configure_checkpoint_batching(unsigned int count) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    checkpoint_batcher_.reset();
    if (count > 1)
      checkpoint_batcher_ = std::make_unique<CheckpointBatcher>(std::min(count, 32u),
          [this](const auto& requests) { return write_checkpoint_batch(requests); });
#else
    (void)count;
#endif
  }

  bool claim_room_checkpoints(std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!validate_room_recovery_owner_locked(error) || !ensure_connection_locked(error)) return false;
    if (!lock_acquired_ || lock_name_.empty()) {
      error = "room recovery claim requires the MySQL instance lock";
      return false;
    }
    return query("UPDATE room_checkpoints c JOIN matches m ON m.match_id=c.match_id SET c.owner_id=" +
                     quote(room_recovery_owner_) + " WHERE m.status IN ('running','finished','settled')",
                 error);
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool begin_recoverable_match(const std::string& match_id, const std::string& player_a,
                               const std::string& player_b, const std::string& checkpoint,
                               std::string& error, CheckpointTimings* timings = nullptr,
                               bool tail_mode = false) {
    CheckpointTimer timer(timings);
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    timer.next(CheckpointTimer::Phase::Connection);
    if (match_id.empty() || match_id.size() > 64 || checkpoint.empty() ||
        checkpoint.size() > 16777215u) {
      error = "recoverable match requires a match id of 1 to 64 bytes and a nonempty MEDIUMBLOB checkpoint";
      return false;
    }
    if (player_a.empty() || player_b.empty() || player_a.size() > 64 ||
        player_b.size() > 64 || player_a == player_b) {
      error = "player ids must be distinct and contain 1 to 64 bytes";
      return false;
    }
    if (!validate_room_recovery_owner_locked(error) || !ensure_connection_locked(error)) return false;
    timer.next(CheckpointTimer::Phase::Sql);
    if (!query("START TRANSACTION", error)) return false;
    if (!ensure_player(player_a, error) || !ensure_player(player_b, error)) return rollback(error);
    if (!query("INSERT INTO matches(match_id,player_a,player_b,status,started_at) VALUES(" +
                   quote(match_id) + "," + quote(player_a) + "," + quote(player_b) +
                   ",'running',NOW()) ON DUPLICATE KEY UPDATE match_id=VALUES(match_id)",
               error))
      return rollback(error);
    if (!query("SELECT player_a,player_b,status FROM matches WHERE match_id=" + quote(match_id) + " FOR UPDATE",
               error))
      return rollback(error);
    MYSQL_RES* match_result = mysql_store_result(connection_);
    if (!match_result) { result_error(error); return rollback(error); }
    MYSQL_ROW match = mysql_fetch_row(match_result);
    const bool same_match = match && match[0] && match[1] && match[2] &&
        std::string(match[0]) == player_a && std::string(match[1]) == player_b && std::string(match[2]) == "running";
    mysql_free_result(match_result);
    if (!same_match) {
      error = "recoverable match metadata differs from the initial candidate";
      return rollback(error);
    }
    if (!query("SELECT owner_id,checkpoint,recovery_format,snapshot_sequence,current_sequence "
               "FROM room_checkpoints WHERE match_id=" + quote(match_id) + " FOR UPDATE", error))
      return rollback(error);
    MYSQL_RES* owner_result = mysql_store_result(connection_);
    if (!owner_result) { result_error(error); return rollback(error); }
    MYSQL_ROW owner = mysql_fetch_row(owner_result);
    const unsigned long* owner_lengths = owner ? mysql_fetch_lengths(owner_result) : nullptr;
    const bool exists = owner != nullptr;
    const bool other_owner = owner && (!owner[0] || std::string(owner[0]) != room_recovery_owner_);
    const std::string format = tail_mode ? "tail" : "full";
    const bool identical_initial = owner && owner_lengths && owner[1] && owner[2] && owner[3] && owner[4] &&
        std::string(owner[1], owner_lengths[1]) == checkpoint && std::string(owner[2]) == format &&
        std::string(owner[3]) == "0" && std::string(owner[4]) == "0";
    mysql_free_result(owner_result);
    if (other_owner) {
      error = "room_checkpoint_owner_changed";
      return rollback(error);
    }
    if (exists && !identical_initial) {
      error = "recoverable initial checkpoint differs from the committed candidate";
      return rollback(error);
    }
    if (!exists && !query("INSERT INTO room_checkpoints(match_id,checkpoint,owner_id,recovery_format) VALUES(" + quote(match_id) +
                   ",_binary" + quote(checkpoint) + "," + quote(room_recovery_owner_) + "," + quote(format) + ")",
               error))
      return rollback(error);
    timer.next(CheckpointTimer::Phase::Commit);
    return commit(error);
#else
    (void)match_id;
    (void)player_a;
    (void)player_b;
    (void)checkpoint;
    (void)tail_mode;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool validate_room_recovery_coverage(std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    if (!query("SELECT COUNT(*) FROM matches m LEFT JOIN room_checkpoints c ON c.match_id=m.match_id "
               "WHERE m.status='running' AND c.match_id IS NULL",
               error))
      return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    MYSQL_ROW row = mysql_fetch_row(result);
    if (!row || !row[0]) {
      mysql_free_result(result);
      error = "mysql room recovery coverage result is empty";
      return false;
    }
    const unsigned long long missing = std::strtoull(row[0], nullptr, 10);
    mysql_free_result(result);
    if (missing == 0) return true;
    error = std::to_string(missing) +
        " running matches lack room checkpoints; run legacy cleanup with ARENA_ROOM_RECOVERY=0 "
        "and ARENA_MYSQL_CLEANUP_RUNNING=1 before enabling recovery";
    return false;
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool save_room_checkpoint(const std::string& match_id, const std::string& checkpoint,
                            std::string& error, CheckpointTimings* timings = nullptr) {
    if (match_id.empty() || match_id.size() > 64 || checkpoint.empty() || checkpoint.size() > 16777215u) {
      if (timings) *timings = {};
      error = "room checkpoint requires a match id of 1 to 64 bytes and a nonempty MEDIUMBLOB payload";
      return false;
    }
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    if (checkpoint_batcher_) {
      const auto result = checkpoint_batcher_->submit(match_id, checkpoint);
      if (timings) {
        *timings = {};
        timings->queue_wait_us = result.queue_wait_us;
        timings->batch_size = result.batch_count;
        timings->batch_leader = result.batch_leader;
        // Count shared physical SQL/commit time once, not once per waiting room.
        if (result.batch_leader) {
          timings->lock_wait_us = result.lock_wait_us;
          timings->connection_us = result.connection_us;
          timings->sql_us = result.sql_us;
          timings->commit_us = result.commit_us;
        }
      }
      error = result.error;
      return result.ok;
    }
#endif
    CheckpointTimer timer(timings);
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    timer.next(CheckpointTimer::Phase::Connection);
    if (match_id.empty() || match_id.size() > 64 || checkpoint.empty() ||
        checkpoint.size() > 16777215u) {
      error = "room checkpoint requires a match id of 1 to 64 bytes and a nonempty MEDIUMBLOB payload";
      return false;
    }
    if (!validate_room_recovery_owner_locked(error) || !ensure_connection_locked(error)) return false;
    timer.next(CheckpointTimer::Phase::Sql);
    return update_checkpoint_locked(match_id, checkpoint, error);
#else
    (void)match_id;
    (void)checkpoint;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool save_room_recovery(const std::string& match_id, const RoomRecoveryWrite& write,
                          std::string& error, CheckpointTimings* timings = nullptr) {
    if (match_id.empty() || match_id.size() > 64 || write.payload.empty() ||
        write.payload.size() > 16777215u ||
        write.expected_sequence == std::numeric_limits<std::uint64_t>::max() ||
        write.sequence != write.expected_sequence + 1 ||
        (write.kind != RoomRecoveryWriteKind::FullSnapshot &&
         write.kind != RoomRecoveryWriteKind::Snapshot && write.kind != RoomRecoveryWriteKind::Tail)) {
      if (timings) *timings = {};
      error = "room recovery write requires a bounded payload and the next consecutive sequence";
      return false;
    }
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    if (checkpoint_batcher_) {
      const auto result = checkpoint_batcher_->submit(match_id, write);
      if (timings) {
        *timings = {};
        timings->queue_wait_us = result.queue_wait_us;
        timings->batch_size = result.batch_count;
        timings->batch_leader = result.batch_leader;
        if (result.batch_leader) {
          timings->lock_wait_us = result.lock_wait_us;
          timings->connection_us = result.connection_us;
          timings->sql_us = result.sql_us;
          timings->commit_us = result.commit_us;
        }
      }
      error = result.error;
      return result.ok;
    }
#endif
    CheckpointTimer timer(timings);
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    timer.next(CheckpointTimer::Phase::Connection);
    if (!validate_room_recovery_owner_locked(error) || !ensure_connection_locked(error)) return false;
    timer.next(CheckpointTimer::Phase::Sql);
    if (!query("START TRANSACTION", error)) return false;
    try {
      if (!write_recovery_locked(match_id, write.kind, write.expected_sequence, write.sequence,
                                 write.payload, error)) return rollback(error);
    } catch (const std::exception& exception) {
      error = exception.what();
      return rollback(error);
    } catch (...) {
      error = "room recovery write failed with an unknown exception";
      return rollback(error);
    }
    timer.next(CheckpointTimer::Phase::Commit);
    return commit(error);
#else
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool load_room_checkpoints(std::vector<RoomRecoveryRow>& rows, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    rows.clear();
    if (!ensure_connection_locked(error)) return false;
    if (!query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", error) ||
        !query("START TRANSACTION WITH CONSISTENT SNAPSHOT", error)) return false;
    if (!query("SELECT c.match_id,c.checkpoint,m.status,m.player_a,m.player_b,"
               "c.recovery_format,c.snapshot_sequence,c.current_sequence FROM room_checkpoints c "
               "JOIN matches m ON m.match_id=c.match_id "
               "WHERE m.status IN ('running','finished','settled') ORDER BY BINARY c.match_id",
               error))
      return rollback(error);
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) { result_error(error); return rollback(error); }
    while (MYSQL_ROW row = mysql_fetch_row(result)) {
      const unsigned long* lengths = mysql_fetch_lengths(result);
      std::uint64_t snapshot_sequence = 0;
      std::uint64_t current_sequence = 0;
      if (!lengths || !row[0] || !row[1] || !row[2] || !row[3] || !row[4] || !row[5] ||
          !row[6] || !row[7] || !parse_recovery_sequence(row[6], lengths[6], snapshot_sequence) ||
          !parse_recovery_sequence(row[7], lengths[7], current_sequence)) {
        mysql_free_result(result);
        rows.clear();
        error = "mysql room checkpoint row is incomplete";
        return rollback(error);
      }
      rows.push_back({std::string(row[0], lengths[0]), std::string(row[1], lengths[1]),
                      std::string(row[2], lengths[2]), std::string(row[3], lengths[3]),
                      std::string(row[4], lengths[4]), std::string(row[5], lengths[5]),
                      snapshot_sequence, current_sequence, {}});
    }
    mysql_free_result(result);
    if (!query("SELECT t.match_id,t.sequence,t.payload FROM room_recovery_tail t "
               "JOIN room_checkpoints c ON c.match_id=t.match_id "
               "JOIN matches m ON m.match_id=c.match_id "
               "WHERE m.status IN ('running','finished','settled') ORDER BY BINARY t.match_id,t.sequence", error)) {
      rows.clear();
      return rollback(error);
    }
    result = mysql_store_result(connection_);
    if (!result) { rows.clear(); result_error(error); return rollback(error); }
    std::size_t index = 0;
    while (MYSQL_ROW row = mysql_fetch_row(result)) {
      const unsigned long* lengths = mysql_fetch_lengths(result);
      std::uint64_t sequence = 0;
      if (!lengths || !row[0] || !row[1] || !row[2] ||
          !parse_recovery_sequence(row[1], lengths[1], sequence)) {
        mysql_free_result(result);
        rows.clear();
        error = "mysql room recovery tail row is incomplete";
        return rollback(error);
      }
      const std::string match_id(row[0], lengths[0]);
      while (index < rows.size() && rows[index].match_id < match_id) ++index;
      if (index == rows.size() || rows[index].match_id != match_id) {
        mysql_free_result(result);
        rows.clear();
        error = "mysql room recovery tail has no loaded snapshot";
        return rollback(error);
      }
      rows[index].tails.emplace_back(sequence, std::string(row[2], lengths[2]));
    }
    mysql_free_result(result);
    if (commit(error)) return true;
    rows.clear();
    return false;
#else
    rows.clear();
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool delete_room_checkpoint(const std::string& match_id, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!validate_room_recovery_owner_locked(error) || !ensure_connection_locked(error)) return false;
    if (!query("DELETE FROM room_checkpoints WHERE match_id=" + quote(match_id) +
                   " AND owner_id=" + quote(room_recovery_owner_), error))
      return false;
    if (mysql_affected_rows(connection_) != 0) return true;
    if (!query("SELECT owner_id FROM room_checkpoints WHERE match_id=" + quote(match_id), error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    const bool remains = mysql_num_rows(result) != 0;
    mysql_free_result(result);
    if (remains) error = "room_checkpoint_owner_changed";
    return !remains;
#else
    (void)match_id;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool has_room_checkpoints(bool& has_checkpoints, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    has_checkpoints = false;
    if (!ensure_connection_locked(error)) return false;
    return has_room_checkpoints_locked(has_checkpoints, error);
#else
    has_checkpoints = false;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  // Legacy restart cleanup is only safe when no durable room can be resumed.
  // It never creates match_results or changes player ratings.
  bool cleanup_running_matches(unsigned long long& cleaned_count, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    cleaned_count = 0;
    if (!ensure_connection_locked(error)) return false;
    if (!initialize_room_recovery_locked(error)) return false;
    bool has_checkpoints = false;
    if (!has_room_checkpoints_locked(has_checkpoints, error)) return false;
    if (has_checkpoints) {
      error = "durable room checkpoints require ARENA_ROOM_RECOVERY=1 and ARENA_MYSQL_CLEANUP_RUNNING=0";
      return false;
    }
    if (!query("START TRANSACTION", error)) return false;
    if (!query("UPDATE matches SET status='aborted',result_reason='server_restart',ended_at=NOW() WHERE status='running'", error))
      return rollback(error);
    cleaned_count = static_cast<unsigned long long>(mysql_affected_rows(connection_));
    return commit(error);
#else
    cleaned_count = 0;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool settle(const std::string& match_id, const std::string& player_a,
              const std::string& player_b, int winner, unsigned int turn_count,
              const std::string& reason, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error)) return false;
    if (player_a.empty() || player_b.empty() || player_a.size() > 64 || player_b.size() > 64 || player_a == player_b) {
      error = "player ids must be distinct and contain 1 to 64 bytes";
      return false;
    }
    if (winner < -1 || winner > 1) {
      error = "winner must be player 0, player 1, or draw";
      return false;
    }

    if (!query("START TRANSACTION", error)) return false;
    if (!room_recovery_owner_.empty()) {
      if (!query("SELECT owner_id FROM room_checkpoints WHERE match_id=" + quote(match_id) + " FOR UPDATE", error))
        return rollback(error);
      MYSQL_RES* ownership = mysql_store_result(connection_);
      if (!ownership) { result_error(error); return rollback(error); }
      MYSQL_ROW owner = mysql_fetch_row(ownership);
      const bool owns_checkpoint = owner && owner[0] && std::string(owner[0]) == room_recovery_owner_;
      mysql_free_result(ownership);
      if (!owns_checkpoint) {
        error = "room_checkpoint_owner_changed";
        return rollback(error);
      }
    }
    if (!ensure_player(player_a, error) || !ensure_player(player_b, error)) return rollback(error);

    const std::string match_q = quote(match_id);
    if (!query("SELECT 1 FROM match_results WHERE match_id=" + match_q + " LIMIT 1", error))
      return rollback(error);
    MYSQL_RES* existing = mysql_store_result(connection_);
    if (!existing) {
      result_error(error);
      return rollback(error);
    }
    const bool already_settled = mysql_num_rows(existing) > 0;
    mysql_free_result(existing);
    if (already_settled) {
      if (!query(
              "INSERT INTO settlement_outbox(match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta,status) "
              "SELECT match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta,'pending' "
              "FROM match_results WHERE match_id=" + match_q +
              " ON DUPLICATE KEY UPDATE match_id=VALUES(match_id)",
              error))
        return rollback(error);
      return commit(error);
    }

    if (!query("INSERT INTO matches(match_id,player_a,player_b,winner_id,status,result_reason,turn_count,started_at,ended_at) VALUES(" +
                   match_q + "," + quote(player_a) + "," + quote(player_b) + ",NULL,'settled'," + quote(reason) + "," +
                   std::to_string(turn_count) + ",NOW(),NOW()) ON DUPLICATE KEY UPDATE match_id=VALUES(match_id)", error))
      return rollback(error);

    std::string winner_id = "NULL";
    std::string loser_id = "NULL";
    int winner_delta = 0;
    int loser_delta = 0;
    if (winner == 0 || winner == 1) {
      const std::string& winner_name = winner == 0 ? player_a : player_b;
      const std::string& loser_name = winner == 0 ? player_b : player_a;
      winner_id = quote(winner_name);
      loser_id = quote(loser_name);
      int loser_rating = 0;
      if (!read_rating(loser_name, loser_rating, error)) return rollback(error);
      loser_delta = -std::min(10, loser_rating);
      if (!query("UPDATE players SET rating=rating+10,wins=wins+1 WHERE player_id=" + winner_id, error) ||
          !query("UPDATE players SET rating=GREATEST(0,rating-10),losses=losses+1 WHERE player_id=" + loser_id, error))
        return rollback(error);
      winner_delta = 10;
    }

    const std::string result_sql = "INSERT INTO match_results(match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta) VALUES(" +
        match_q + "," + winner_id + "," + loser_id + "," + std::to_string(winner_delta) + "," +
        std::to_string(loser_delta) + ")";
    if (!query(result_sql, error)) return rollback(error);
    if (!query("UPDATE matches SET winner_id=" + winner_id + ",status='finished',result_reason=" + quote(reason) + ",turn_count=" +
                   std::to_string(turn_count) + ",ended_at=NOW() WHERE match_id=" + match_q, error))
      return rollback(error);
    if (!query(
            "INSERT INTO settlement_outbox(match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta,status) VALUES(" +
                match_q + "," + winner_id + "," + loser_id + "," + std::to_string(winner_delta) + "," +
                std::to_string(loser_delta) + ",'pending') ON DUPLICATE KEY UPDATE match_id=VALUES(match_id)",
            error))
      return rollback(error);
    return commit(error);
#else
    (void)match_id; (void)player_a; (void)player_b; (void)winner; (void)turn_count; (void)reason;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool get_outbox(const std::string& match_id, PendingSettlement& entry, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error, true)) return false;
    if (!query("SELECT match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta FROM settlement_outbox WHERE match_id=" +
                   quote(match_id),
               error))
      return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    MYSQL_ROW row = mysql_fetch_row(result);
    if (!row) {
      mysql_free_result(result);
      error = "settlement outbox row not found";
      return false;
    }
    entry.match_id = row[0] ? row[0] : "";
    entry.winner_id = row[1] ? row[1] : "";
    entry.loser_id = row[2] ? row[2] : "";
    entry.winner_rating_delta = row[3] ? std::atoi(row[3]) : 0;
    entry.loser_rating_delta = row[4] ? std::atoi(row[4]) : 0;
    mysql_free_result(result);
    return true;
#else
    (void)match_id;
    (void)entry;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool list_pending_outbox(std::vector<PendingSettlement>& entries, unsigned int limit, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    entries.clear();
    if (!ensure_connection_locked(error, true)) return false;
    limit = std::max(1u, std::min(limit, 1000u));
    // Cache-free draws must remain consumable behind an offline rated backlog.
    if (!query("SELECT match_id,winner_id,loser_id,winner_rating_delta,loser_rating_delta FROM settlement_outbox WHERE status='pending' "
               "ORDER BY (winner_id IS NULL AND loser_id IS NULL AND winner_rating_delta=0 AND loser_rating_delta=0) DESC,created_at,match_id LIMIT " +
                   std::to_string(limit),
               error))
      return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    while (MYSQL_ROW row = mysql_fetch_row(result)) {
      PendingSettlement entry;
      entry.match_id = row[0] ? row[0] : "";
      entry.winner_id = row[1] ? row[1] : "";
      entry.loser_id = row[2] ? row[2] : "";
      entry.winner_rating_delta = row[3] ? std::atoi(row[3]) : 0;
      entry.loser_rating_delta = row[4] ? std::atoi(row[4]) : 0;
      entries.push_back(std::move(entry));
    }
    mysql_free_result(result);
    return true;
#else
    (void)entries;
    (void)limit;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool mark_outbox_applied(const std::string& match_id, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error, true)) return false;
    if (!query("UPDATE settlement_outbox SET status='applied',applied_at=COALESCE(applied_at,NOW()),last_error='' WHERE match_id=" +
                   quote(match_id) + " AND status='pending'",
               error))
      return false;
    if (mysql_affected_rows(connection_) == 1) return true;
    if (!query("SELECT status FROM settlement_outbox WHERE match_id=" + quote(match_id), error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    MYSQL_ROW row = mysql_fetch_row(result);
    const bool row_exists = row != nullptr;
    const bool already_applied = row_exists && row[0] && std::string(row[0]) == "applied";
    mysql_free_result(result);
    if (already_applied) return true;
    error = row_exists ? "settlement outbox row is not pending" : "settlement outbox row not found";
    return false;
#else
    (void)match_id;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool record_outbox_failure(const std::string& match_id, const std::string& failure, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    if (!ensure_connection_locked(error, true)) return false;
    std::string message = failure.substr(0, 255);
    return query("UPDATE settlement_outbox SET attempts=attempts+1,last_error=" + quote(message) +
                     " WHERE match_id=" + quote(match_id) + " AND status='pending'",
                 error);
#else
    (void)match_id;
    (void)failure;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

  bool pending_outbox_count(unsigned long long& count, std::string& error) {
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
    std::lock_guard<std::mutex> lock(mutex_);
    count = 0;
    if (!ensure_connection_locked(error, true)) return false;
    if (!query("SELECT COUNT(*) FROM settlement_outbox WHERE status='pending'", error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    MYSQL_ROW row = mysql_fetch_row(result);
    if (!row || !row[0]) {
      mysql_free_result(result);
      error = "mysql outbox count result is empty";
      return false;
    }
    count = std::strtoull(row[0], nullptr, 10);
    mysql_free_result(result);
    return true;
#else
    count = 0;
    error = "Arena was built without MySQL client development files";
    return false;
#endif
  }

 private:
#if defined(ARENA_WITH_MYSQL) && ARENA_WITH_MYSQL
  CheckpointBatchResult write_checkpoint_batch(const std::vector<std::shared_ptr<CheckpointBatcher::Request>>& requests) {
    CheckpointBatchResult result;
    CheckpointTimings timings;
    {
      CheckpointTimer timer(&timings);
      std::lock_guard<std::mutex> lock(mutex_);
      timer.next(CheckpointTimer::Phase::Connection);
      if (validate_room_recovery_owner_locked(result.error) && ensure_connection_locked(result.error)) {
        timer.next(CheckpointTimer::Phase::Sql);
        if (query("START TRANSACTION", result.error)) {
          try {
            bool updated = true;
            for (const auto& request : requests)
              if (!(request->sequenced
                    ? write_recovery_locked(request->match_id, request->write_kind, request->expected_sequence,
                                            request->sequence, request->checkpoint, result.error)
                    : update_checkpoint_locked(request->match_id, request->checkpoint, result.error))) {
                updated = false;
                break;
              }
            if (updated) {
              room::test_fault_barrier("checkpoint_batch_before_commit", requests.front()->match_id);
              timer.next(CheckpointTimer::Phase::Commit);
              result.ok = commit(result.error);
            } else rollback(result.error);
          } catch (const std::exception& exception) {
            result.error = exception.what();
            rollback(result.error);
          } catch (...) {
            result.error = "checkpoint batch failed with an unknown exception";
            rollback(result.error);
          }
        }
      }
    }
    result.lock_wait_us = timings.lock_wait_us;
    result.connection_us = timings.connection_us;
    result.sql_us = timings.sql_us;
    result.commit_us = timings.commit_us;
    return result;
  }

  bool update_checkpoint_locked(const std::string& match_id, const std::string& checkpoint, std::string& error) {
    if (!query("UPDATE room_checkpoints c JOIN matches m ON m.match_id=c.match_id SET c.checkpoint=_binary" +
                   quote(checkpoint) + ",c.updated_at=CURRENT_TIMESTAMP WHERE c.match_id=" + quote(match_id) +
                   " AND c.owner_id=" + quote(room_recovery_owner_) +
                   " AND c.recovery_format='full'" +
                   " AND m.status IN ('running','finished','settled')", error)) return false;
    if (mysql_affected_rows(connection_) != 0) return true;
    // Identical retries have zero changed rows; ownership still needs validation.
    if (!query("SELECT c.owner_id,m.status,c.recovery_format FROM room_checkpoints c JOIN matches m ON m.match_id=c.match_id "
               "WHERE c.match_id=" + quote(match_id), error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    MYSQL_ROW row = mysql_fetch_row(result);
    const bool exists = row != nullptr;
    const bool owns_checkpoint = exists && row[0] && std::string(row[0]) == room_recovery_owner_;
    const std::string status = exists && row[1] ? row[1] : "";
    const bool full_format = exists && row[2] && std::string(row[2]) == "full";
    mysql_free_result(result);
    if (!exists) error = "recoverable match row not found for checkpoint";
    else if (!owns_checkpoint) error = "room_checkpoint_owner_changed";
    else if (status != "running" && status != "finished" && status != "settled")
      error = "room checkpoint match is not recoverable";
    else if (!full_format) error = "legacy checkpoint write cannot overwrite room recovery tail";
    else return true;
    return false;
  }

  static bool parse_recovery_sequence(const char* bytes, std::size_t length, std::uint64_t& value) {
    if (!bytes || length == 0) return false;
    const auto parsed = std::from_chars(bytes, bytes + length, value);
    return parsed.ec == std::errc{} && parsed.ptr == bytes + length;
  }

  bool clear_covered_tail_locked(const std::string& match_id, std::uint64_t sequence, std::string& error) {
    room::test_fault_barrier("snapshot_before_tail_cleanup", match_id);
    if (!query("DELETE FROM room_recovery_tail WHERE match_id=" + quote(match_id) +
                   " AND sequence<=" + std::to_string(sequence), error)) return false;
    room::test_fault_barrier("snapshot_after_tail_cleanup", match_id);
    return true;
  }

  bool write_recovery_locked(const std::string& match_id, RoomRecoveryWriteKind kind,
                             std::uint64_t expected_sequence, std::uint64_t sequence,
                             const std::string& payload, std::string& error) {
    const bool tail = kind == RoomRecoveryWriteKind::Tail;
    const std::string format = kind == RoomRecoveryWriteKind::FullSnapshot ? "full" : "tail";
    const std::string owner_condition = " WHERE c.match_id=" + quote(match_id) +
        " AND c.owner_id=" + quote(room_recovery_owner_) +
        " AND c.current_sequence=" + std::to_string(expected_sequence) +
        " AND m.status IN ('running','finished','settled')";
    const std::string prefix = "UPDATE room_checkpoints c JOIN matches m ON m.match_id=c.match_id SET ";
    const std::string assignments = tail
        ? "c.current_sequence=" + std::to_string(sequence) + ",c.updated_at=CURRENT_TIMESTAMP"
        : "c.checkpoint=_binary" + quote(payload) + ",c.snapshot_sequence=" + std::to_string(sequence) +
          ",c.current_sequence=" + std::to_string(sequence) + ",c.recovery_format=" + quote(format) +
          ",c.updated_at=CURRENT_TIMESTAMP";
    // Keep the ordinary full path to one UPDATE. A mode switch takes the
    // locked fallback below, where deleting the previous tail is necessary.
    const std::string mode_condition = (tail || kind == RoomRecoveryWriteKind::FullSnapshot)
        ? " AND c.recovery_format=" + quote(format) : " AND c.recovery_format IN ('full','tail')";
    if (!query(prefix + assignments + owner_condition + mode_condition, error)) return false;
    if (mysql_affected_rows(connection_) != 0) {
      if (tail)
        return query("INSERT INTO room_recovery_tail(match_id,sequence,payload) VALUES(" + quote(match_id) +
                         "," + std::to_string(sequence) + ",_binary" + quote(payload) + ")", error);
      if (kind == RoomRecoveryWriteKind::Snapshot) return clear_covered_tail_locked(match_id, sequence, error);
      return true;
    }

    if (!query("SELECT c.owner_id,m.status,c.recovery_format,c.snapshot_sequence,c.current_sequence,c.checkpoint "
               "FROM room_checkpoints c JOIN matches m ON m.match_id=c.match_id WHERE c.match_id=" +
                   quote(match_id) + " FOR UPDATE", error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    MYSQL_ROW row = mysql_fetch_row(result);
    const unsigned long* lengths = row ? mysql_fetch_lengths(result) : nullptr;
    const bool exists = row != nullptr;
    const bool owns = row && row[0] && std::string(row[0]) == room_recovery_owner_;
    const std::string status = row && row[1] ? row[1] : "";
    const std::string stored_format = row && row[2] ? row[2] : "";
    std::uint64_t snapshot_sequence = 0;
    std::uint64_t current_sequence = 0;
    const bool valid_sequences = row && lengths && row[3] && row[4] &&
        parse_recovery_sequence(row[3], lengths[3], snapshot_sequence) &&
        parse_recovery_sequence(row[4], lengths[4], current_sequence) && snapshot_sequence <= current_sequence;
    const bool identical_snapshot = row && lengths && row[5] && std::string(row[5], lengths[5]) == payload;
    mysql_free_result(result);
    if (!exists) error = "recoverable match row not found for checkpoint";
    else if (!owns) error = "room_checkpoint_owner_changed";
    else if (status != "running" && status != "finished" && status != "settled")
      error = "room checkpoint match is not recoverable";
    else if (!valid_sequences || (stored_format != "full" && stored_format != "tail"))
      error = "room recovery stored sequence or format is invalid";
    else if (current_sequence == sequence) {
      // A lost COMMIT reply may cause an exact retry. Sequence equality alone
      // is insufficient: a different candidate must never be acknowledged.
      if (!tail) {
        if (stored_format == format && snapshot_sequence == sequence && identical_snapshot) return true;
      } else if (stored_format == "tail" && snapshot_sequence < sequence) {
        if (!query("SELECT payload FROM room_recovery_tail WHERE match_id=" + quote(match_id) +
                       " AND sequence=" + std::to_string(sequence), error)) return false;
        result = mysql_store_result(connection_);
        if (!result) return result_error(error);
        row = mysql_fetch_row(result);
        lengths = row ? mysql_fetch_lengths(result) : nullptr;
        const bool identical_tail = row && lengths && row[0] && std::string(row[0], lengths[0]) == payload;
        mysql_free_result(result);
        if (identical_tail) return true;
      }
      error = "room recovery retry differs from the committed candidate";
    } else if (current_sequence == expected_sequence && kind == RoomRecoveryWriteKind::FullSnapshot &&
               stored_format == "tail") {
      // The row remains locked until COMMIT, including its mode conversion and
      // tail removal. Startup can observe either complete representation.
      if (!query(prefix + assignments + owner_condition, error)) return false;
      if (mysql_affected_rows(connection_) != 1) {
        error = "room recovery mode switch lost the expected sequence";
        return false;
      }
      return clear_covered_tail_locked(match_id, sequence, error);
    } else error = "room recovery sequence or mode does not match the committed predecessor";
    return false;
  }

  static constexpr std::size_t kNoSlot = std::numeric_limits<std::size_t>::max();

  struct ConnectionConfig {
    std::string host;
    unsigned int port = 3306;
    std::string user;
    std::string password;
    std::string database;
  };

  bool initialize_room_recovery_locked(std::string& error) {
    if (!query(
        "CREATE TABLE IF NOT EXISTS room_checkpoints ("
        "match_id VARCHAR(64) PRIMARY KEY,"
        "checkpoint MEDIUMBLOB NOT NULL,"
        "owner_id VARCHAR(64) NOT NULL DEFAULT '',"
        "recovery_format VARCHAR(16) NOT NULL DEFAULT 'full',"
        "snapshot_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        "current_sequence BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        "updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,"
        "CONSTRAINT fk_room_checkpoint_match FOREIGN KEY (match_id) REFERENCES matches(match_id)"
        ")",
        error)) return false;
    if (!ensure_room_recovery_column_locked("owner_id", "VARCHAR(64) NOT NULL DEFAULT ''", error) ||
        !ensure_room_recovery_column_locked("recovery_format", "VARCHAR(16) NOT NULL DEFAULT 'full'", error) ||
        !ensure_room_recovery_column_locked("snapshot_sequence", "BIGINT UNSIGNED NOT NULL DEFAULT 0", error) ||
        !ensure_room_recovery_column_locked("current_sequence", "BIGINT UNSIGNED NOT NULL DEFAULT 0", error))
      return false;
    return query("CREATE TABLE IF NOT EXISTS room_recovery_tail ("
                 "match_id VARCHAR(64) NOT NULL,"
                 "sequence BIGINT UNSIGNED NOT NULL,"
                 "payload MEDIUMBLOB NOT NULL,"
                 "PRIMARY KEY(match_id,sequence),"
                 "CONSTRAINT fk_room_recovery_tail_checkpoint FOREIGN KEY (match_id) "
                 "REFERENCES room_checkpoints(match_id) ON DELETE CASCADE)", error);
  }

  bool ensure_room_recovery_column_locked(const std::string& column, const std::string& definition,
                                          std::string& error) {
    if (!query("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
               "AND TABLE_NAME='room_checkpoints' AND COLUMN_NAME=" + quote(column), error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    MYSQL_ROW row = mysql_fetch_row(result);
    const bool present = row && row[0] && std::strtoull(row[0], nullptr, 10) != 0;
    mysql_free_result(result);
    if (present) return true;
    return query("ALTER TABLE room_checkpoints ADD COLUMN " + column + " " + definition, error);
  }

  bool validate_room_recovery_owner_locked(std::string& error) const {
    if (!room_recovery_owner_.empty() && room_recovery_owner_.size() <= 64) return true;
    error = "room recovery owner must contain 1 to 64 bytes";
    return false;
  }

  bool has_room_checkpoints_locked(bool& has_checkpoints, std::string& error) {
    if (!query("SELECT 1 FROM room_checkpoints c JOIN matches m ON m.match_id=c.match_id "
               "WHERE m.status IN ('running','finished','settled') LIMIT 1",
               error))
      return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) return result_error(error);
    has_checkpoints = mysql_num_rows(result) != 0;
    mysql_free_result(result);
    return true;
  }

  static bool is_connection_error(unsigned int code) {
    switch (code) {
#ifdef CR_CONNECTION_ERROR
      case CR_CONNECTION_ERROR:
#endif
#ifdef CR_CONN_HOST_ERROR
      case CR_CONN_HOST_ERROR:
#endif
#ifdef CR_IPSOCK_ERROR
      case CR_IPSOCK_ERROR:
#endif
#ifdef CR_UNKNOWN_HOST
      case CR_UNKNOWN_HOST:
#endif
#ifdef CR_SERVER_GONE_ERROR
      case CR_SERVER_GONE_ERROR:
#endif
#ifdef CR_SERVER_LOST
      case CR_SERVER_LOST:
#endif
#ifdef CR_SERVER_HANDSHAKE_ERR
      case CR_SERVER_HANDSHAKE_ERR:
#endif
#ifdef CR_TCP_CONNECTION
      case CR_TCP_CONNECTION:
#endif
        return true;
      default:
        return false;
    }
  }

  void close_active_connection_locked() {
    if (active_slot_ < connections_.size() && connections_[active_slot_]) {
      mysql_close(connections_[active_slot_]);
      connections_[active_slot_] = nullptr;
    }
    connection_ = nullptr;
    if (lock_slot_ == active_slot_) {
      lock_slot_ = kNoSlot;
      lock_acquired_ = false;
    }
  }

  void close_connection_locked(bool clear_lock_name) {
    for (MYSQL*& handle : connections_) {
      if (handle) mysql_close(handle);
      handle = nullptr;
    }
    if (connection_ && connections_.empty()) mysql_close(connection_);
    connection_ = nullptr;
    active_slot_ = 0;
    lock_slot_ = kNoSlot;
    next_slot_ = 0;
    lock_acquired_ = false;
    if (clear_lock_name) lock_name_.clear();
  }

  void invalidate_connection_locked() {
    close_active_connection_locked();
    connection_losses_.fetch_add(1, std::memory_order_relaxed);
    reconnect_backoff_.fail(RetryBackoff::Clock::now());
  }

  bool open_connection_locked(std::string& error) {
    connection_attempts_.fetch_add(1, std::memory_order_relaxed);
    if (!configured_) {
      error = "mysql connection settings are not configured";
      connection_failures_.fetch_add(1, std::memory_order_relaxed);
      return false;
    }
    MYSQL*& slot = connections_[active_slot_];
    slot = mysql_init(nullptr);
    connection_ = slot;
    if (!slot) {
      error = "mysql_init failed";
      connection_failures_.fetch_add(1, std::memory_order_relaxed);
      reconnect_backoff_.fail(RetryBackoff::Clock::now());
      return false;
    }
    bool reconnect = false;
    mysql_options(connection_, MYSQL_OPT_RECONNECT, &reconnect);
    if (!mysql_real_connect(connection_, connection_config_.host.c_str(), connection_config_.user.c_str(),
                            connection_config_.password.c_str(), connection_config_.database.c_str(),
                            connection_config_.port, nullptr, 0)) {
      error = mysql_error(connection_);
      close_active_connection_locked();
      connection_failures_.fetch_add(1, std::memory_order_relaxed);
      reconnect_backoff_.fail(RetryBackoff::Clock::now());
      return false;
    }
    if (mysql_set_character_set(connection_, "utf8mb4") != 0) {
      error = mysql_error(connection_);
      close_active_connection_locked();
      connection_failures_.fetch_add(1, std::memory_order_relaxed);
      reconnect_backoff_.fail(RetryBackoff::Clock::now());
      return false;
    }
    connection_successes_.fetch_add(1, std::memory_order_relaxed);
    reconnect_backoff_.reset();
    return true;
  }

  bool acquire_instance_lock_locked(std::string& error) {
    if (lock_name_.empty()) return true;
    if (!query("SELECT GET_LOCK(" + quote(lock_name_) + ",0)", error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    MYSQL_ROW row = mysql_fetch_row(result);
    const bool acquired = row && row[0] && std::string(row[0]) == "1";
    mysql_free_result(result);
    if (!acquired) {
      error = "mysql instance lock is already held";
      return false;
    }
    lock_acquired_ = true;
    lock_slot_ = active_slot_;
    return true;
  }

  bool ensure_connection_locked(std::string& error, bool prefer_pool_slot = false) {
    // A lock is scoped to the MySQL session that acquired it.  If that slot
    // has already been invalidated while another pool slot was servicing an
    // Outbox operation, forget the stale ownership before admitting a new
    // transaction; the healthy slot selected below must reacquire GET_LOCK.
    if (lock_acquired_ &&
        (lock_slot_ == kNoSlot || lock_slot_ >= connections_.size() || !connections_[lock_slot_])) {
      lock_acquired_ = false;
      lock_slot_ = kNoSlot;
    }
    if (prefer_pool_slot) select_pool_slot_locked();
    // Transactions and startup ownership checks must stay on the session that
    // owns GET_LOCK.  Only explicitly pool-aware outbox operations may use a
    // different slot; if no such slot is available, fall back to the lock slot.
    if (lock_acquired_ && lock_slot_ != kNoSlot &&
        (!prefer_pool_slot || !connection_) &&
        lock_slot_ < connections_.size() && connections_[lock_slot_]) {
      active_slot_ = lock_slot_;
      connection_ = connections_[active_slot_];
    }
    if (connection_) {
      if (mysql_ping(connection_) == 0) {
        if (!lock_name_.empty() && !lock_acquired_) return acquire_instance_lock_locked(error);
        return true;
      }
      error = mysql_error(connection_);
      invalidate_connection_locked();
    }
    if (!configured_) {
      error = "mysql connection settings are not configured";
      return false;
    }
    if (!connection_ && !connections_.empty()) {
      for (std::size_t index = 0; index < connections_.size(); ++index) {
        if (connections_[index]) {
          active_slot_ = index;
          connection_ = connections_[index];
          if (mysql_ping(connection_) == 0) {
            // A lock is bound to the MySQL session that acquired it.  If that
            // session was lost, reacquire the lock before exposing this slot.
            if (!lock_name_.empty() && !lock_acquired_) return acquire_instance_lock_locked(error);
            return true;
          }
          error = mysql_error(connection_);
          invalidate_connection_locked();
        }
      }
    }
    const auto now = RetryBackoff::Clock::now();
    if (!reconnect_backoff_.ready(now)) {
      error = "mysql reconnect backoff active for " +
              std::to_string(reconnect_backoff_.current_delay_seconds()) + "s";
      return false;
    }
    if (connections_.empty()) {
      connections_.assign(normalize_pool_size(pool_size_), nullptr);
      active_slot_ = 0;
    }
    if (!open_connection_locked(error)) return false;
    if (!lock_name_.empty() && !lock_acquired_) return acquire_instance_lock_locked(error);
    return true;
  }

  // Outbox reads and idempotent updates may use a non-locking session.  The
  // store mutex still serializes its public operations, while rotating the
  // session makes the bounded pool reusable and keeps the instance-lock
  // session reserved for transactions and startup ownership checks.
  void select_pool_slot_locked() {
    if (connections_.empty()) return;
    const std::size_t start = next_slot_ % connections_.size();
    for (std::size_t offset = 0; offset < connections_.size(); ++offset) {
      const std::size_t index = (start + offset) % connections_.size();
      if (lock_acquired_ && index == lock_slot_ && connections_.size() > 1) continue;
      if (connections_[index]) {
        active_slot_ = index;
        connection_ = connections_[index];
        next_slot_ = (index + 1) % connections_.size();
        return;
      }
    }
    connection_ = nullptr;
  }

  bool result_error(std::string& error) {
    if (!connection_) {
      error = "mysql connection is not open";
      return false;
    }
    const unsigned int code = mysql_errno(connection_);
    error = mysql_error(connection_);
    if (is_connection_error(code)) invalidate_connection_locked();
    return false;
  }

  bool query(const std::string& sql, std::string& error) {
    if (!connection_) {
      error = "mysql connection is not open";
      return false;
    }
    if (mysql_query(connection_, sql.c_str()) == 0) return true;
    const unsigned int code = mysql_errno(connection_);
    error = mysql_error(connection_);
    if (is_connection_error(code)) invalidate_connection_locked();
    return false;
  }

  bool rollback(std::string& original_error) {
    const std::string saved = original_error;
    std::string rollback_error;
    if (connection_) query("ROLLBACK", rollback_error);
    original_error = saved;
    return false;
  }

  bool commit(std::string& error) {
    if (query("COMMIT", error)) return true;
    const std::string saved = error;
    std::string rollback_error;
    if (connection_) query("ROLLBACK", rollback_error);
    error = saved;
    return false;
  }

  bool ensure_player(const std::string& name, std::string& error) {
    return query("INSERT INTO players(player_id,nickname) VALUES(" + quote(name) + "," + quote(name) +
                     ") ON DUPLICATE KEY UPDATE nickname=VALUES(nickname)", error);
  }

  bool read_rating(const std::string& name, int& rating, std::string& error) {
    if (!query("SELECT rating FROM players WHERE player_id=" + quote(name), error)) return false;
    MYSQL_RES* result = mysql_store_result(connection_);
    if (!result) {
      return result_error(error);
    }
    MYSQL_ROW row = mysql_fetch_row(result);
    if (!row || !row[0]) {
      mysql_free_result(result);
      error = "player row missing during settlement";
      return false;
    }
    rating = std::atoi(row[0]);
    mysql_free_result(result);
    return true;
  }

  std::string quote(const std::string& value) const {
    std::vector<char> escaped(value.size() * 2 + 1);
    const unsigned long length = mysql_real_escape_string(connection_, escaped.data(), value.c_str(),
                                                            static_cast<unsigned long>(value.size()));
    return "'" + std::string(escaped.data(), length) + "'";
  }

  MYSQL* connection_ = nullptr;
  std::vector<MYSQL*> connections_;
  std::size_t active_slot_ = 0;
  std::size_t lock_slot_ = kNoSlot;
  std::size_t next_slot_ = 0;
  ConnectionConfig connection_config_;
  bool configured_ = false;
  std::string lock_name_;
  bool lock_acquired_ = false;
  RetryBackoff reconnect_backoff_;
  std::atomic<unsigned long long> connection_attempts_{0};
  std::atomic<unsigned long long> connection_successes_{0};
  std::atomic<unsigned long long> connection_failures_{0};
  std::atomic<unsigned long long> connection_losses_{0};
  std::unique_ptr<CheckpointBatcher> checkpoint_batcher_;
#endif
  unsigned int pool_size_ = 1;
  mutable std::mutex mutex_;
  std::string room_recovery_owner_;
};

inline std::string environment(const char* key, const std::string& fallback = "") {
#ifdef _WIN32
  char* raw = nullptr;
  size_t size = 0;
  if (_dupenv_s(&raw, &size, key) != 0 || raw == nullptr) return fallback;
  std::string value(raw);
  std::free(raw);
  return value.empty() ? fallback : value;
#else
  const char* value = std::getenv(key);
  return value && *value ? std::string(value) : fallback;
#endif
}

inline bool truthy(const std::string& value) {
  return value == "1" || value == "true" || value == "TRUE" || value == "yes";
}

}  // namespace arena::persistence
