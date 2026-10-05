#pragma once

#include <cstdint>

namespace arena::battle {

enum class CommandKind { PlayCard, EndTurn };

struct BattleCommand {
  CommandKind kind = CommandKind::EndTurn;
  int player = -1;
  std::uint64_t turn_id = 0;
  std::uint64_t action_id = 0;
  int card = 0;
};

}
