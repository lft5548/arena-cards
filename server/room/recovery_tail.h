#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#include "server/room/recovery_checkpoint.h"

namespace arena::room {

// The online journal records authoritative state changes, appended battle
// events and receipt changes. It does not re-execute client commands. The
// storage sequence is independent of the battle revision: disconnects and
// deadline changes also produce durable journal records.
class RecoveryTailCodec {
 public:
  static constexpr std::size_t kMaximumBytes = RecoveryCodec::kMaximumBytes;

  static bool encode(const RecoveryCheckpoint& base, const RecoveryCheckpoint& next,
                     std::uint64_t previous_sequence, std::uint64_t sequence,
                     std::string& data, std::string& error) {
    if (!valid_sequence(previous_sequence, sequence))
      return fail(error, "invalid_tail_sequence");
    if (RecoveryCodec::encoded_size(next) > kMaximumBytes)
      return fail(error, "reconstructed_checkpoint_too_large");
    if (base.match_id != next.match_id || base.rules_fingerprint != next.rules_fingerprint ||
        base.snapshot.version != next.snapshot.version || base.replay.seed() != next.replay.seed())
      return fail(error, "tail_identity_changed");
    if (base.match_id.empty() || base.rules_fingerprint.empty() ||
        next.snapshot.version != battle::BattleSnapshot::kVersion ||
        base.snapshot.revision != base.replay.events().size() ||
        next.snapshot.revision != next.replay.events().size() ||
        next.replay.events().size() < base.replay.events().size() ||
        next.replay.events().size() > 20000)
      return fail(error, "invalid_tail_revisions");
    if (!std::equal(base.replay.events().begin(), base.replay.events().end(),
                    next.replay.events().begin()))
      return fail(error, "tail_replay_prefix_changed");
    if (next.snapshot.state.last_actions[0] < base.snapshot.state.last_actions[0] ||
        next.snapshot.state.last_actions[1] < base.snapshot.state.last_actions[1])
      return fail(error, "tail_action_regressed");

    Writer changes;
    std::uint64_t mask = 0;
    const auto integer = [&](unsigned bit, auto before, auto after) {
      if (before != after) { mask |= std::uint64_t{1} << bit; changes.integer(after); }
    };
    const auto string = [&](unsigned bit, const std::string& before, const std::string& after) {
      if (before != after) { mask |= std::uint64_t{1} << bit; changes.string(after); }
    };
    const auto cards = [&](unsigned bit, const std::vector<int>& before, const std::vector<int>& after) {
      if (before != after) { mask |= std::uint64_t{1} << bit; changes.cards(after); }
    };
    const auto& before = base.snapshot.state;
    const auto& after = next.snapshot.state;
    integer(0, before.turn, after.turn);
    integer(1, before.turn_id, after.turn_id);
    integer(2, before.finished, after.finished);
    integer(3, before.rng_state, after.rng_state);
    for (unsigned index = 0; index < 2; ++index) {
      const auto& old_player = before.players[index];
      const auto& player = after.players[index];
      const unsigned bit = 4 + index * 18;
      integer(bit, old_player.hp, player.hp);
      integer(bit + 1, old_player.energy, player.energy);
      integer(bit + 2, old_player.shield, player.shield);
      integer(bit + 3, before.last_actions[index], after.last_actions[index]);
      integer(bit + 4, old_player.attack_boost.value, player.attack_boost.value);
      integer(bit + 5, old_player.attack_boost.uses, player.attack_boost.uses);
      integer(bit + 6, old_player.heal_boost.value, player.heal_boost.value);
      integer(bit + 7, old_player.heal_boost.uses, player.heal_boost.uses);
      integer(bit + 8, old_player.statuses.poison.value, player.statuses.poison.value);
      integer(bit + 9, old_player.statuses.poison.turns, player.statuses.poison.turns);
      integer(bit + 10, old_player.statuses.regen.value, player.statuses.regen.value);
      integer(bit + 11, old_player.statuses.regen.turns, player.statuses.regen.turns);
      integer(bit + 12, old_player.statuses.burn.value, player.statuses.burn.value);
      integer(bit + 13, old_player.statuses.burn.turns, player.statuses.burn.turns);
      cards(bit + 14, old_player.hand, player.hand);
      cards(bit + 15, old_player.deck, player.deck);
      cards(bit + 16, old_player.refill_deck, player.refill_deck);
      cards(bit + 17, old_player.discard, player.discard);
    }
    for (unsigned index = 0; index < 2; ++index) {
      const unsigned bit = 40 + index * 4;
      string(bit, base.users[index], next.users[index]);
      string(bit + 1, base.tokens[index], next.tokens[index]);
      integer(bit + 2, base.disconnect_deadline_ms[index], next.disconnect_deadline_ms[index]);
      if (!same_receipts(base.receipts[index], next.receipts[index])) {
        mask |= std::uint64_t{1} << (bit + 3);
        if (!receipt_delta(base.receipts[index], next.receipts[index], changes, error)) return false;
      }
    }
    integer(48, base.turn_deadline_ms, next.turn_deadline_ms);
    string(49, base.pending_result, next.pending_result);
    integer(50, base.replay_valid, next.replay_valid);
    integer(51, base.terminal_replay_recorded, next.terminal_replay_recorded);

    Writer output;
    output.data = magic();
    output.integer(previous_sequence);
    output.integer(sequence);
    output.string(next.match_id);
    output.string(next.rules_fingerprint);
    output.integer(next.snapshot.version);
    output.integer(base.snapshot.revision);
    output.integer(next.snapshot.revision);
    output.integer(next.replay.seed());
    output.integer(mask);
    output.data += changes.data;
    output.integer(next.replay.events().size() - base.replay.events().size());
    for (std::size_t index = base.replay.events().size(); index < next.replay.events().size(); ++index) {
      const auto& event = next.replay.events()[index];
      if (event.revision != index + 1 || event.player_index < -1 || event.player_index > 1 ||
          event.type.empty()) return fail(error, "invalid_tail_event");
      output.integer(event.revision);
      output.integer(event.turn_id);
      output.integer(event.action_id);
      output.integer(event.player_index);
      output.string(event.type);
      output.string(event.payload);
    }
    output.integer(checksum(output.data));
    if (output.data.size() > kMaximumBytes) return fail(error, "tail_too_large");
    data = std::move(output.data);
    error.clear();
    return true;
  }

  // Application is atomic in memory: rejected data leaves next unchanged.
  // Startup uses the full checkpoint validator after each journal record;
  // normal writes never encode/decode the full historical replay for validation.
  static bool apply(const RecoveryCheckpoint& base, const std::string& data,
                    std::uint64_t expected_previous_sequence,
                    RecoveryCheckpoint& next, std::string& error) {
    std::uint64_t previous_sequence = 0, sequence = 0;
    if (!inspect(data, previous_sequence, sequence, error)) return false;
    if (previous_sequence != expected_previous_sequence)
      return fail(error, "tail_sequence_gap");
    Reader input(data, std::char_traits<char>::length(magic()) + 16, data.size() - 8);
    std::string match_id, fingerprint;
    std::uint32_t version = 0;
    std::uint64_t base_revision = 0, revision = 0, seed = 0, mask = 0;
    if (!input.string(match_id) || !input.string(fingerprint) || !input.integer(version) ||
        !input.integer(base_revision) || !input.integer(revision) || !input.integer(seed) ||
        !input.integer(mask)) return fail(error, "truncated_tail_header");
    if (match_id != base.match_id || fingerprint != base.rules_fingerprint ||
        version != base.snapshot.version || seed != base.replay.seed())
      return fail(error, "tail_identity_mismatch");
    if (base_revision != base.snapshot.revision || base_revision != base.replay.events().size() ||
        revision < base_revision || revision > 20000 || (mask >> 52) != 0)
      return fail(error, "invalid_tail_revisions");

    RecoveryCheckpoint parsed = base;
    auto& state = parsed.snapshot.state;
    const auto integer = [&](unsigned bit, auto& value) {
      return (mask & (std::uint64_t{1} << bit)) == 0 || input.integer(value);
    };
    const auto boolean = [&](unsigned bit, bool& value) {
      if ((mask & (std::uint64_t{1} << bit)) == 0) return true;
      int encoded = 0;
      if (!input.integer(encoded) || encoded < 0 || encoded > 1) return false;
      value = encoded != 0;
      return true;
    };
    const auto string = [&](unsigned bit, std::string& value) {
      return (mask & (std::uint64_t{1} << bit)) == 0 || input.string(value);
    };
    const auto cards = [&](unsigned bit, std::vector<int>& value) {
      return (mask & (std::uint64_t{1} << bit)) == 0 || input.cards(value);
    };
    if (!integer(0, state.turn) || !integer(1, state.turn_id) || !boolean(2, state.finished) ||
        !integer(3, state.rng_state)) return fail(error, "invalid_tail_state");
    for (unsigned index = 0; index < 2; ++index) {
      auto& player = state.players[index];
      const unsigned bit = 4 + index * 18;
      if (!integer(bit, player.hp) || !integer(bit + 1, player.energy) ||
          !integer(bit + 2, player.shield) || !integer(bit + 3, state.last_actions[index]) ||
          !integer(bit + 4, player.attack_boost.value) || !integer(bit + 5, player.attack_boost.uses) ||
          !integer(bit + 6, player.heal_boost.value) || !integer(bit + 7, player.heal_boost.uses) ||
          !integer(bit + 8, player.statuses.poison.value) || !integer(bit + 9, player.statuses.poison.turns) ||
          !integer(bit + 10, player.statuses.regen.value) || !integer(bit + 11, player.statuses.regen.turns) ||
          !integer(bit + 12, player.statuses.burn.value) || !integer(bit + 13, player.statuses.burn.turns) ||
          !cards(bit + 14, player.hand) || !cards(bit + 15, player.deck) ||
          !cards(bit + 16, player.refill_deck) || !cards(bit + 17, player.discard))
        return fail(error, "invalid_tail_player");
      if (state.last_actions[index] < base.snapshot.state.last_actions[index])
        return fail(error, "tail_action_regressed");
    }
    for (unsigned index = 0; index < 2; ++index) {
      const unsigned bit = 40 + index * 4;
      if (!string(bit, parsed.users[index]) || !string(bit + 1, parsed.tokens[index]) ||
          !integer(bit + 2, parsed.disconnect_deadline_ms[index]))
        return fail(error, "invalid_tail_identity");
      if ((mask & (std::uint64_t{1} << (bit + 3))) != 0 &&
          !apply_receipts(input, parsed.receipts[index], error)) return false;
    }
    if (!integer(48, parsed.turn_deadline_ms) || !string(49, parsed.pending_result) ||
        !boolean(50, parsed.replay_valid) || !boolean(51, parsed.terminal_replay_recorded))
      return fail(error, "invalid_tail_metadata");
    std::uint64_t count = 0;
    if (!input.integer(count) || count != revision - base_revision)
      return fail(error, "tail_event_count_mismatch");
    for (std::uint64_t index = 0; index < count; ++index) {
      battle::ReplayEvent event;
      if (!input.integer(event.revision) || !input.integer(event.turn_id) ||
          !input.integer(event.action_id) || !input.integer(event.player_index) ||
          !input.string(event.type) || !input.string(event.payload) ||
          !parsed.replay.append(event, &error)) return fail(error, "invalid_tail_event");
    }
    if (input.position != input.limit) return fail(error, "tail_trailing_data");
    parsed.snapshot.revision = revision;
    RecoveryCheckpoint validated;
    if (!RecoveryCodec::decode(RecoveryCodec::encode(parsed), validated, error)) return false;
    next = std::move(validated);
    error.clear();
    return true;
  }

  static bool inspect(const std::string& data, std::uint64_t& previous_sequence,
                      std::uint64_t& sequence, std::string& error) {
    const std::size_t prefix = std::char_traits<char>::length(magic());
    if (data.size() < prefix + 24 || data.size() > kMaximumBytes ||
        data.compare(0, prefix, magic()) != 0) return fail(error, "invalid_tail_format");
    Reader trailer(data, data.size() - 8, data.size());
    std::uint64_t expected = 0;
    if (!trailer.integer(expected) || expected != checksum(data, data.size() - 8))
      return fail(error, "tail_checksum_mismatch");
    Reader input(data, prefix, data.size() - 8);
    std::uint64_t previous = 0, next = 0;
    if (!input.integer(previous) || !input.integer(next) || !valid_sequence(previous, next))
      return fail(error, "invalid_tail_sequence");
    previous_sequence = previous;
    sequence = next;
    error.clear();
    return true;
  }

 private:
  static const char* magic() { return "ARENA_ROOM_TAIL_V1\n"; }
  static bool fail(std::string& error, const char* message) { error = message; return false; }
  static bool valid_sequence(std::uint64_t previous, std::uint64_t sequence) {
    return previous != std::numeric_limits<std::uint64_t>::max() && sequence == previous + 1;
  }
  static std::uint64_t checksum(const std::string& data, std::size_t count) {
    std::uint64_t value = 14695981039346656037ull;
    for (std::size_t index = 0; index < count; ++index) {
      value ^= static_cast<unsigned char>(data[index]);
      value *= 1099511628211ull;
    }
    return value;
  }
  static std::uint64_t checksum(const std::string& data) { return checksum(data, data.size()); }
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
  static bool same_receipt(const RecoveryReceipt& lhs, const RecoveryReceipt& rhs) {
    return lhs.action_id == rhs.action_id && lhs.signature == rhs.signature && lhs.response == rhs.response;
  }
  static bool same_receipts(const std::vector<RecoveryReceipt>& lhs,
                            const std::vector<RecoveryReceipt>& rhs) {
    return lhs.size() == rhs.size() && std::equal(lhs.begin(), lhs.end(), rhs.begin(), same_receipt);
  }
  static bool receipt_delta(const std::vector<RecoveryReceipt>& base,
                            const std::vector<RecoveryReceipt>& next,
                            Writer& output, std::string& error) {
    if (next.size() > 128) return fail(error, "tail_receipt_overflow");
    std::size_t drop = base.size();
    if (!next.empty()) {
      for (std::size_t index = 0; index < base.size(); ++index) {
        if (base[index].action_id == next.front().action_id) { drop = index; break; }
      }
    }
    const std::size_t retained = base.size() - drop;
    if (retained > next.size()) return fail(error, "tail_receipt_history_changed");
    for (std::size_t index = 0; index < retained; ++index) {
      if (!same_receipt(base[drop + index], next[index]))
        return fail(error, "tail_receipt_history_changed");
    }
    std::uint64_t previous = base.empty() ? 0 : base.back().action_id;
    output.integer(drop);
    output.integer(next.size() - retained);
    for (std::size_t index = retained; index < next.size(); ++index) {
      const auto& receipt = next[index];
      if (receipt.action_id <= previous || receipt.signature.empty() || receipt.response.empty())
        return fail(error, "invalid_tail_receipt");
      previous = receipt.action_id;
      output.integer(receipt.action_id);
      output.string(receipt.signature);
      output.string(receipt.response);
    }
    return true;
  }
  static bool apply_receipts(Reader& input, std::vector<RecoveryReceipt>& receipts, std::string& error) {
    std::uint64_t drop = 0, count = 0;
    if (!input.integer(drop) || !input.integer(count) || drop > receipts.size() ||
        count > 128 || receipts.size() - drop + count > 128)
      return fail(error, "invalid_tail_receipt_count");
    std::uint64_t previous = receipts.empty() ? 0 : receipts.back().action_id;
    receipts.erase(receipts.begin(), receipts.begin() + static_cast<std::ptrdiff_t>(drop));
    for (std::uint64_t index = 0; index < count; ++index) {
      RecoveryReceipt receipt;
      if (!input.integer(receipt.action_id) || !input.string(receipt.signature) ||
          !input.string(receipt.response) || receipt.action_id <= previous ||
          receipt.signature.empty() || receipt.response.empty())
        return fail(error, "invalid_tail_receipt");
      previous = receipt.action_id;
      receipts.push_back(std::move(receipt));
    }
    return true;
  }
};

}  // namespace arena::room
