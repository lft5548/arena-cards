#include "server/battle/card_effect.h"

#include <algorithm>

#include "server/battle/discard_effects.h"

namespace arena::battle {

CardEffectResult apply_card_effect(PlayerState& player, PlayerState& opponent,
                                  const config::CardDefinition& card) {
  CardEffectResult result;
  switch (card.effect) {
    case config::Effect::Damage: {
      result.applied_bonus = player.attack_boost.consume();
      const int damage = card.value + result.applied_bonus;
      const int blocked = std::min(damage, opponent.shield);
      opponent.shield -= blocked;
      opponent.hp -= damage - blocked;
      break;
    }
    case config::Effect::Heal:
      result.applied_bonus = player.heal_boost.consume();
      player.hp = std::min(30, player.hp + card.value + result.applied_bonus);
      break;
    case config::Effect::Shield:
      player.shield = std::min(20, player.shield + card.value);
      break;
    case config::Effect::Draw:
      for (int count = 0; count < card.value && !player.deck.empty() && player.hand.size() < 10; ++count) {
        player.hand.push_back(player.deck.front());
        player.deck.erase(player.deck.begin());
      }
      break;
    case config::Effect::Poison:
      opponent.statuses.apply(StatusKind::Poison, card.value, card.duration);
      break;
    case config::Effect::Regen:
      player.statuses.apply(StatusKind::Regen, card.value, card.duration);
      break;
    case config::Effect::Discard:
      result.discarded_count = discard_front(opponent.hand, opponent.discard, card.value);
      break;
    case config::Effect::Burn:
      opponent.statuses.apply(StatusKind::Burn, card.value, card.duration);
      break;
    case config::Effect::AttackBoost:
      player.attack_boost.apply(card.value, card.duration);
      break;
    case config::Effect::HealBoost:
      player.heal_boost.apply(card.value, card.duration);
      break;
  }
  return result;
}

}
