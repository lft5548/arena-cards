#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>

#include "server/persistence/mysql_store.h"

static void check(bool condition, const std::string& message) {
  if (!condition) { std::cerr << message << "\n"; std::exit(1); }
}

int main() {
  using arena::persistence::environment;
  using arena::persistence::RoomRecoveryWrite;
  using arena::persistence::RoomRecoveryWriteKind;
  arena::persistence::MysqlStore store;
  std::string error;
  check(store.connect(environment("ARENA_MYSQL_HOST", "127.0.0.1"),
                      static_cast<unsigned int>(std::stoul(environment("ARENA_MYSQL_PORT", "3307"))),
                      environment("ARENA_MYSQL_USER"), environment("ARENA_MYSQL_PASSWORD"),
                      environment("ARENA_MYSQL_DATABASE"), error), error);
  check(store.acquire_instance_lock("recovery-owner-test", error), error);
  check(store.ensure_outbox_schema(error), error);
  check(store.initialize_room_recovery(error), error);
  store.configure_checkpoint_batching(16);
  store.set_room_recovery_owner("old-process-owner");
  const std::string match = "recovery-owner-test";
  check(store.begin_recoverable_match(match, "owner_a", "owner_b", "old-checkpoint", error), error);
  check(store.begin_recoverable_match(match, "owner_a", "owner_b", "old-checkpoint", error),
        "initial recovery creation is not idempotent: " + error);
  store.set_room_recovery_owner("new-process-owner");
  check(store.claim_room_checkpoints(error), error);
  RoomRecoveryWrite write{RoomRecoveryWriteKind::FullSnapshot, 0, 1, "new-checkpoint"};
  check(store.save_room_recovery(match, write, error), error);
  check(store.save_room_recovery(match, write, error), "full write retry is not idempotent: " + error);
  write.payload = "conflicting-full";
  check(!store.save_room_recovery(match, write, error), "same full sequence accepted a different candidate");
  write = {RoomRecoveryWriteKind::Tail, 1, 3, "gap"};
  check(!store.save_room_recovery(match, write, error), "nonconsecutive sequence was accepted");
  write = {RoomRecoveryWriteKind::Tail, std::numeric_limits<std::uint64_t>::max(), 0, "overflow"};
  check(!store.save_room_recovery(match, write, error), "sequence overflow was accepted");
  write = {RoomRecoveryWriteKind::Tail, 1, 2, "delta-before-snapshot"};
  check(!store.save_room_recovery(match, write, error), "tail was appended to full representation");
  write = {RoomRecoveryWriteKind::Snapshot, 1, 2, "tail-base"};
  check(store.save_room_recovery(match, write, error), "full-to-tail switch failed: " + error);
  check(store.save_room_recovery(match, write, error), "snapshot write retry is not idempotent: " + error);
  write = {RoomRecoveryWriteKind::Tail, 2, 3, std::string("delta\0bytes", 11)};
  check(store.save_room_recovery(match, write, error), error);
  check(store.save_room_recovery(match, write, error), "tail write retry is not idempotent: " + error);
  write.payload = "conflicting-tail";
  check(!store.save_room_recovery(match, write, error), "same tail sequence accepted a different candidate");
  check(!store.save_room_checkpoint(match, "legacy-overwrite", error), "legacy API discarded a tail");
  check(!store.begin_recoverable_match(match, "owner_a", "owner_b", "old-checkpoint", error),
        "initial retry overwrote a progressed recovery row");
  store.set_room_recovery_owner("old-process-owner");
  write = {RoomRecoveryWriteKind::Tail, 3, 4, "stale-tail"};
  check(!store.save_room_recovery(match, write, error), "stale owner appended a tail");
  write = {RoomRecoveryWriteKind::Snapshot, 3, 4, "stale-snapshot"};
  check(!store.save_room_recovery(match, write, error), "stale owner published a snapshot");
  check(!store.save_room_checkpoint(match, "stale-checkpoint", error), "stale owner overwrote checkpoint");
  check(!store.begin_recoverable_match(match, "owner_a", "owner_b", "stale-start", error),
        "stale owner recreated checkpoint");
  check(!store.delete_room_checkpoint(match, error), "stale owner deleted checkpoint");
  check(!store.settle(match, "owner_a", "owner_b", 0, 1, "owner_test", error),
        "stale owner settled a room owned by the new process");
  std::vector<arena::persistence::RoomRecoveryRow> rows;
  check(store.load_room_checkpoints(rows, error), error);
  check(rows.size() == 1 && rows[0].checkpoint == "tail-base" && rows[0].recovery_format == "tail" &&
        rows[0].snapshot_sequence == 2 && rows[0].current_sequence == 3 && rows[0].tails.size() == 1 &&
        rows[0].tails[0].first == 3 && rows[0].tails[0].second == std::string("delta\0bytes", 11),
        "retry or owner rejection altered durable snapshot/tail state");
  store.set_room_recovery_owner("new-process-owner");
  write = {RoomRecoveryWriteKind::FullSnapshot, 3, 4, "switched-full"};
  check(store.save_room_recovery(match, write, error), "tail-to-full switch failed: " + error);
  check(store.save_room_recovery(match, write, error), "mode switch retry is not idempotent: " + error);
  check(store.load_room_checkpoints(rows, error), error);
  check(rows.size() == 1 && rows[0].recovery_format == "full" && rows[0].snapshot_sequence == 4 &&
        rows[0].current_sequence == 4 && rows[0].checkpoint == "switched-full" && rows[0].tails.empty(),
        "tail-to-full switch left an incomplete representation");
  write = {RoomRecoveryWriteKind::Snapshot, 4, 5, "next-tail-base"};
  check(store.save_room_recovery(match, write, error), error);
  write = {RoomRecoveryWriteKind::Tail, 5, 6, "next-delta"};
  check(store.save_room_recovery(match, write, error), error);
  write = {RoomRecoveryWriteKind::Snapshot, 6, 7, "compacted-base"};
  check(store.save_room_recovery(match, write, error), error);
  check(store.save_room_recovery(match, write, error), "compaction retry is not idempotent: " + error);
  check(store.load_room_checkpoints(rows, error), error);
  check(rows.size() == 1 && rows[0].snapshot_sequence == 7 && rows[0].current_sequence == 7 &&
        rows[0].checkpoint == "compacted-base" && rows[0].tails.empty(), "compaction left covered tails");
  write = {RoomRecoveryWriteKind::Tail, 7, 8, "terminal-tail"};
  check(store.save_room_recovery(match, write, error), error);
  check(store.settle(match, "owner_a", "owner_b", 0, 1, "owner_test", error), error);
  check(store.settle(match, "owner_a", "owner_b", 0, 1, "owner_test", error), error);
  check(store.delete_room_checkpoint(match, error), error);
  check(store.delete_room_checkpoint(match, error), "checkpoint deletion is not idempotent");
  check(store.load_room_checkpoints(rows, error) && rows.empty(), "deleted snapshot retained a loaded tail");
  std::cout << "mysql room recovery ownership test passed\n";
}
