#pragma once

#include "server/battle/battle_state.h"
#include "server/config/card_catalog.h"

namespace arena::battle {

struct CardEffectResult {
  int applied_bonus = 0;
  int discarded_count = 0;
};

CardEffectResult apply_card_effect(PlayerState& player, PlayerState& opponent,
                                  const config::CardDefinition& card);

}
