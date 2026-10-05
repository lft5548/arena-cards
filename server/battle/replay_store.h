#pragma once

#include "battle_replay.h"

#include <chrono>
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <system_error>
#include <utility>

#ifdef _WIN32
#include <windows.h>
#endif

namespace arena::battle {

// ARENA_REPLAY_V1 is deliberately a text format: it is inspectable in an
// incident and still has a strict binary-compatible digest through
// BattleReplay. Type and payload are hex encoded so tabs and newlines cannot
// alter record boundaries.
struct ReplayDocument {
  std::string match_id;
  BattleReplay replay;
  std::uint64_t stored_digest = 0;
};

class ReplayStore {
 public:
  explicit ReplayStore(std::filesystem::path directory)
      : directory_(std::move(directory)) {}

  bool save(const std::string& match_id, const BattleReplay& replay,
            std::string* error = nullptr) const {
    return write(directory_, match_id, replay, error);
  }

  bool load(const std::string& match_id, ReplayDocument& document,
            std::string* error = nullptr) const {
    return read(directory_, match_id, document, error);
  }

  static std::filesystem::path path_for(const std::filesystem::path& directory,
                                         const std::string& match_id) {
    return directory / (match_id + ".replay");
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
                    const std::string& match_id, const BattleReplay& replay,
                    std::string* error = nullptr) {
    if (!valid_match_id(match_id)) return fail(error, "invalid_match_id");
    std::error_code ec;
    std::filesystem::create_directories(directory, ec);
    if (ec) return fail(error, "create_directory_failed");

    const auto target = path_for(directory, match_id);
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    static std::atomic<std::uint64_t> sequence{0};
    auto temp = target;
    temp += ".tmp-" + std::to_string(stamp) + "-" +
            std::to_string(sequence.fetch_add(1, std::memory_order_relaxed));
    {
      std::ofstream output(temp, std::ios::binary | std::ios::trunc);
      if (!output) return fail(error, "open_temp_failed");
      output << "ARENA_REPLAY_V1\n"
             << "match_id\t" << match_id << "\n"
             << "seed\t" << replay.seed() << "\n"
             << "digest\t" << replay.digest() << "\n"
             << "count\t" << replay.events().size() << "\n";
      for (const auto& event : replay.events()) {
        output << event.revision << '\t' << event.turn_id << '\t'
               << event.action_id << '\t' << event.player_index << '\t'
               << hex(event.type) << '\t' << hex(event.payload) << '\n';
      }
      output.flush();
      if (!output) {
        std::error_code ignored;
        std::filesystem::remove(temp, ignored);
        return fail(error, "write_temp_failed");
      }
    }
    if (!atomic_replace(temp, target)) {
      std::error_code ignored;
      std::filesystem::remove(temp, ignored);
      return fail(error, "rename_temp_failed");
    }
    return true;
  }

  static bool read(const std::filesystem::path& directory,
                   const std::string& match_id, ReplayDocument& document,
                   std::string* error = nullptr) {
    if (!valid_match_id(match_id)) return fail(error, "invalid_match_id");
    std::ifstream input(path_for(directory, match_id), std::ios::binary);
    if (!input) return fail(error, "open_replay_failed");

    std::string line;
    if (!std::getline(input, line) || line != "ARENA_REPLAY_V1")
      return fail(error, "invalid_magic");
    std::string key, value;
    if (!read_header(input, "match_id", key, value) || value != match_id)
      return fail(error, "match_id_mismatch");
    std::uint64_t seed = 0, digest = 0, count = 0;
    if (!read_number_header(input, "seed", seed) ||
        !read_number_header(input, "digest", digest) ||
        !read_number_header(input, "count", count) || count > 1000000)
      return fail(error, "invalid_header");

    ReplayDocument parsed;
    parsed.match_id = match_id;
    parsed.replay = BattleReplay(seed);
    parsed.stored_digest = digest;
    for (std::uint64_t index = 0; index < count; ++index) {
      if (!std::getline(input, line)) return fail(error, "truncated_events");
      std::vector<std::string> fields;
      split_tabs(line, fields);
      if (fields.size() != 6) return fail(error, "invalid_event_fields");
      ReplayEvent event;
      if (!number(fields[0], event.revision) || !number(fields[1], event.turn_id) ||
          !number(fields[2], event.action_id) || !number(fields[3], event.player_index) ||
          !unhex(fields[4], event.type) || !unhex(fields[5], event.payload))
        return fail(error, "invalid_event_value");
      std::string append_error;
      if (!parsed.replay.append(event, &append_error))
        return fail(error, append_error.c_str());
    }
    if (std::getline(input, line)) return fail(error, "trailing_data");
    if (parsed.replay.digest() != parsed.stored_digest)
      return fail(error, "digest_mismatch");
    document = std::move(parsed);
    return true;
  }

 private:
  static bool fail(std::string* error, const char* value) {
    if (error != nullptr) *error = value;
    return false;
  }

  static std::string hex(const std::string& value) {
    static constexpr char digits[] = "0123456789abcdef";
    std::string output;
    output.reserve(value.size() * 2);
    for (const unsigned char byte : value) {
      output.push_back(digits[byte >> 4]);
      output.push_back(digits[byte & 0x0f]);
    }
    return output;
  }

  static int hex_digit(char value) {
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
    return -1;
  }

  static bool unhex(const std::string& value, std::string& output) {
    if (value.size() % 2 != 0) return false;
    output.clear();
    output.reserve(value.size() / 2);
    for (size_t i = 0; i < value.size(); i += 2) {
      const int high = hex_digit(value[i]);
      const int low = hex_digit(value[i + 1]);
      if (high < 0 || low < 0) return false;
      output.push_back(static_cast<char>((high << 4) | low));
    }
    return true;
  }

  static void split_tabs(const std::string& line,
                         std::vector<std::string>& fields) {
    size_t start = 0;
    while (true) {
      const size_t end = line.find('\t', start);
      fields.push_back(line.substr(start, end == std::string::npos
                                            ? std::string::npos
                                            : end - start));
      if (end == std::string::npos) return;
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
    const size_t split = line.find('\t');
    if (split == std::string::npos || line.find('\t', split + 1) != std::string::npos)
      return false;
    key = line.substr(0, split);
    value = line.substr(split + 1);
    return key == expected && !value.empty();
  }

  static bool read_number_header(std::ifstream& input, const char* expected,
                                 std::uint64_t& value) {
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
