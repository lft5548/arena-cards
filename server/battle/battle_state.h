#pragma once

#include <vector>
#include <array>
#include <cstdint>

#include "server/battle/status_effects.h"
#include "server/battle/bonus_effects.h"

namespace arena::battle {

struct PlayerState {
  int hp = 30;
  int energy = 3;
  int shield = 0;
  std::vector<int> hand{1, 2, 3};
  std::vector<int> deck;
  std::vector<int> refill_deck;
  std::vector<int> discard;
  StatusEffects statuses;
  Bonus attack_boost;
  Bonus heal_boost;
};

struct BattleState {
  std::array<PlayerState, 2> players;
  int turn = 0;
  std::uint64_t turn_id = 1;
  std::array<std::uint64_t, 2> last_actions{0, 0};
  bool finished = false;
  // XorShift64 state captured by periodic snapshots. A zero state is never used.
  std::uint64_t rng_state = 0;
};

}
