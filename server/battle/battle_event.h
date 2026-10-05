#pragma once

#include <cstdint>
#include <optional>
#include <string>

#include "server/battle/status_effects.h"

namespace arena::battle {

enum class EventKind { Card, EndTurn, StatusTick };

struct BattleEvent {
  EventKind kind = EventKind::EndTurn;
  int player = -1;
  std::uint64_t turn_id = 0;
  std::uint64_t action_id = 0;
  std::string type;
  int card = 0;
  int value = 0;
  int duration = 0;
  int target = -1;
  int count = 0;
  int uses = 0;
  std::optional<int> bonus;
  StatusKind status = StatusKind::Poison;
  bool turn_end = false;
  int remaining = 0;
  int hp = 0;

  std::string payload(const std::string& match_id) const;
};

}
