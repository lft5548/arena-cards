#include "server/battle/battle_engine.h"

#include <algorithm>
#include <stdexcept>
#include <utility>

#include "server/battle/card_effect.h"

namespace arena::battle {

BattleEngine::BattleEngine(std::shared_ptr<const config::CardCatalog> catalog) : catalog_(std::move(catalog)) {
  if (!catalog_ || !catalog_->find(1) || !catalog_->find(2) || !catalog_->find(3))
    throw std::invalid_argument("battle requires a validated starter catalog");
  state_.rng_state = 0x9e3779b97f4a7c15ull;
  for (auto& player : state_.players) {
    player.deck = catalog_->starter_deck();
    player.refill_deck = player.deck;
  }
}

void BattleEngine::set_rng_seed(std::uint64_t seed) {
  state_.rng_state = seed == 0 ? 0x9e3779b97f4a7c15ull : seed;
}

BattleSnapshot BattleEngine::snapshot(std::uint64_t revision) const {
  BattleSnapshot copy;
  copy.revision = revision;
  copy.state = state_;
  return copy;
}

bool BattleEngine::restore(const BattleSnapshot& snapshot, std::string* error) {
  if (snapshot.version != BattleSnapshot::kVersion) {
    if (error != nullptr) *error = "unsupported_snapshot_version";
    return false;
  }
  if (snapshot.state.turn < 0 || snapshot.state.turn > 1 || snapshot.state.turn_id == 0) {
    if (error != nullptr) *error = "invalid_snapshot_state";
    return false;
  }
  state_ = snapshot.state;
  if (state_.rng_state == 0) state_.rng_state = 0x9e3779b97f4a7c15ull;
  return true;
}

std::uint64_t BattleEngine::next_random() {
  if (state_.rng_state == 0) state_.rng_state = 0x9e3779b97f4a7c15ull;
  std::uint64_t value = state_.rng_state;
  value ^= value << 13;
  value ^= value >> 7;
  value ^= value << 17;
  state_.rng_state = value;
  return value;
}

void BattleEngine::shuffle_refill(std::vector<int>& deck) {
  for (std::size_t index = deck.size(); index > 1; --index) {
    const auto selected = static_cast<std::size_t>(next_random() % index);
    std::swap(deck[index - 1], deck[selected]);
  }
}

std::string BattleEngine::validate(const BattleCommand& command) const {
  if (state_.finished) return "battle_finished";
  if (command.player < 0 || command.player > 1) return "invalid_player";
  if (command.turn_id != state_.turn_id) return "stale_turn";
  if (command.player != state_.turn) return "not_your_turn";
  if (command.action_id == 0 || command.action_id <= state_.last_actions[command.player])
    return "duplicate_action";
  if (command.kind == CommandKind::EndTurn) return {};
  if (command.kind != CommandKind::PlayCard) return "invalid_command";
  const auto* card = catalog_->find(command.card);
  if (!card) return "unknown_card";
  const auto& player = state_.players[command.player];
  if (std::find(player.hand.begin(), player.hand.end(), command.card) == player.hand.end())
    return "card_not_in_hand";
  if (player.energy < card->cost) return "not_enough_energy";
  return {};
}

BattleOutcome BattleEngine::apply(const BattleCommand& command) {
  BattleOutcome outcome;
  outcome.error = validate(command);
  if (!outcome.error.empty()) return outcome;
  state_.last_actions[command.player] = command.action_id;
  BattleEvent event;
  event.player = command.player;
  event.turn_id = command.turn_id;
  event.action_id = command.action_id;
  if (command.kind == CommandKind::PlayCard) {
    const auto& card = *catalog_->find(command.card);
    auto& player = state_.players[command.player];
    auto& opponent = state_.players[1 - command.player];
    player.energy -= card.cost;
    player.hand.erase(std::find(player.hand.begin(), player.hand.end(), command.card));
    if (player.deck.empty()) {
      player.deck = player.refill_deck;
      shuffle_refill(player.deck);
    }
    if (!player.deck.empty()) {
      player.hand.push_back(player.deck.front());
      player.deck.erase(player.deck.begin());
    }
    const auto effects = apply_card_effect(player, opponent, card);
    event.kind = EventKind::Card;
    event.card = card.id;
    event.type = config::CardCatalog::effect_name(card.effect);
    event.value = card.value;
    event.duration = card.duration;
    event.target = 1 - command.player;
    event.count = effects.discarded_count;
    event.uses = card.duration;
    if (std::string(catalog_->rules_name()) == "bonus_v1" &&
        (card.effect == config::Effect::Damage || card.effect == config::Effect::Heal))
      event.bonus = effects.applied_bonus;
    outcome.events.push_back(std::move(event));
    if (opponent.hp <= 0) {
      conclude(outcome, command.player, "");
      return outcome;
    }
  } else {
    event.kind = EventKind::EndTurn;
    outcome.events.push_back(std::move(event));
  }
  advance_turn(outcome);
  return outcome;
}

void BattleEngine::conclude(BattleOutcome& outcome, int winner, const std::string& reason) {
  state_.finished = true;
  outcome.terminal = true;
  outcome.winner = winner;
  outcome.reason = reason;
}

void BattleEngine::append_ticks(BattleOutcome& outcome, const std::vector<StatusTick>& ticks, bool turn_end) {
  for (const auto& tick : ticks) {
    BattleEvent event;
    event.kind = EventKind::StatusTick;
    event.player = state_.turn;
    event.turn_id = state_.turn_id;
    event.status = tick.kind;
    event.turn_end = turn_end;
    event.value = tick.value;
    event.remaining = tick.remaining;
    event.hp = tick.hp;
    outcome.events.push_back(std::move(event));
  }
}

void BattleEngine::advance_turn(BattleOutcome& outcome) {
  auto& outgoing = state_.players[state_.turn];
  append_ticks(outcome, outgoing.statuses.on_turn_end(outgoing.hp), true);
  if (outgoing.hp <= 0) { conclude(outcome, 1 - state_.turn, "burn"); return; }
  state_.turn = 1 - state_.turn;
  ++state_.turn_id;
  if (state_.turn_id > 40) {
    const auto& players = state_.players;
    const int winner = players[0].hp == players[1].hp ? -1 : players[0].hp > players[1].hp ? 0 : 1;
    conclude(outcome, winner, "max_turns");
    return;
  }
  auto& player = state_.players[state_.turn];
  player.energy = 3;
  append_ticks(outcome, player.statuses.on_turn_start(player.hp), false);
  if (player.hp <= 0) conclude(outcome, 1 - state_.turn, "poison");
}

}
