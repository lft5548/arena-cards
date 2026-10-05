#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <string>
#include <vector>

#include "server/battle/battle_engine.h"
#include "server/room/recovery_tail.h"

using arena::room::RecoveryCheckpoint;
using arena::room::RecoveryCodec;
using arena::room::RecoveryTailCodec;

static void check(bool condition, const char* message) {
  if (!condition) { std::cerr << message << '\n'; std::exit(1); }
}

static RecoveryCheckpoint initial(const std::shared_ptr<arena::config::CardCatalog>& catalog) {
  arena::battle::BattleEngine engine(catalog);
  engine.set_rng_seed(811);
  RecoveryCheckpoint checkpoint;
  checkpoint.match_id = "tail-test";
  checkpoint.rules_fingerprint = arena::room::recovery_rules_fingerprint(*catalog);
  checkpoint.snapshot = engine.snapshot(1);
  checkpoint.replay = arena::battle::BattleReplay(811);
  check(checkpoint.replay.append({1, 1, 0, -1, "match_start", "deck=1,2,3"}), "start append failed");
  checkpoint.users = {"alice", "bob"};
  checkpoint.tokens = {"token-a", "token-b"};
  checkpoint.turn_deadline_ms = 1888111222333;
  return checkpoint;
}

static void append(RecoveryCheckpoint& checkpoint, const std::string& type,
                   std::uint64_t action = 0, int player = -1, const std::string& payload = "") {
  const auto revision = static_cast<std::uint64_t>(checkpoint.replay.events().size()) + 1;
  check(checkpoint.replay.append({revision, checkpoint.snapshot.state.turn_id, action, player, type, payload}),
        "event append failed");
  checkpoint.snapshot.revision = revision;
}

static std::string roundtrip(const RecoveryCheckpoint& base, const RecoveryCheckpoint& next,
                             std::uint64_t sequence) {
  std::string data, error;
  check(RecoveryTailCodec::encode(base, next, sequence - 1, sequence, data, error), error.c_str());
  std::uint64_t previous = 0, observed = 0;
  check(RecoveryTailCodec::inspect(data, previous, observed, error), error.c_str());
  check(previous == sequence - 1 && observed == sequence, "storage sequence changed");
  RecoveryCheckpoint restored;
  check(RecoveryTailCodec::apply(base, data, sequence - 1, restored, error), error.c_str());
  check(RecoveryCodec::encode(restored) == RecoveryCodec::encode(next), "tail recovery changed authoritative data");
  check(RecoveryCodec::encoded_size(next) == RecoveryCodec::encode(next).size(), "checkpoint size changed");
  return data;
}

// Recompute the trailer only to test structural validation after a valid
// checksum, rather than letting checksum rejection hide a parser defect.
static void seal(std::string& data) {
  std::uint64_t hash = 14695981039346656037ull;
  for (std::size_t index = 0; index < data.size() - 8; ++index) {
    hash ^= static_cast<unsigned char>(data[index]); hash *= 1099511628211ull;
  }
  for (unsigned index = 0; index < 8; ++index)
    data[data.size() - 8 + index] = static_cast<char>(hash >> (index * 8));
}

int main(int argc, char** argv) {
  check(argc == 2, "card config required");
  auto catalog = std::make_shared<arena::config::CardCatalog>();
  std::string error;
  check(catalog->load_csv(argv[1], error), error.c_str());
  const auto base = initial(catalog);
  {
    // A small append can cross the reconstructed-document limit even though
    // the journal record itself would fit. Never confirm such a candidate.
    auto large = base;
    append(large, "battle_event", 0, -1,
           std::string(RecoveryCodec::kMaximumBytes - RecoveryCodec::encoded_size(base) - 1024, 'x'));
    check(RecoveryCodec::encoded_size(large) <= RecoveryCodec::kMaximumBytes, "large base exceeds limit");
    auto oversized = large;
    append(oversized, "battle_event", 0, -1, std::string(2048, 'y'));
    std::string rejected;
    check(!RecoveryTailCodec::encode(large, oversized, 0, 1, rejected, error) &&
              error == "reconstructed_checkpoint_too_large", "unrecoverable oversized candidate accepted");
  }
  auto changed = base;
  auto& state = changed.snapshot.state;
  state.turn = 1;
  state.turn_id = 2;
  state.rng_state = 829981;
  for (unsigned index = 0; index < 2; ++index) {
    auto& player = state.players[index];
    player.hp = 25 + static_cast<int>(index);
    player.energy = 4 + static_cast<int>(index);
    player.shield = 5 + static_cast<int>(index);
    player.attack_boost = {2, 3};
    player.heal_boost = {3, 2};
    player.statuses.poison = {3, 2};
    player.statuses.regen = {2, 3};
    player.statuses.burn = {4, 1};
    player.hand = {3, 2, 1};
    player.deck = {12, 11, 10};
    player.refill_deck = {9, 8, 7};
    player.discard = {1, 2};
    state.last_actions[index] = 1;
    changed.users[index] += "-changed";
    changed.tokens[index] += "-changed";
    changed.disconnect_deadline_ms[index] = 1888111222000 + index;
    changed.receipts[index].push_back({1, "play:1:1", "ACK\nrequest_id=exact\";payload=\x01"});
  }
  changed.turn_deadline_ms += 500;
  append(changed, "battle_event", 1, 0, "first");
  append(changed, "battle_event", 1, 1, "second");
  const auto all_fields = roundtrip(base, changed, 1);
  check(all_fields.size() < RecoveryCodec::encode(changed).size(), "tail did not reduce write bytes");

  auto metadata = changed;
  metadata.disconnect_deadline_ms = {0, 1888111224000};
  metadata.turn_deadline_ms += 1000;
  const auto metadata_tail = roundtrip(changed, metadata, 2);
  check(metadata.snapshot.revision == changed.snapshot.revision, "metadata changed battle revision");
  check(metadata_tail.size() < all_fields.size(), "metadata record rewrote battle state");

  auto terminal = metadata;
  terminal.snapshot.state.finished = true;
  terminal.pending_result = "winner=0;reason=max_turns";
  terminal.terminal_replay_recorded = true;
  append(terminal, "match_result", 0, -1, terminal.pending_result);
  roundtrip(metadata, terminal, 3);

  // Receipt retention is an old suffix plus new entries, preserving the exact
  // original ACK, signature and request ID through eviction at the 128 limit.
  auto receipts = base;
  for (std::uint64_t action = 1; action <= 128; ++action)
    receipts.receipts[0].push_back({action, "sig-" + std::to_string(action), "ACK-" + std::to_string(action)});
  receipts.snapshot.state.last_actions[0] = 128;
  auto evicted = receipts;
  evicted.receipts[0].erase(evicted.receipts[0].begin());
  evicted.receipts[0].push_back({129, "sig-129", "ACK-129;request_id=original"});
  evicted.snapshot.state.last_actions[0] = 129;
  const auto receipt_tail = roundtrip(receipts, evicted, 4);
  check(receipt_tail.size() * 4 < RecoveryCodec::encode(evicted).size(), "tail rewrote retained ACK history");

  RecoveryCheckpoint untouched = base;
  const auto untouched_data = RecoveryCodec::encode(untouched);
  const auto reject = [&](const RecoveryCheckpoint& checkpoint, const std::string& data, std::uint64_t sequence) {
    check(!RecoveryTailCodec::apply(checkpoint, data, sequence, untouched, error), "invalid tail accepted");
    check(RecoveryCodec::encode(untouched) == untouched_data, "failed apply changed output");
  };
  reject(base, all_fields, 1);  // Missing preceding record / unexpected sequence.
  reject(changed, metadata_tail, 0);  // Reordered records.
  reject(metadata, all_fields, 0);  // Replayed older battle revision.
  auto other = base;
  other.match_id = "other-match";
  reject(other, all_fields, 0);
  other = base;
  other.rules_fingerprint += "-other";
  reject(other, all_fields, 0);
  auto corrupt = all_fields;
  corrupt[corrupt.size() / 2] ^= 1;
  reject(base, corrupt, 0);
  for (std::size_t length = 0; length < all_fields.size(); ++length)
    reject(base, all_fields.substr(0, length), 0);
  corrupt = all_fields;
  // Magic is followed by two uint64 sequences. A valid checksum cannot make
  // a non-contiguous storage sequence or overflow acceptable.
  const auto prefix = std::char_traits<char>::length("ARENA_ROOM_TAIL_V1\n");
  corrupt[prefix + 8] = 2;
  seal(corrupt);
  reject(base, corrupt, 0);
  std::string encoded;
  check(!RecoveryTailCodec::encode(base, changed, std::numeric_limits<std::uint64_t>::max(), 0,
                                  encoded, error), "sequence overflow accepted");
  auto invalid = changed;
  invalid.receipts[0][0].response += "mutated";
  check(!RecoveryTailCodec::encode(changed, invalid, 1, 2, encoded, error), "old ACK rewrite accepted");
  invalid = changed;
  invalid.replay = arena::battle::BattleReplay(811);
  check(invalid.replay.append({1, 1, 0, -1, "match_start", "changed"}), "invalid fixture failed");
  append(invalid, "battle_event");
  append(invalid, "battle_event");
  check(!RecoveryTailCodec::encode(changed, invalid, 1, 2, encoded, error), "replay prefix rewrite accepted");
  invalid = changed;
  invalid.snapshot.state.players[0].hp = 31;
  check(RecoveryTailCodec::encode(base, invalid, 0, 1, encoded, error), "invalid state fixture encoding failed");
  reject(base, encoded, 0);  // State constraints remain identical to full checkpoints.

  // Resume the actual C++ battle engine after every persisted tail record.
  // Harmless card plays drain/refill decks and change the captured RNG;
  // the same seed/commands must produce exactly the uninterrupted outcome.
  arena::battle::BattleEngine uninterrupted(catalog), resumed(catalog);
  uninterrupted.set_rng_seed(811);
  resumed.set_rng_seed(811);
  auto current = base;
  std::uint64_t storage_sequence = 0;
  while (!uninterrupted.state().finished) {
    const auto& live = uninterrupted.state();
    const int player = live.turn;
    int card = 0;
    for (const int candidate : live.players[player].hand) {
      const auto* definition = catalog->find(candidate);
      if (definition && definition->cost <= live.players[player].energy &&
          (definition->effect == arena::config::Effect::Heal || definition->effect == arena::config::Effect::Shield)) {
        card = candidate; break;
      }
    }
    const arena::battle::BattleCommand command{
        card ? arena::battle::CommandKind::PlayCard : arena::battle::CommandKind::EndTurn,
        player, live.turn_id, live.last_actions[player] + 1, card};
    const auto outcome = uninterrupted.apply(command);
    const auto restored_outcome = resumed.apply(command);
    check(outcome.error.empty() && restored_outcome.error.empty(), "resumed command rejected");
    check(outcome.terminal == restored_outcome.terminal && outcome.winner == restored_outcome.winner,
          "resumed result diverged");
    auto next = current;
    for (const auto& event : outcome.events)
      append(next, "battle_event", event.action_id, event.player, event.payload(next.match_id));
    if (outcome.terminal) {
      next.pending_result = "winner=" + std::to_string(outcome.winner);
      next.terminal_replay_recorded = true;
      append(next, "match_result", 0, -1, next.pending_result);
    }
    next.snapshot = uninterrupted.snapshot(next.replay.events().size());
    next.receipts[player].push_back({command.action_id, "command", "ACK:" + std::to_string(command.action_id)});
    next.turn_deadline_ms += 100;
    const auto data = roundtrip(current, next, ++storage_sequence);
    RecoveryCheckpoint restored;
    check(RecoveryTailCodec::apply(current, data, storage_sequence - 1, restored, error), error.c_str());
    check(resumed.restore(restored.snapshot, &error), error.c_str());
    check(RecoveryCodec::encode(restored) == RecoveryCodec::encode(next), "long chain data diverged");
    current = std::move(restored);
  }
  check(current.snapshot.state.turn_id == 41, "long battle did not reach maximum turn");
  std::cout << "room recovery tail test passed\n";
}
