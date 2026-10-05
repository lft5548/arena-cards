#pragma once

#include <algorithm>
#include <vector>

namespace arena::battle {

enum class StatusKind { Poison, Regen, Burn };

inline const char* status_name(StatusKind kind) {
  return kind == StatusKind::Poison ? "poison" : kind == StatusKind::Regen ? "regen" : "burn";
}

struct Status {
  int value = 0;
  int turns = 0;
};

struct StatusTick {
  StatusKind kind = StatusKind::Poison;
  int value = 0;
  int remaining = 0;
  int hp = 0;
};

class StatusEffects {
 public:
  Status poison;
  Status regen;
  Status burn;

  static bool valid_parameters(int value, int duration) {
    return value >= 1 && value <= 30 && duration >= 1 && duration <= 5;
  }

  bool apply(StatusKind kind, int value, int duration) {
    if (!valid_parameters(value, duration)) return false;
    auto& status = kind == StatusKind::Poison ? poison : kind == StatusKind::Regen ? regen : burn;
    status = {value, duration};
    return true;
  }

  std::vector<StatusTick> on_turn_start(int& hp) {
    std::vector<StatusTick> ticks;
    if (hp <= 0) return ticks;
    for (const auto kind : {StatusKind::Poison, StatusKind::Regen}) {
      auto& status = kind == StatusKind::Poison ? poison : regen;
      if (status.turns == 0) continue;
      const int value = status.value;
      hp = kind == StatusKind::Poison ? std::max(0, hp - value) :
          std::min(30, hp + value);
      --status.turns;
      ticks.push_back({kind, value, status.turns, hp});
      if (status.turns == 0) status.value = 0;
      if (hp == 0) break;
    }
    return ticks;
  }

  std::vector<StatusTick> on_turn_end(int& hp) {
    if (hp <= 0 || burn.turns == 0) return {};
    const int value = burn.value;
    hp = std::max(0, hp - value);
    --burn.turns;
    const StatusTick tick{StatusKind::Burn, value, burn.turns, hp};
    if (burn.turns == 0) burn.value = 0;
    return {tick};
  }
};

}
