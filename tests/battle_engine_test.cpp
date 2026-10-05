#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>

#include "server/battle/battle_engine.h"
#include "server/battle/battle_replay.h"

namespace {

using arena::battle::BattleCommand;
using arena::battle::BattleEngine;
using arena::battle::BattleOutcome;
using arena::battle::CommandKind;

void require(bool condition, const char* message) {
  if (!condition) { std::cerr << "battle engine test failed: " << message << "\n"; std::exit(1); }
}

class Catalogs {
 public:
  Catalogs() {
    directory_ = std::filesystem::temp_directory_path() /
        ("arena-engine-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    std::filesystem::create_directories(directory_);
  }
  ~Catalogs() { std::error_code ignored; std::filesystem::remove_all(directory_, ignored); }
  std::shared_ptr<const arena::config::CardCatalog> load(const std::string& rows) {
    const auto path = directory_ / (std::to_string(++sequence_) + ".csv");
    {
      std::ofstream stream(path);
      stream << "id,name,cost,effect,value,duration\n" << rows;
    }
    auto catalog = std::make_shared<arena::config::CardCatalog>();
    std::string error;
    require(catalog->load_csv(path.string(), error), "test catalog validation");
    return catalog;
  }

 private:
  std::filesystem::path directory_;
  int sequence_ = 0;
};

std::string fingerprint(const arena::battle::BattleState& state) {
  std::ostringstream output;
  output << state.turn << "," << state.turn_id << "," << state.finished;
  for (std::size_t index = 0; index < state.players.size(); ++index) {
    const auto& player = state.players[index];
    output << ";" << state.last_actions[index] << "," << player.hp << "," << player.energy << "," << player.shield;
    for (const auto* cards : {&player.hand, &player.deck, &player.refill_deck, &player.discard}) {
      output << ";";
      for (const int card : *cards) output << card << ",";
    }
    for (const auto* status : {&player.statuses.poison, &player.statuses.regen, &player.statuses.burn})
      output << ";" << status->value << "," << status->turns;
    for (const auto* bonus : {&player.attack_boost, &player.heal_boost})
      output << ";" << bonus->value << "," << bonus->uses;
  }
  return output.str();
}

BattleOutcome action(BattleEngine& engine, int card = 0) {
  const auto& state = engine.state();
  const auto outcome = engine.apply({card ? CommandKind::PlayCard : CommandKind::EndTurn,
      state.turn, state.turn_id, state.last_actions[state.turn] + 1, card});
  require(outcome.error.empty(), "legal test action rejected");
  return outcome;
}

void rejects_without_mutation(BattleEngine& engine, const BattleCommand& command, const std::string& error) {
  const auto before = fingerprint(engine.state());
  const auto outcome = engine.apply(command);
  require(outcome.error == error && outcome.events.empty() && !outcome.terminal, "expected rejection only");
  require(fingerprint(engine.state()) == before, "invalid command changed complete battle state");
}

void validation_and_determinism(Catalogs& catalogs) {
  auto catalog = catalogs.load("1,Strike,2,damage,8,0\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n");
  BattleEngine engine(catalog);
  require(engine.state().turn == 0 && engine.state().turn_id == 1 && !engine.state().finished &&
          engine.state().players[0].hand == std::vector<int>({1, 2, 3}) &&
          engine.state().players[1].deck == std::vector<int>({1, 2, 3, 1, 2, 3, 1, 2, 3}), "deterministic initial state");
  rejects_without_mutation(engine, {CommandKind::PlayCard, -1, 1, 1, 1}, "invalid_player");
  rejects_without_mutation(engine, {CommandKind::PlayCard, 0, 2, 1, 1}, "stale_turn");
  rejects_without_mutation(engine, {CommandKind::PlayCard, 1, 1, 1, 1}, "not_your_turn");
  rejects_without_mutation(engine, {CommandKind::PlayCard, 0, 1, 0, 1}, "duplicate_action");
  rejects_without_mutation(engine, {CommandKind::PlayCard, 0, 1, 1, 999}, "unknown_card");
  rejects_without_mutation(engine, {static_cast<CommandKind>(99), 0, 1, 1, 1}, "invalid_command");
  const auto first = action(engine, 1);
  require(first.events.size() == 1 && first.events[0].payload("match-golden") ==
      "match_id=match-golden;turn_id=1;player=0;action_id=1;card=1;type=damage;value=8", "old card payload unchanged");
  require(engine.state().players[0].energy == 1 && engine.state().players[1].hp == 22 &&
          engine.state().players[0].hand == std::vector<int>({2, 3, 1}), "cost/refill/direct damage");
  const auto ended = action(engine);
  require(ended.events[0].payload("match-golden") ==
      "match_id=match-golden;turn_id=2;player=1;action_id=1;type=end_turn", "old EndTurn payload unchanged");
  rejects_without_mutation(engine, {CommandKind::EndTurn, 0, 3, 1, 0}, "duplicate_action");

  BattleEngine copy(catalog);
  action(copy, 1);
  action(copy);
  require(fingerprint(copy.state()) == fingerprint(engine.state()), "same commands yield exact same state");
  auto expensive = catalogs.load("1,Heavy,4,damage,8,0\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n");
  BattleEngine energy(expensive);
  rejects_without_mutation(energy, {CommandKind::PlayCard, 0, 1, 1, 1}, "not_enough_energy");
  auto extra = catalogs.load("1,Strike,2,damage,8,0\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n9,Disrupt,2,discard,2,0\n");
  BattleEngine hand(extra);
  rejects_without_mutation(hand, {CommandKind::PlayCard, 0, 1, 1, 9}, "card_not_in_hand");
  bool missing_catalog = false;
  try { BattleEngine invalid(nullptr); } catch (const std::invalid_argument&) { missing_catalog = true; }
  require(missing_catalog, "unvalidated catalog rejected");
}

void finite_effects(Catalogs& catalogs) {
  BattleEngine shield(catalogs.load("1,Focus,1,attack_boost,3,1\n2,Tap,1,damage,0,0\n3,Barrier,1,shield,6,0\n"));
  action(shield, 1);
  action(shield, 3);
  const auto boosted = action(shield, 2);
  require(boosted.events[0].payload("bonus-match") ==
      "match_id=bonus-match;turn_id=3;player=0;action_id=2;card=2;type=damage;value=0;bonus=3", "bonus field order");
  require(shield.state().players[1].hp == 30 && shield.state().players[1].shield == 3 &&
          shield.state().players[0].attack_boost.uses == 0, "shield absorbs combined damage and consumes charge");
  action(shield);
  action(shield, 3);
  action(shield);
  require(action(shield, 2).events[0].bonus == 0, "zero bonus explicitly present after expiry");

  BattleEngine heal(catalogs.load("1,Bless,1,heal_boost,10,1\n2,Mend,1,heal,4,0\n3,Strike,1,damage,6,0\n"));
  action(heal, 1);
  action(heal, 3);
  require(action(heal, 2).events[0].bonus == 10 && heal.state().players[0].hp == 30 &&
          heal.state().players[0].heal_boost.uses == 0, "enhanced heal caps and consumes");
  action(heal);
  action(heal, 1);
  action(heal);
  action(heal, 2);
  require(heal.state().players[0].hp == 30 && heal.state().players[0].heal_boost.uses == 0,
          "full hp legal heal consumes charge");

  BattleEngine discard(catalogs.load("1,Disrupt,2,discard,2,0\n2,Cycle,1,draw,0,0\n3,Barrier,1,shield,0,0\n"));
  require(action(discard, 1).events[0].count == 2, "discard actual count");
  action(discard);
  require(action(discard, 1).events[0].count == 1 && discard.state().players[1].hand.empty() &&
          discard.state().players[1].discard == std::vector<int>({1, 2, 3}), "short hand ordered history");
  action(discard);
  action(discard, 2);
  action(discard);
  action(discard, 3);
  action(discard);
  require(action(discard, 1).events[0].count == 0 && discard.state().players[1].deck.size() == 9,
          "empty target preserves opposing deck");

  BattleEngine draw(catalogs.load("1,Insight,1,draw,1000,0\n2,Barrier,1,shield,6,0\n3,Mend,1,heal,4,0\n"));
  action(draw, 1);
  require(draw.state().players[0].hand.size() == 10 && draw.state().players[0].deck.size() == 1,
          "draw respects hand cap and remaining deck");
  action(draw);
  action(draw, 1);
  require(draw.state().players[0].deck.empty(), "effect draw does not refill empty deck");
  action(draw);
  action(draw, 1);
  require(draw.state().players[0].deck.size() == 8 && draw.state().players[0].hand.size() == 10,
          "normal replacement refills deterministic deck before effect");
}

void phase_and_terminal(Catalogs& catalogs) {
  BattleEngine engine(catalogs.load("1,Ember,1,burn,4,2\n2,Renew,1,regen,3,2\n3,Barrier,1,shield,6,0\n"));
  action(engine, 1);
  require(engine.state().players[1].hp == 30 && engine.state().players[1].statuses.burn.turns == 2,
          "burn waits for holder end");
  const auto ended = action(engine, 3);
  require(ended.events.size() == 2 && ended.events[1].payload("phase-match") ==
      "match_id=phase-match;turn_id=2;player=1;action_id=0;type=status_tick;phase=end;status=burn;value=4;remaining=1;hp=26",
      "golden turn-end event identity");
  require(engine.state().players[1].shield == 6, "burn bypasses shield");
  action(engine, 2);
  const auto cross = action(engine, 1);
  require(cross.events.size() == 3 && cross.events[1].player == 1 && cross.events[1].turn_id == 4 &&
          cross.events[1].turn_end && cross.events[2].player == 0 && cross.events[2].turn_id == 5 &&
          !cross.events[2].turn_end && cross.events[2].status == arena::battle::StatusKind::Regen,
          "outgoing burn precedes incoming regen in structured event batch");

  BattleEngine poison(catalogs.load("1,Venom,1,poison,30,2\n2,Renew,1,regen,30,2\n3,Barrier,1,shield,6,0\n"));
  const auto lethal_poison = action(poison, 1);
  require(lethal_poison.terminal && lethal_poison.winner == 0 && lethal_poison.reason == "poison" &&
          poison.state().turn_id == 2 && lethal_poison.events.size() == 2, "start lethal outcome");
  rejects_without_mutation(poison, {CommandKind::EndTurn, 1, 2, 1, 0}, "battle_finished");

  BattleEngine burn(catalogs.load("1,Ember,1,burn,30,2\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"));
  action(burn, 1);
  const auto lethal_burn = action(burn);
  require(lethal_burn.terminal && lethal_burn.winner == 0 && lethal_burn.reason == "burn" &&
          burn.state().turn_id == 2 && lethal_burn.events[1].turn_end, "end lethal keeps outgoing turn");

  BattleEngine direct(catalogs.load("1,Focus,1,attack_boost,3,1\n2,Strike,2,damage,27,0\n3,Ember,1,burn,30,2\n"));
  action(direct, 1);
  action(direct, 3);
  const auto direct_win = action(direct, 2);
  require(direct_win.terminal && direct_win.winner == 0 && direct_win.reason.empty() &&
          direct_win.events.size() == 1 && direct.state().players[0].hp == 30 && direct.state().turn_id == 3,
          "boosted direct victory suppresses outgoing lethal burn");

  for (const int value : {3, 30}) {
    BattleEngine final(catalogs.load("1,Ember,1,burn," + std::to_string(value) +
        ",2\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"));
    while (final.state().turn_id < 39) action(final);
    action(final, 1);
    const auto outcome = action(final);
    require(outcome.terminal && outcome.winner == 0 && outcome.events.size() == 2 &&
            outcome.events[1].turn_id == 40 && outcome.reason == (value == 30 ? "burn" : "max_turns") &&
            final.state().turn_id == (value == 30 ? 40u : 41u), "turn-40 burn precedes maximum cutoff");
  }
  BattleEngine maximum(catalogs.load("1,Strike,2,damage,8,0\n2,Mend,1,heal,4,0\n3,Barrier,1,shield,6,0\n"));
  BattleOutcome last;
  arena::battle::BattleReplay golden(2026);
  require(golden.append({1, 1, 0, -1, "match_start", "match_id=golden-match"}), "golden replay start");
  for (int turn = 0; turn < 40; ++turn) {
    last = action(maximum);
    for (const auto& event : last.events)
      require(golden.append({static_cast<std::uint64_t>(golden.events().size() + 1),
          event.turn_id, event.action_id, event.player, "battle_event", event.payload("golden-match")}),
          "golden replay contiguous events");
  }
  require(last.terminal && last.winner == -1 && last.reason == "max_turns" &&
          maximum.state().turn_id == 41 && last.events[0].payload("draw-match") ==
          "match_id=draw-match;turn_id=40;player=1;action_id=20;type=end_turn", "final successful action retained");
  require(golden.append({42, 41, 0, -1, "match_result",
      "match_id=golden-match;winner=-1;reason=max_turns;turn_id=41"}) &&
      golden.digest() == 7880734735704546418ull, "legacy cross-language golden replay digest unchanged");
}

}

int main() {
  Catalogs catalogs;
  validation_and_determinism(catalogs);
  finite_effects(catalogs);
  phase_and_terminal(catalogs);
  std::cout << "pure Battle validation/effects/determinism/event order/terminal boundaries passed\n";
  return 0;
}
