#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include "server/battle/battle_replay.h"
#include "server/battle/battle_snapshot.h"
#include "server/config/card_catalog.h"

namespace arena::room {

struct RecoveryReceipt {
  std::uint64_t action_id = 0;
  std::string signature;
  std::string response;
};

struct RecoveryCheckpoint {
  std::string match_id;
  std::string rules_fingerprint;
  battle::BattleSnapshot snapshot;
  battle::BattleReplay replay;
  std::array<std::string, 2> users;
  std::array<std::string, 2> tokens;
  std::array<std::vector<RecoveryReceipt>, 2> receipts;
  std::array<std::int64_t, 2> disconnect_deadline_ms{0, 0};
  std::int64_t turn_deadline_ms = 0;
  std::string pending_result;
  bool replay_valid = true;
  bool terminal_replay_recorded = false;
};

inline std::int64_t unix_milliseconds() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
}

inline std::string recovery_rules_fingerprint(const config::CardCatalog& catalog) {
  std::ostringstream result;
  result << "battle_rules_v1:" << catalog.rules_name();
  auto ids = catalog.starter_deck();
  std::sort(ids.begin(), ids.end());
  ids.erase(std::unique(ids.begin(), ids.end()), ids.end());
  for (const int id : ids) {
    const auto& card = *catalog.find(id);
    result << ';' << id << ',' << card.cost << ',' << config::CardCatalog::effect_name(card.effect)
           << ',' << card.value << ',' << card.duration;
  }
  return result.str();
}

// This schema is independent of offline snapshot/replay formats. Length-prefixed
// strings preserve ACK payloads exactly, including their original request IDs.
class RecoveryCodec {
 public:
  static constexpr std::size_t kMaximumBytes = 8 * 1024 * 1024;

  static std::string encode(const RecoveryCheckpoint& checkpoint) {
    Writer output;
    output.data = "ARENA_ROOM_RECOVERY_V1\n";
    write_fields(checkpoint, output);
    output.integer(checksum(output.data));
    return output.data;
  }

  // Count with the same field traversal without copying historical payloads.
  // A journal candidate must fit the full document reconstructed at startup.
  static std::size_t encoded_size(const RecoveryCheckpoint& checkpoint) {
    CountingWriter output;
    output.bytes = std::char_traits<char>::length("ARENA_ROOM_RECOVERY_V1\n") + 8;
    write_fields(checkpoint, output);
    return output.bytes;
  }

  template <typename Output>
  static void write_fields(const RecoveryCheckpoint& checkpoint, Output& output) {
    output.string(checkpoint.match_id);
    output.string(checkpoint.rules_fingerprint);
    const auto& state = checkpoint.snapshot.state;
    output.integer(checkpoint.snapshot.version);
    output.integer(checkpoint.snapshot.revision);
    output.integer(state.turn);
    output.integer(state.turn_id);
    output.integer(state.finished ? 1 : 0);
    output.integer(state.rng_state);
    for (std::size_t index = 0; index < 2; ++index) {
      const auto& player = state.players[index];
      output.integer(player.hp);
      output.integer(player.energy);
      output.integer(player.shield);
      output.integer(state.last_actions[index]);
      output.integer(player.attack_boost.value);
      output.integer(player.attack_boost.uses);
      output.integer(player.heal_boost.value);
      output.integer(player.heal_boost.uses);
      output.integer(player.statuses.poison.value);
      output.integer(player.statuses.poison.turns);
      output.integer(player.statuses.regen.value);
      output.integer(player.statuses.regen.turns);
      output.integer(player.statuses.burn.value);
      output.integer(player.statuses.burn.turns);
      output.cards(player.hand);
      output.cards(player.deck);
      output.cards(player.refill_deck);
      output.cards(player.discard);
      output.string(checkpoint.users[index]);
      output.string(checkpoint.tokens[index]);
      output.integer(checkpoint.disconnect_deadline_ms[index]);
      output.integer(checkpoint.receipts[index].size());
      for (const auto& receipt : checkpoint.receipts[index]) {
        output.integer(receipt.action_id);
        output.string(receipt.signature);
        output.string(receipt.response);
      }
    }
    output.integer(checkpoint.turn_deadline_ms);
    output.string(checkpoint.pending_result);
    output.integer(checkpoint.replay_valid ? 1 : 0);
    output.integer(checkpoint.terminal_replay_recorded ? 1 : 0);
    output.integer(checkpoint.replay.seed());
    output.integer(checkpoint.replay.events().size());
    for (const auto& event : checkpoint.replay.events()) {
      output.integer(event.revision);
      output.integer(event.turn_id);
      output.integer(event.action_id);
      output.integer(event.player_index);
      output.string(event.type);
      output.string(event.payload);
    }
  }

  static bool decode(const std::string& data, RecoveryCheckpoint& checkpoint, std::string& error) {
    constexpr const char* magic = "ARENA_ROOM_RECOVERY_V1\n";
    const std::size_t prefix = std::char_traits<char>::length(magic);
    if (data.size() > kMaximumBytes || data.size() < prefix + 8 || data.compare(0, prefix, magic))
      return fail(error, "invalid_recovery_format");
    Reader input(data, prefix, data.size() - 8);
    Reader trailer(data, data.size() - 8, data.size());
    std::uint64_t expected = 0;
    if (!trailer.integer(expected) || expected != checksum(data.substr(0, data.size() - 8)))
      return fail(error, "recovery_checksum_mismatch");
    RecoveryCheckpoint parsed;
    auto& state = parsed.snapshot.state;
    int finished = 0;
    if (!input.string(parsed.match_id) || !input.string(parsed.rules_fingerprint) ||
        !input.integer(parsed.snapshot.version) || !input.integer(parsed.snapshot.revision) ||
        !input.integer(state.turn) || !input.integer(state.turn_id) ||
        !input.integer(finished) || !input.integer(state.rng_state))
      return fail(error, "truncated_recovery_state");
    if (parsed.snapshot.version != battle::BattleSnapshot::kVersion || state.turn < 0 ||
        state.turn > 1 || state.turn_id == 0 || state.turn_id > 41 ||
        finished < 0 || finished > 1 || state.rng_state == 0)
      return fail(error, "invalid_recovery_state");
    state.finished = finished != 0;
    for (std::size_t index = 0; index < 2; ++index) {
      auto& player = state.players[index];
      std::uint64_t receipt_count = 0;
      if (!input.integer(player.hp) || !input.integer(player.energy) ||
          !input.integer(player.shield) || !input.integer(state.last_actions[index]) ||
          !input.integer(player.attack_boost.value) || !input.integer(player.attack_boost.uses) ||
          !input.integer(player.heal_boost.value) || !input.integer(player.heal_boost.uses) ||
          !input.integer(player.statuses.poison.value) || !input.integer(player.statuses.poison.turns) ||
          !input.integer(player.statuses.regen.value) || !input.integer(player.statuses.regen.turns) ||
          !input.integer(player.statuses.burn.value) || !input.integer(player.statuses.burn.turns) ||
          !input.cards(player.hand) || !input.cards(player.deck) ||
          !input.cards(player.refill_deck) || !input.cards(player.discard) ||
          !input.string(parsed.users[index]) || !input.string(parsed.tokens[index]) ||
          !input.integer(parsed.disconnect_deadline_ms[index]) ||
          !input.integer(receipt_count) || receipt_count > 128)
        return fail(error, "invalid_recovery_player");
      if (player.hp < 0 || player.hp > 30 || player.energy < 0 || player.energy > 10 ||
          player.shield < 0 || parsed.users[index].empty() || parsed.users[index].size() > 64 ||
          parsed.tokens[index].empty() || parsed.tokens[index].size() > 128 ||
          parsed.disconnect_deadline_ms[index] < 0)
        return fail(error, "invalid_recovery_player_values");
      std::uint64_t previous_action = 0;
      for (std::uint64_t count = 0; count < receipt_count; ++count) {
        RecoveryReceipt receipt;
        if (!input.integer(receipt.action_id) || !input.string(receipt.signature) ||
            !input.string(receipt.response) || receipt.action_id <= previous_action ||
            receipt.action_id > state.last_actions[index] || receipt.signature.empty() ||
            receipt.response.empty()) return fail(error, "invalid_recovery_receipt");
        previous_action = receipt.action_id;
        parsed.receipts[index].push_back(std::move(receipt));
      }
      if (previous_action != state.last_actions[index])
        return fail(error, "recovery_last_action_receipt_missing");
    }
    int valid = 0, terminal = 0;
    std::uint64_t seed = 0, event_count = 0;
    if (!input.integer(parsed.turn_deadline_ms) || !input.string(parsed.pending_result) ||
        !input.integer(valid) || !input.integer(terminal) || !input.integer(seed) ||
        !input.integer(event_count) || event_count == 0 || event_count > 20000 ||
        event_count != parsed.snapshot.revision || parsed.turn_deadline_ms <= 0 ||
        valid != 1 || terminal < 0 || terminal > 1)
      return fail(error, "invalid_recovery_metadata");
    parsed.replay_valid = true;
    parsed.terminal_replay_recorded = terminal != 0;
    if (parsed.pending_result.empty() == parsed.terminal_replay_recorded)
      return fail(error, "invalid_recovery_terminal_state");
    if (state.finished && !parsed.terminal_replay_recorded)
      return fail(error, "finished_recovery_without_result");
    parsed.replay = battle::BattleReplay(seed);
    for (std::uint64_t count = 0; count < event_count; ++count) {
      battle::ReplayEvent event;
      if (!input.integer(event.revision) || !input.integer(event.turn_id) ||
          !input.integer(event.action_id) || !input.integer(event.player_index) ||
          !input.string(event.type) || !input.string(event.payload) ||
          !parsed.replay.append(event, &error)) return fail(error, "invalid_recovery_replay");
    }
    if (input.position != input.limit || parsed.match_id.empty() ||
        parsed.replay.events().front().type != "match_start" ||
        (parsed.terminal_replay_recorded && parsed.replay.events().back().type != "match_result"))
      return fail(error, "invalid_recovery_event_sequence");
    checkpoint = std::move(parsed);
    return true;
  }

 private:
  static bool fail(std::string& error, const char* message) { error = message; return false; }
  static std::uint64_t checksum(const std::string& data) {
    std::uint64_t value = 14695981039346656037ull;
    for (const unsigned char byte : data) { value ^= byte; value *= 1099511628211ull; }
    return value;
  }
  struct CountingWriter {
    std::size_t bytes = 0;
    void add(std::size_t count) {
      bytes = count > kMaximumBytes || bytes > kMaximumBytes - count ? kMaximumBytes + 1 : bytes + count;
    }
    template <typename T> void integer(T) { add(8); }
    void string(const std::string& value) { add(8); add(value.size()); }
    void cards(const std::vector<int>& values) {
      add(8);
      add(values.size() > kMaximumBytes / 8 ? kMaximumBytes + 1 : values.size() * 8);
    }
  };
  struct Writer {
    std::string data;
    template <typename T> void integer(T value) {
      const auto bits = static_cast<std::uint64_t>(value);
      for (int shift = 0; shift < 64; shift += 8) data.push_back(static_cast<char>(bits >> shift));
    }
    void string(const std::string& value) { integer(value.size()); data += value; }
    void cards(const std::vector<int>& values) {
      integer(values.size());
      for (const int value : values) integer(value);
    }
  };
  struct Reader {
    const std::string& data;
    std::size_t position;
    std::size_t limit;
    Reader(const std::string& source, std::size_t start, std::size_t end)
        : data(source), position(start), limit(end) {}
    template <typename T> bool integer(T& result) {
      if (limit - position < 8) return false;
      std::uint64_t value = 0;
      for (int shift = 0; shift < 64; shift += 8)
        value |= static_cast<std::uint64_t>(static_cast<unsigned char>(data[position++])) << shift;
      if constexpr (std::numeric_limits<T>::is_signed) {
        const auto signed_value = static_cast<std::int64_t>(value);
        if (signed_value < std::numeric_limits<T>::min() || signed_value > std::numeric_limits<T>::max())
          return false;
        result = static_cast<T>(signed_value);
      } else {
        if (value > std::numeric_limits<T>::max()) return false;
        result = static_cast<T>(value);
      }
      return true;
    }
    bool string(std::string& result) {
      std::uint64_t length = 0;
      if (!integer(length) || length > limit - position) return false;
      result.assign(data, position, static_cast<std::size_t>(length));
      position += static_cast<std::size_t>(length);
      return true;
    }
    bool cards(std::vector<int>& result) {
      std::uint64_t count = 0;
      if (!integer(count) || count > 10000) return false;
      result.clear();
      for (std::uint64_t index = 0; index < count; ++index) {
        int value = 0;
        if (!integer(value) || value <= 0 || value > 1000) return false;
        result.push_back(value);
      }
      return true;
    }
  };
};

}  // namespace arena::room
