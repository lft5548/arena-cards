#pragma once

#include <cstdint>

#include "server/battle/battle_state.h"

namespace arena::battle {

// A versioned point-in-time copy of the authoritative room state. It is used
// for diagnostics and offline reconstruction; loading it does not resume a
// live room or bypass the Room actor.
struct BattleSnapshot {
  static constexpr std::uint32_t kVersion = 1;

  std::uint32_t version = kVersion;
  std::uint64_t revision = 0;
  BattleState state;
};

}  // namespace arena::battle
