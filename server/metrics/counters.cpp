#include "server/metrics/counters.h"

#include <sstream>

#include "server/gateway/session.h"

namespace arena::metrics {

std::string rooms_payload(const Counters& counters, const gateway::Metrics& network) {
    std::ostringstream payload;
    payload<<"active_rooms="<<counters.active_rooms.load()<<";active_sessions="<<network.active_sessions.load()
      <<";connections_rejected="<<network.connections_rejected.load()
      <<";requests_rate_limited="<<network.requests_rate_limited.load()
      <<";heartbeat_timeouts="<<network.heartbeat_timeouts.load()
      <<";room_commands_enqueued="<<counters.room_commands_enqueued.load()
      <<";room_commands_processed="<<counters.room_commands_processed.load()
      <<";room_command_queue_high_watermark="<<counters.room_command_queue_high_watermark.load()
      <<";send_frames_enqueued="<<network.send_frames_enqueued.load()
      <<";send_frames_dropped="<<network.send_frames_dropped.load()
      <<";send_queue_high_watermark_bytes="<<network.send_queue_high_watermark_bytes.load()
      <<";settlements_started="<<counters.settlements_started.load()
      <<";settlements_succeeded="<<counters.settlements_succeeded.load()
      <<";settlements_failed="<<counters.settlements_failed.load()
      <<";settlement_retries="<<counters.settlement_retries.load()
      <<";settlement_latency_ms_total="<<counters.settlement_latency_ms_total.load()
      <<";settlement_latency_ms_max="<<counters.settlement_latency_ms_max.load()
      <<";settlement_outbox_applied="<<counters.settlement_outbox_applied.load()
      <<";settlement_outbox_failures="<<counters.settlement_outbox_failures.load()
      <<";settlement_outbox_pending="<<counters.settlement_outbox_pending.load()
      <<";settlement_outbox_load_failures="<<counters.settlement_outbox_load_failures.load()
      <<";settlement_outbox_mark_failures="<<counters.settlement_outbox_mark_failures.load()
      <<";settlement_outbox_record_failures="<<counters.settlement_outbox_record_failures.load()
      <<";mysql_connection_attempts="<<counters.mysql_connection_attempts.load()
      <<";mysql_connection_successes="<<counters.mysql_connection_successes.load()
      <<";mysql_connection_failures="<<counters.mysql_connection_failures.load()
      <<";mysql_connection_losses="<<counters.mysql_connection_losses.load()
      <<";redis_connection_failures="<<counters.redis_connection_failures.load()
      <<";redis_apply_failures="<<counters.redis_apply_failures.load()
      <<";replays_saved="<<counters.replays_saved.load()
      <<";replay_save_failures="<<counters.replay_save_failures.load()
      <<";recovery_checkpoint_attempts="<<counters.recovery_checkpoint_attempts.load()
      <<";recovery_checkpoint_successes="<<counters.recovery_checkpoint_successes.load()
      <<";recovery_checkpoint_failures="<<counters.recovery_checkpoint_failures.load()
      <<";recovery_checkpoint_bytes_total="<<counters.recovery_checkpoint_bytes_total.load()
      <<";recovery_checkpoint_bytes_max="<<counters.recovery_checkpoint_bytes_max.load()
      <<";recovery_checkpoint_serialize_us_total="<<counters.recovery_checkpoint_serialize_us_total.load()
      <<";recovery_checkpoint_write_us_total="<<counters.recovery_checkpoint_write_us_total.load()
      <<";recovery_checkpoint_write_us_max="<<counters.recovery_checkpoint_write_us_max.load()
      <<";recovery_checkpoint_lock_wait_us_total="<<counters.recovery_checkpoint_lock_wait_us_total.load()
      <<";recovery_checkpoint_connection_us_total="<<counters.recovery_checkpoint_connection_us_total.load()
      <<";recovery_checkpoint_sql_us_total="<<counters.recovery_checkpoint_sql_us_total.load()
      <<";recovery_checkpoint_commit_us_total="<<counters.recovery_checkpoint_commit_us_total.load()
      <<";recovery_checkpoint_queue_wait_us_total="<<counters.recovery_checkpoint_queue_wait_us_total.load()
      <<";recovery_checkpoint_batches="<<counters.recovery_checkpoint_batches.load()
      <<";recovery_checkpoint_batch_items_max="<<counters.recovery_checkpoint_batch_items_max.load();
  return payload.str();
}

}
