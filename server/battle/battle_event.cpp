#include "server/battle/battle_event.h"

#include <sstream>

namespace arena::battle {

std::string BattleEvent::payload(const std::string& match_id) const {
  std::ostringstream event;
  event << "match_id=" << match_id << ";turn_id=" << turn_id << ";player=" << player
        << ";action_id=" << action_id;
  if (kind == EventKind::EndTurn) {
    event << ";type=end_turn";
  } else if (kind == EventKind::StatusTick) {
    event << ";type=status_tick";
    if (turn_end) event << ";phase=end";
    event << ";status=" << status_name(status) << ";value=" << value
          << ";remaining=" << remaining << ";hp=" << hp;
  } else {
    event << ";card=" << card << ";type=" << type << ";value=" << value;
    if (type == "poison" || type == "regen" || type == "burn") event << ";duration=" << duration;
    if (type == "discard") event << ";target=" << target << ";count=" << count;
    if (type == "attack_boost" || type == "heal_boost") event << ";uses=" << uses;
    if (bonus) event << ";bonus=" << *bonus;
  }
  return event.str();
}

}
