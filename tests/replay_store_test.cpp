#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#include "server/battle/replay_store.h"

using arena::battle::BattleReplay;
using arena::battle::ReplayEvent;
using arena::battle::ReplayStore;

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) {
      std::cerr << "replay store test failed: " << message << "\n";
      std::exit(1);
    }
  };

  const auto directory = std::filesystem::temp_directory_path() /
      "arena-cards-replay-store-test";
  std::error_code ignored;
  std::filesystem::remove_all(directory, ignored);

  BattleReplay replay(0x12345678u);
  std::string error;
  BattleReplay cross_language(0x12345678u);
  require(cross_language.append(ReplayEvent{1, 2, 3, -1, "battle_event",
                                           "type=damage;value=8"}, &error),
          "append cross-language digest vector");
  require(cross_language.digest() == 14449955194524173080ull,
          "digest matches Python replay tool format");

  require(replay.append(ReplayEvent{1, 1, 0, -1, "match_start", "match_id=test-match"}, &error),
          "append start");
  require(replay.append(ReplayEvent{2, 1, 3, 0, "battle_event",
                                    "match_id=test-match;type=damage;value=8"}, &error),
          "append action");
  require(replay.append(ReplayEvent{3, 1, 0, -1, "match_result",
                                    "match_id=test-match;winner=0;turn_id=1"}, &error),
          "append result");
  ReplayStore store(directory);
  require(store.save("test-match", replay, &error), "atomic save");
  require(std::filesystem::exists(ReplayStore::path_for(directory, "test-match")),
          "saved target exists");

  arena::battle::ReplayDocument loaded;
  require(store.load("test-match", loaded, &error), "load and verify");
  require(loaded.match_id == "test-match", "match id round trip");
  require(loaded.replay.seed() == replay.seed(), "seed round trip");
  require(loaded.replay.digest() == replay.digest(), "digest round trip");
  require(loaded.replay.events() == replay.events(), "events round trip");

  BattleReplay replacement = replay;
  require(replacement.append(ReplayEvent{4, 2, 1, 1, "battle_event",
                                         "match_id=test-match;type=end_turn"}, &error),
          "append replacement event");
  require(store.save("test-match", replacement, &error), "replace existing replay atomically");
  require(store.load("test-match", loaded, &error), "load replacement replay");
  require(loaded.replay.digest() == replacement.digest(), "replacement digest round trip");
  require(loaded.replay.events() == replacement.events(), "replacement events round trip");

  require(!store.save("../escape", replay, &error), "reject traversal id");
  require(error == "invalid_match_id", "traversal error");

  {
    std::ofstream corrupt(ReplayStore::path_for(directory, "test-match"),
                          std::ios::binary | std::ios::trunc);
    corrupt << "ARENA_REPLAY_V1\nmatch_id\ttest-match\nseed\t1\n"
            << "digest\t2\ncount\t1\n1\t1\t0\t-1\t78\t79\n";
  }
  require(!store.load("test-match", loaded, &error), "reject bad digest");
  require(error == "digest_mismatch", "bad digest error");

  std::filesystem::remove_all(directory, ignored);
  return 0;
}
