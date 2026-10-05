#include <cstdlib>
#include <iostream>

#include "server/battle/status_effects.h"

using arena::battle::StatusEffects;
using arena::battle::StatusKind;

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) {
      std::cerr << "status effects test failed: " << message << "\n";
      std::exit(1);
    }
  };
  StatusEffects effects;
  int hp = 30;
  require(effects.apply(StatusKind::Poison, 4, 2), "apply poison");
  auto ticks = effects.on_turn_start(hp);
  require(ticks.size() == 1 && hp == 26 && ticks[0].remaining == 1, "first poison tick");
  ticks = effects.on_turn_start(hp);
  require(ticks.size() == 1 && hp == 22 && ticks[0].remaining == 0, "last poison tick");
  require(effects.poison.value == 0 && effects.poison.turns == 0, "expired poison cleared");
  require(effects.on_turn_start(hp).empty() && hp == 22, "no extra tick");

  require(effects.apply(StatusKind::Poison, 9, 5), "strong poison");
  require(effects.apply(StatusKind::Poison, 2, 1), "replace poison");
  require(effects.poison.value == 2 && effects.poison.turns == 1, "last application replaces");
  require(!effects.apply(StatusKind::Poison, 0, 2), "zero value rejected");
  require(!effects.apply(StatusKind::Poison, 31, 2), "excess value rejected");
  require(!effects.apply(StatusKind::Poison, 2, 0), "zero duration rejected");
  require(!effects.apply(StatusKind::Poison, 2, 6), "excess duration rejected");
  require(!effects.apply(StatusKind::Regen, -1, 2), "negative value rejected");
  require(!effects.apply(StatusKind::Regen, 2, -1), "negative duration rejected");
  require(effects.poison.value == 2 && effects.poison.turns == 1, "rejection preserves state");
  effects.on_turn_start(hp);

  hp = 29;
  require(effects.apply(StatusKind::Regen, 4, 2), "apply regen");
  ticks = effects.on_turn_start(hp);
  require(ticks.size() == 1 && hp == 30 && ticks[0].remaining == 1, "regen capped");
  ticks = effects.on_turn_start(hp);
  require(ticks.size() == 1 && hp == 30 && ticks[0].remaining == 0, "full hp consumes tick");
  require(effects.regen.value == 0 && effects.regen.turns == 0, "expired regen cleared");

  hp = 8;
  require(effects.apply(StatusKind::Poison, 3, 1), "ordered poison");
  require(effects.apply(StatusKind::Regen, 5, 1), "ordered regen");
  ticks = effects.on_turn_start(hp);
  require(ticks.size() == 2 && ticks[0].kind == StatusKind::Poison &&
          ticks[0].hp == 5 && ticks[1].kind == StatusKind::Regen && hp == 10,
          "poison before regen");

  hp = 2;
  require(effects.apply(StatusKind::Poison, 3, 2), "lethal poison");
  require(effects.apply(StatusKind::Regen, 30, 5), "pending regen");
  ticks = effects.on_turn_start(hp);
  require(ticks.size() == 1 && hp == 0, "lethal poison stops regen");
  require(effects.regen.turns == 5 && effects.regen.value == 30, "untriggered regen preserved");
  require(effects.on_turn_start(hp).empty(), "dead player cannot trigger");

  StatusEffects bounded;
  hp = 30;
  require(bounded.apply(StatusKind::Poison, 1, 5), "maximum duration");
  for (int turn = 0; turn < 5; ++turn) {
    require(bounded.on_turn_start(hp).size() == 1, "bounded tick");
  }
  require(hp == 25 && bounded.on_turn_start(hp).empty(), "finite duration");

  StatusEffects ending;
  hp = 30;
  require(ending.apply(StatusKind::Burn, 4, 2), "apply burn");
  require(ending.on_turn_start(hp).empty() && hp == 30 && ending.burn.turns == 2,
          "burn does not trigger on start");
  ticks = ending.on_turn_end(hp);
  require(ticks.size() == 1 && ticks[0].kind == StatusKind::Burn && hp == 26 &&
          ticks[0].remaining == 1 && ending.burn.value == 4, "first end tick");
  require(ending.apply(StatusKind::Burn, 2, 1), "replace burn");
  ticks = ending.on_turn_end(hp);
  require(ticks.size() == 1 && hp == 24 && ticks[0].value == 2 && ticks[0].remaining == 0 &&
          ending.burn.value == 0 && ending.burn.turns == 0, "replacement and expiry");
  require(ending.on_turn_end(hp).empty() && hp == 24, "no extra end tick");
  require(ending.apply(StatusKind::Burn, 30, 5), "maximum burn");
  require(!ending.apply(StatusKind::Burn, 31, 5) && !ending.apply(StatusKind::Burn, 1, 6),
          "reject invalid burn without replacement");
  ticks = ending.on_turn_end(hp);
  require(ticks.size() == 1 && hp == 0 && ending.burn.turns == 4, "burn clamps lethal hp");
  require(ending.on_turn_end(hp).empty() && ending.burn.turns == 4, "dead player cannot end-trigger");
  return 0;
}
