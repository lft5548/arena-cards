#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace arena::battle {

// A compact, deterministic event log for post-match replay and desync checks.
// The actor is expected to append events in revision order; no synchronization
// is needed here because ownership stays with the room actor.
struct ReplayEvent {
  std::uint64_t revision = 0;
  std::uint64_t turn_id = 0;
  std::uint64_t action_id = 0;
  std::int32_t player_index = -1;
  std::string type;
  std::string payload;

  bool operator==(const ReplayEvent& other) const {
    return revision == other.revision && turn_id == other.turn_id &&
           action_id == other.action_id && player_index == other.player_index &&
           type == other.type && payload == other.payload;
  }
};

class BattleReplay {
 public:
  explicit BattleReplay(std::uint64_t seed = 0) : seed_(seed) {}

  std::uint64_t seed() const { return seed_; }
  const std::vector<ReplayEvent>& events() const { return events_; }

  // Revisions are the authoritative event sequence number. Requiring the
  // next revision here makes gaps and accidental duplicate appends visible at
  // the state-owner boundary instead of producing an unreplayable log.
  bool append(const ReplayEvent& event, std::string* error = nullptr) {
    const std::uint64_t expected =
        static_cast<std::uint64_t>(events_.size()) + 1;
    if (event.revision != expected) {
      set_error(error, "revision_must_be_contiguous");
      return false;
    }
    if (event.type.empty()) {
      set_error(error, "event_type_required");
      return false;
    }
    if (event.player_index < -1 || event.player_index > 1) {
      set_error(error, "invalid_player_index");
      return false;
    }
    events_.push_back(event);
    return true;
  }

  // Recomputes the canonical digest from seed and every event. This is an
  // intentionally small FNV-1a implementation so the result is identical on
  // MSVC and Linux without pulling a crypto dependency into the server.
  std::uint64_t digest() const { return digest_for(seed_, events_); }

  bool verify(std::uint64_t expected_seed,
              const std::vector<ReplayEvent>& expected_events,
              std::uint64_t expected_digest,
              std::string* error = nullptr) const {
    if (seed_ != expected_seed) {
      set_error(error, "seed_mismatch");
      return false;
    }
    if (events_ != expected_events) {
      set_error(error, "event_sequence_mismatch");
      return false;
    }
    if (digest() != expected_digest) {
      set_error(error, "digest_mismatch");
      return false;
    }
    return true;
  }

  static std::uint64_t digest_for(
      std::uint64_t seed, const std::vector<ReplayEvent>& events) {
    std::uint64_t hash = kFnvOffset;
    hash_u64(hash, seed);
    for (const auto& event : events) {
      hash_u64(hash, event.revision);
      hash_u64(hash, event.turn_id);
      hash_u64(hash, event.action_id);
      hash_u32(hash, static_cast<std::uint32_t>(event.player_index));
      hash_string(hash, event.type);
      hash_string(hash, event.payload);
    }
    return hash;
  }

 private:
  static constexpr std::uint64_t kFnvOffset = 14695981039346656037ull;
  static constexpr std::uint64_t kFnvPrime = 1099511628211ull;

  static void set_error(std::string* error, const char* value) {
    if (error != nullptr) *error = value;
  }

  static void hash_byte(std::uint64_t& hash, std::uint8_t value) {
    hash ^= value;
    hash *= kFnvPrime;
  }

  static void hash_u32(std::uint64_t& hash, std::uint32_t value) {
    for (int shift = 0; shift < 32; shift += 8) {
      hash_byte(hash, static_cast<std::uint8_t>(value >> shift));
    }
  }

  static void hash_u64(std::uint64_t& hash, std::uint64_t value) {
    for (int shift = 0; shift < 64; shift += 8) {
      hash_byte(hash, static_cast<std::uint8_t>(value >> shift));
    }
  }

  static void hash_string(std::uint64_t& hash, const std::string& value) {
    hash_u64(hash, static_cast<std::uint64_t>(value.size()));
    for (const unsigned char byte : value) hash_byte(hash, byte);
  }

  std::uint64_t seed_ = 0;
  std::vector<ReplayEvent> events_;
};

}  // namespace arena::battle
