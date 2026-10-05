#include <atomic>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "server/app/runtime.h"
#include "server/app/session.h"
#include "server/app/identifiers.h"
#include "server/config/card_catalog.h"
#include "server/match/matchmaker.h"
#include "server/room/room.h"

int main(int argc, char** argv) {
  const auto require = [](bool condition, const char* message) {
    if (!condition) { std::cerr << "application modules test failed: " << message << "\n"; std::exit(1); }
  };
  require(argc == 2, "card configuration argument");
  auto catalog = std::make_shared<arena::config::CardCatalog>();
  std::string error;
  require(catalog->load_csv(argv[1], error), "immutable catalog loads");
  auto runtime = std::make_shared<arena::Runtime>();
  auto other_runtime = std::make_shared<arena::Runtime>();
  auto matchmaker = std::make_shared<arena::Matchmaker>(catalog, runtime);
  auto first = std::make_shared<arena::Session>(matchmaker, runtime);
  auto second = std::make_shared<arena::Session>(matchmaker, runtime);
  first->set_identity("first", "first-token");
  second->set_identity("second", "second-token");
  require(first->logged_in() && first->user() == "first" && first->token() == "first-token", "identity synchronization");
  require(first->closed() && !first->room(), "unbound application session has no transport or room");
  auto room = std::make_shared<arena::Room>(first, second, catalog, runtime);
  room->assign_token(0, first->token(), first->user());
  room->assign_token(1, second->token(), second->user());
  const auto entry = runtime->rooms.find("first-token");
  require(entry.player_index == 0 && entry.room.lock() == room, "registry stores correct room/slot");
  require(runtime->rooms.find("second-token").player_index == 1, "second slot registration");
  require(other_runtime->rooms.find("first-token").room.expired(), "registry is not process-global");
  require(runtime->rooms.find("unknown-token").player_index == -1, "unknown token cannot select a player");
  first->set_room(room);
  auto unrelated = std::make_shared<arena::Room>(first, second, catalog, runtime);
  first->clear_room(unrelated.get());
  require(first->room() == room, "old room cleanup cannot erase a different room binding");
  first->clear_room(room.get());
  require(!first->room(), "matching room cleanup releases binding");
  const auto weak_room = std::weak_ptr<arena::Room>(room);
  room.reset();
  require(weak_room.expired() && runtime->rooms.find("first-token").room.expired(), "registry does not retain finished room");
  runtime->rooms.erase("first-token");
  runtime->rooms.erase("first-token");
  require(runtime->rooms.find("first-token").player_index == -1, "registry deletion is idempotent");
  runtime->rooms.attach("second-token", unrelated, 0);
  require(runtime->rooms.find("second-token").room.lock() == unrelated &&
          runtime->rooms.find("second-token").player_index == 0, "replacement preserves current association");
  unrelated.reset();
  require(runtime->rooms.find("second-token").room.expired(), "weak registry expires replacement");

  std::vector<std::thread> writers;
  for (unsigned long long index = 1; index <= 4; ++index) {
    writers.emplace_back([runtime, index]() {
      for (unsigned long long count = 0; count < 100; ++count) {
        runtime->counters.room_commands_enqueued.fetch_add(1, std::memory_order_relaxed);
        arena::metrics::update_high_watermark(runtime->counters.room_command_queue_high_watermark, index);
      }
    });
  }
  for (auto& writer : writers) writer.join();
  require(runtime->counters.room_commands_enqueued.load() == 400 &&
          runtime->counters.room_command_queue_high_watermark.load() == 4, "counters and high-watermark concurrency");
  require(other_runtime->counters.room_commands_enqueued.load() == 0, "metrics isolation");
  runtime->gateway_metrics.active_sessions = 3;
  runtime->gateway_metrics.connections_rejected = 9223372036854775808ull;
  runtime->gateway_metrics.requests_rate_limited = 18446744073709551615ull;
  runtime->gateway_metrics.heartbeat_timeouts = 9223372036854775808ull;
  runtime->gateway_metrics.send_frames_dropped = 7;
  runtime->counters.active_rooms = 2;
  runtime->counters.settlements_succeeded = 11;
  runtime->counters.settlement_outbox_applied = 5;
  runtime->counters.settlement_outbox_failures = 2;
  runtime->counters.settlement_outbox_pending = 1;
  runtime->counters.settlement_outbox_load_failures = 17;
  runtime->counters.settlement_outbox_mark_failures = 18;
  runtime->counters.settlement_outbox_record_failures = 19;
  runtime->counters.mysql_connection_attempts = 18446744073709551615ull;
  runtime->counters.mysql_connection_successes = 20;
  arena::metrics::update_high_watermark(runtime->counters.mysql_connection_successes, 19ull);
  require(runtime->counters.mysql_connection_successes.load() == 20, "older MySQL snapshot cannot decrease counters");
  runtime->counters.mysql_connection_failures = 21;
  runtime->counters.mysql_connection_losses = 22;
  runtime->counters.redis_connection_failures = 23;
  runtime->counters.redis_apply_failures = 9223372036854775808ull;
  runtime->counters.replays_saved = 13;
  runtime->counters.recovery_checkpoint_attempts = 9;
  runtime->counters.recovery_checkpoint_successes = 8;
  runtime->counters.recovery_checkpoint_failures = 1;
  runtime->counters.recovery_checkpoint_bytes_total = 9223372036854775808ull;
  runtime->counters.recovery_checkpoint_bytes_max = 2048;
  runtime->counters.recovery_checkpoint_serialize_us_total = 120;
  runtime->counters.recovery_checkpoint_write_us_total = 3000;
  runtime->counters.recovery_checkpoint_write_us_max = 1200;
  runtime->counters.recovery_checkpoint_lock_wait_us_total = 18446744073709551615ull;
  runtime->counters.recovery_checkpoint_connection_us_total = 12;
  runtime->counters.recovery_checkpoint_sql_us_total = 13;
  runtime->counters.recovery_checkpoint_commit_us_total = 14;
  runtime->counters.recovery_checkpoint_queue_wait_us_total = 15;
  runtime->counters.recovery_checkpoint_batches = 2;
  runtime->counters.recovery_checkpoint_batch_items_max = 4;
  std::unordered_map<std::string, std::string> fields;
  std::istringstream stream(arena::metrics::rooms_payload(runtime->counters, runtime->gateway_metrics));
  std::string field;
  while (std::getline(stream, field, ';')) {
    const auto separator = field.find('=');
    require(separator != std::string::npos, "metrics wire field syntax");
    require(fields.emplace(field.substr(0, separator), field.substr(separator + 1)).second, "metrics keys are unique");
  }
  require(fields.size() == 46 && fields["active_sessions"] == "3" && fields["active_rooms"] == "2" &&
          fields["send_frames_dropped"] == "7" && fields["settlements_succeeded"] == "11" &&
          fields["settlement_outbox_applied"] == "5" && fields["settlement_outbox_failures"] == "2" &&
          fields["settlement_outbox_pending"] == "1" && fields["replays_saved"] == "13",
          "admin metrics contract unchanged");
  require(fields["connections_rejected"] == "9223372036854775808" &&
          fields["requests_rate_limited"] == "18446744073709551615", "network counters preserve uint64");
  require(fields["heartbeat_timeouts"] == "9223372036854775808", "heartbeat timeouts preserve uint64");
  require(fields["settlement_outbox_load_failures"] == "17" &&
          fields["settlement_outbox_mark_failures"] == "18" &&
          fields["settlement_outbox_record_failures"] == "19" &&
          fields["mysql_connection_attempts"] == "18446744073709551615" &&
          fields["mysql_connection_successes"] == "20" &&
          fields["mysql_connection_failures"] == "21" && fields["mysql_connection_losses"] == "22" &&
          fields["redis_connection_failures"] == "23" && fields["redis_apply_failures"] == "9223372036854775808",
          "failure classifications and lossless uint64 TextV1 metrics");
  require(fields["recovery_checkpoint_attempts"] == "9" && fields["recovery_checkpoint_successes"] == "8" &&
          fields["recovery_checkpoint_failures"] == "1" &&
          fields["recovery_checkpoint_bytes_total"] == "9223372036854775808" &&
          fields["recovery_checkpoint_bytes_max"] == "2048" &&
          fields["recovery_checkpoint_serialize_us_total"] == "120" &&
          fields["recovery_checkpoint_write_us_total"] == "3000" &&
          fields["recovery_checkpoint_write_us_max"] == "1200", "checkpoint observability contract");
  require(fields["recovery_checkpoint_lock_wait_us_total"] == "18446744073709551615" &&
          fields["recovery_checkpoint_connection_us_total"] == "12" &&
          fields["recovery_checkpoint_sql_us_total"] == "13" && fields["recovery_checkpoint_commit_us_total"] == "14" &&
          fields["recovery_checkpoint_queue_wait_us_total"] == "15" && fields["recovery_checkpoint_batches"] == "2" &&
          fields["recovery_checkpoint_batch_items_max"] == "4", "physical batch timing counters are lossless");

  std::atomic<unsigned long long> sequence{1};
  const auto first_id = arena::app::make_match_id(sequence);
  const auto second_id = arena::app::make_match_id(sequence);
  require(first_id != second_id && first_id.substr(first_id.size() - 2) == "-1" &&
          second_id.substr(second_id.size() - 2) == "-2" && sequence.load() == 3, "injected match sequence");
  require(arena::app::replay_seed_for(first_id) == arena::app::replay_seed_for(first_id), "deterministic seed derivation");
  std::cout << "application identity/registry/metrics/runtime isolation checks passed\n";
  return 0;
}
