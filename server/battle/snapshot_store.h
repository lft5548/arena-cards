#pragma once

#include "battle_snapshot.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

namespace arena::battle {

// Periodic snapshots intentionally use a separate versioned file. This keeps
// ARENA_REPLAY_V1 byte-for-byte compatible while allowing a snapshot schema to
// evolve independently. A snapshot is a recovery/reconstruction artifact; it
// is not a live-room crash resume protocol.
struct SnapshotDocument {
  std::string match_id;
  BattleSnapshot snapshot;
};

class SnapshotStore {
 public:
  explicit SnapshotStore(std::filesystem::path directory)
      : directory_(std::move(directory)) {}

  bool save(const std::string& match_id, const BattleSnapshot& snapshot,
            std::string* error = nullptr) const {
    return write(directory_, match_id, snapshot, error);
  }

  bool load(const std::string& match_id, SnapshotDocument& document,
            std::string* error = nullptr) const {
    return read(directory_, match_id, document, error);
  }

  static std::filesystem::path path_for(const std::filesystem::path& directory,
                                         const std::string& match_id) {
    return directory / (match_id + ".snapshot");
  }

  static bool valid_match_id(const std::string& value) {
    if (value.empty() || value.size() > 128 || value == "." || value == "..")
      return false;
    for (const unsigned char ch : value) {
      if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
          (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '.')
        continue;
      return false;
    }
    return true;
  }

  static bool write(const std::filesystem::path& directory,
                    const std::string& match_id, const BattleSnapshot& snapshot,
                    std::string* error = nullptr) {
    if (!valid_match_id(match_id)) return fail(error, "invalid_match_id");
    if (snapshot.version != BattleSnapshot::kVersion)
      return fail(error, "unsupported_snapshot_version");
    std::error_code ec;
    std::filesystem::create_directories(directory, ec);
    if (ec) return fail(error, "create_directory_failed");

    const auto target = path_for(directory, match_id);
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    static std::atomic<std::uint64_t> sequence{0};
    auto temporary = target;
    temporary += ".tmp-" + std::to_string(stamp) + "-" +
                 std::to_string(sequence.fetch_add(1, std::memory_order_relaxed));
    {
      std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
      if (!output) return fail(error, "open_temp_failed");
      output << "ARENA_SNAPSHOT_V1\n"
             << "match_id\t" << match_id << "\n"
             << "version\t" << snapshot.version << "\n"
             << "revision\t" << snapshot.revision << "\n"
             << "turn_id\t" << snapshot.state.turn_id << "\n"
             << "turn\t" << snapshot.state.turn << "\n"
             << "finished\t" << (snapshot.state.finished ? 1 : 0) << "\n"
             << "rng_state\t" << snapshot.state.rng_state << "\n";
      for (std::size_t index = 0; index < snapshot.state.players.size(); ++index)
        write_player(output, index, snapshot.state.players[index],
                     snapshot.state.last_actions[index]);
      output.flush();
      if (!output) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        return fail(error, "write_temp_failed");
      }
    }
    if (!atomic_replace(temporary, target)) {
      std::error_code ignored;
      std::filesystem::remove(temporary, ignored);
      return fail(error, "rename_temp_failed");
    }
    return true;
  }

  static bool read(const std::filesystem::path& directory,
                   const std::string& match_id, SnapshotDocument& document,
                   std::string* error = nullptr) {
    if (!valid_match_id(match_id)) return fail(error, "invalid_match_id");
    std::ifstream input(path_for(directory, match_id), std::ios::binary);
    if (!input) return fail(error, "open_snapshot_failed");

    std::string line;
    if (!std::getline(input, line) || line != "ARENA_SNAPSHOT_V1")
      return fail(error, "invalid_magic");
    std::string key, value;
    if (!read_header(input, "match_id", key, value) || value != match_id)
      return fail(error, "match_id_mismatch");
    std::uint64_t version = 0;
    std::uint64_t revision = 0;
    std::uint64_t turn_id = 0;
    int turn = 0;
    std::uint64_t finished = 0;
    std::uint64_t rng_state = 0;
    if (!read_number_header(input, "version", version) ||
        !read_number_header(input, "revision", revision) ||
        !read_number_header(input, "turn_id", turn_id) ||
        !read_number_header(input, "turn", turn) ||
        !read_number_header(input, "finished", finished) ||
        !read_number_header(input, "rng_state", rng_state))
      return fail(error, "invalid_header");
    if (version != BattleSnapshot::kVersion || turn_id == 0 || turn < 0 || turn > 1 || finished > 1)
      return fail(error, "invalid_snapshot_state");

    SnapshotDocument parsed;
    parsed.match_id = match_id;
    parsed.snapshot.version = static_cast<std::uint32_t>(version);
    parsed.snapshot.revision = revision;
    parsed.snapshot.state.turn_id = turn_id;
    parsed.snapshot.state.turn = turn;
    parsed.snapshot.state.finished = finished != 0;
    parsed.snapshot.state.rng_state = rng_state == 0 ? 0x9e3779b97f4a7c15ull : rng_state;
    for (std::size_t expected = 0; expected < parsed.snapshot.state.players.size(); ++expected) {
      if (!std::getline(input, line)) return fail(error, "truncated_players");
      std::vector<std::string> fields;
      split_tabs(line, fields);
      if (fields.size() != 20) return fail(error, "invalid_player_fields");
      std::uint64_t index = 0;
      if (!number(fields[0], index) || index != expected ||
          !number(fields[1], parsed.snapshot.state.players[index].hp) ||
          !number(fields[2], parsed.snapshot.state.players[index].energy) ||
          !number(fields[3], parsed.snapshot.state.players[index].shield) ||
          !number(fields[4], parsed.snapshot.state.last_actions[index]) ||
          !number(fields[5], parsed.snapshot.state.players[index].attack_boost.value) ||
          !number(fields[6], parsed.snapshot.state.players[index].attack_boost.uses) ||
          !number(fields[7], parsed.snapshot.state.players[index].heal_boost.value) ||
          !number(fields[8], parsed.snapshot.state.players[index].heal_boost.uses) ||
          !number(fields[9], parsed.snapshot.state.players[index].statuses.poison.value) ||
          !number(fields[10], parsed.snapshot.state.players[index].statuses.poison.turns) ||
          !number(fields[11], parsed.snapshot.state.players[index].statuses.regen.value) ||
          !number(fields[12], parsed.snapshot.state.players[index].statuses.regen.turns) ||
          !number(fields[13], parsed.snapshot.state.players[index].statuses.burn.value) ||
          !number(fields[14], parsed.snapshot.state.players[index].statuses.burn.turns) ||
          !parse_list(fields[15], parsed.snapshot.state.players[index].hand) ||
          !parse_list(fields[16], parsed.snapshot.state.players[index].deck) ||
          !parse_list(fields[17], parsed.snapshot.state.players[index].refill_deck) ||
          !parse_list(fields[18], parsed.snapshot.state.players[index].discard))
        return fail(error, "invalid_player_value");
      // Field 19 is a reserved checksum slot for forward-compatible writers.
      if (fields[19] != "-") return fail(error, "invalid_reserved_field");
    }
    if (std::getline(input, line)) return fail(error, "trailing_data");
    document = std::move(parsed);
    return true;
  }

 private:
  static bool fail(std::string* error, const char* value) {
    if (error != nullptr) *error = value;
    return false;
  }

  static std::string list(const std::vector<int>& values) {
    if (values.empty()) return "-";
    std::ostringstream output;
    for (std::size_t index = 0; index < values.size(); ++index) {
      if (index > 0) output << ',';
      output << values[index];
    }
    return output.str();
  }

  static void write_player(std::ofstream& output, std::size_t index,
                           const PlayerState& player, std::uint64_t last_action) {
    output << index << '\t' << player.hp << '\t' << player.energy << '\t'
           << player.shield << '\t' << last_action << '\t'
           << player.attack_boost.value << '\t' << player.attack_boost.uses << '\t'
           << player.heal_boost.value << '\t' << player.heal_boost.uses << '\t'
           << player.statuses.poison.value << '\t' << player.statuses.poison.turns << '\t'
           << player.statuses.regen.value << '\t' << player.statuses.regen.turns << '\t'
           << player.statuses.burn.value << '\t' << player.statuses.burn.turns << '\t'
           << list(player.hand) << '\t' << list(player.deck) << '\t'
           << list(player.refill_deck) << '\t' << list(player.discard) << "\t-\n";
  }

  static void split_tabs(const std::string& line,
                         std::vector<std::string>& fields) {
    std::size_t start = 0;
    while (true) {
      const std::size_t end = line.find('\t', start);
      fields.push_back(line.substr(start, end == std::string::npos ?
                                            std::string::npos : end - start));
      if (end == std::string::npos) return;
      start = end + 1;
    }
  }

  static bool parse_list(const std::string& raw, std::vector<int>& values) {
    values.clear();
    if (raw == "-") return true;
    if (raw.empty()) return false;
    std::size_t start = 0;
    while (true) {
      const std::size_t end = raw.find(',', start);
      const std::string item = raw.substr(start, end == std::string::npos ?
                                                     std::string::npos : end - start);
      int value = 0;
      if (!number(item, value)) return false;
      values.push_back(value);
      if (end == std::string::npos) return true;
      start = end + 1;
    }
  }

  template <typename T>
  static bool number(const std::string& value, T& output) {
    if (value.empty()) return false;
    std::istringstream stream(value);
    stream >> output;
    return stream.eof() && !stream.fail();
  }

  static bool read_header(std::ifstream& input, const char* expected,
                          std::string& key, std::string& value) {
    std::string line;
    if (!std::getline(input, line)) return false;
    const std::size_t split = line.find('\t');
    if (split == std::string::npos || line.find('\t', split + 1) != std::string::npos)
      return false;
    key = line.substr(0, split);
    value = line.substr(split + 1);
    return key == expected && !value.empty();
  }

  template <typename T>
  static bool read_number_header(std::ifstream& input, const char* expected, T& value) {
    std::string key, raw;
    return read_header(input, expected, key, raw) && number(raw, value);
  }

  static bool atomic_replace(const std::filesystem::path& temporary,
                             const std::filesystem::path& target) {
#ifdef _WIN32
    return MoveFileExW(temporary.c_str(), target.c_str(),
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
#else
    return std::rename(temporary.c_str(), target.c_str()) == 0;
#endif
  }

  std::filesystem::path directory_;
};

}  // namespace arena::battle
