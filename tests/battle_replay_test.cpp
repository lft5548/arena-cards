#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#include "server/battle/battle_replay.h"

using arena::battle::BattleReplay;
using arena::battle::ReplayEvent;

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) {
      std::cerr << "battle replay test failed: " << message << "\n";
      std::exit(1);
    }
  };
  BattleReplay replay(0x12345678u);
  std::string error;
  require(!replay.append(ReplayEvent{2, 1, 1, 0, "play", "card=1"}, &error), "revision rejection");
  require(error == "revision_must_be_contiguous", "revision error");

  require(replay.append(ReplayEvent{1, 1, 1, 0, "play", "card=1"}, &error), "first event");
  require(replay.append(ReplayEvent{2, 1, 0, 0, "effect", "damage=3"}, &error), "second event");
  require(replay.append(ReplayEvent{3, 2, 0, 0, "turn_end", ""}, &error), "third event");
  const std::uint64_t digest = replay.digest();
  require(digest != 0, "nonzero digest");

  BattleReplay same(0x12345678u);
  for (const auto& event : replay.events()) require(same.append(event, &error), "copy event");
  require(same.digest() == digest, "same digest");
  require(same.verify(replay.seed(), replay.events(), digest, &error), "verify same replay");

  BattleReplay different_seed(0x12345679u);
  for (const auto& event : replay.events()) require(different_seed.append(event), "different seed event");
  require(different_seed.digest() != digest, "different seed digest");
  require(!different_seed.verify(replay.seed(), replay.events(), digest, &error), "different seed rejection");
  require(error == "seed_mismatch", "seed error");

  std::vector<ReplayEvent> tampered = replay.events();
  tampered[1].payload = "damage=4";
  require(!replay.verify(replay.seed(), tampered, digest, &error), "tampered rejection");
  require(error == "event_sequence_mismatch", "tampered error");

  require(!replay.append(ReplayEvent{4, 2, 0, 2, "invalid", ""}, &error), "player rejection");
  require(error == "invalid_player_index", "player error");
  require(!replay.append(ReplayEvent{4, 2, 0, -1, "", ""}, &error), "event type rejection");
  require(error == "event_type_required", "event type error");
  return 0;
}
