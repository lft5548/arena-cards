#pragma once

#include <memory>
#include <string>
#include <vector>

#include "server/battle/battle_command.h"
#include "server/battle/battle_event.h"
#include "server/battle/battle_state.h"
#include "server/battle/battle_snapshot.h"
#include "server/config/card_catalog.h"

namespace arena::battle {

struct BattleOutcome {
  std::string error;
  std::vector<BattleEvent> events;
  bool terminal = false;
  int winner = -1;
  std::string reason;
};

class BattleEngine {
 public:
  explicit BattleEngine(std::shared_ptr<const config::CardCatalog> catalog);
  const BattleState& state() const { return state_; }
  const config::CardCatalog& catalog() const { return *catalog_; }
  BattleOutcome apply(const BattleCommand& command);

  // The seed is supplied by the room after match_id allocation. Existing
  // callers keep the stable deterministic default.
  void set_rng_seed(std::uint64_t seed);
  std::uint64_t rng_state() const { return state_.rng_state; }
  BattleSnapshot snapshot(std::uint64_t revision = 0) const;
  bool restore(const BattleSnapshot& snapshot, std::string* error = nullptr);

 private:
  std::shared_ptr<const config::CardCatalog> catalog_;
  BattleState state_;
  std::string validate(const BattleCommand& command) const;
  void advance_turn(BattleOutcome& outcome);
  void conclude(BattleOutcome& outcome, int winner, const std::string& reason);
  void append_ticks(BattleOutcome& outcome, const std::vector<StatusTick>& ticks, bool turn_end);
  std::uint64_t next_random();
  void shuffle_refill(std::vector<int>& deck);
};

}
