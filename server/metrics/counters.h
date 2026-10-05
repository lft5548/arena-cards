#pragma once

#include <atomic>
#include <string>

namespace arena::gateway { struct Metrics; }

namespace arena::metrics {

struct Counters {
  std::atomic<unsigned int> active_rooms{0};
  std::atomic<unsigned long long> room_commands_enqueued{0};
  std::atomic<unsigned long long> room_commands_processed{0};
  std::atomic<unsigned long long> room_command_queue_high_watermark{0};
  std::atomic<unsigned long long> settlements_started{0};
  std::atomic<unsigned long long> settlements_succeeded{0};
  std::atomic<unsigned long long> settlements_failed{0};
  std::atomic<unsigned long long> settlement_retries{0};
  std::atomic<unsigned long long> settlement_latency_ms_total{0};
  std::atomic<unsigned long long> settlement_latency_ms_max{0};
  std::atomic<unsigned long long> settlement_outbox_applied{0};
  std::atomic<unsigned long long> settlement_outbox_failures{0};
  std::atomic<unsigned long long> settlement_outbox_pending{0};
  std::atomic<unsigned long long> settlement_outbox_load_failures{0};
  std::atomic<unsigned long long> settlement_outbox_mark_failures{0};
  std::atomic<unsigned long long> settlement_outbox_record_failures{0};
  std::atomic<unsigned long long> mysql_connection_attempts{0};
  std::atomic<unsigned long long> mysql_connection_successes{0};
  std::atomic<unsigned long long> mysql_connection_failures{0};
  std::atomic<unsigned long long> mysql_connection_losses{0};
  std::atomic<unsigned long long> redis_connection_failures{0};
  std::atomic<unsigned long long> redis_apply_failures{0};
  std::atomic<unsigned long long> replays_saved{0};
  std::atomic<unsigned long long> replay_save_failures{0};
  std::atomic<unsigned long long> recovery_checkpoint_attempts{0};
  std::atomic<unsigned long long> recovery_checkpoint_successes{0};
  std::atomic<unsigned long long> recovery_checkpoint_failures{0};
  std::atomic<unsigned long long> recovery_checkpoint_bytes_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_bytes_max{0};
  std::atomic<unsigned long long> recovery_checkpoint_serialize_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_write_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_write_us_max{0};
  std::atomic<unsigned long long> recovery_checkpoint_lock_wait_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_connection_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_sql_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_commit_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_queue_wait_us_total{0};
  std::atomic<unsigned long long> recovery_checkpoint_batches{0};
  std::atomic<unsigned long long> recovery_checkpoint_batch_items_max{0};
};

template <typename Integer>
void update_high_watermark(std::atomic<Integer>& target, Integer value) {
  Integer previous = target.load(std::memory_order_relaxed);
  while (previous < value && !target.compare_exchange_weak(
      previous, value, std::memory_order_relaxed, std::memory_order_relaxed)) {}
}

std::string rooms_payload(const Counters& counters, const gateway::Metrics& network);

}
