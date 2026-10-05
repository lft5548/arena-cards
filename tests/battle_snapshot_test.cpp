#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <string>

#include "server/battle/battle_engine.h"
#include "server/battle/snapshot_store.h"

namespace {
void require(bool value, const char* message) {
  if (!value) { std::cerr << "battle snapshot test failed: " << message << "\n"; std::exit(1); }
}
}

int main() {
  const auto directory = std::filesystem::temp_directory_path() /
      ("arena-snapshot-" + std::to_string(
          std::chrono::steady_clock::now().time_since_epoch().count()));
  std::filesystem::create_directories(directory);
  const auto catalog_path = directory / "cards.csv";
  {
    std::ofstream output(catalog_path);
    output << "id,name,cost,effect,value,duration\n"
           << "1,Strike,1,damage,2,0\n2,Mend,1,heal,2,0\n3,Barrier,1,shield,2,0\n";
  }
  auto catalog = std::make_shared<arena::config::CardCatalog>();
  std::string error;
  require(catalog->load_csv(catalog_path.string(), error), "catalog");

  arena::battle::BattleEngine engine(catalog);
  engine.set_rng_seed(123456789);
  auto first = engine.apply({arena::battle::CommandKind::PlayCard, 0, 1, 1, 1});
  require(first.error.empty(), "first action");
  const auto snapshot = engine.snapshot(4);
  require(snapshot.version == arena::battle::BattleSnapshot::kVersion &&
          snapshot.revision == 4 && snapshot.state.rng_state == 123456789 &&
          snapshot.state.players[0].hand == std::vector<int>({2, 3, 1}),
          "snapshot captures authoritative state and RNG");

  arena::battle::SnapshotStore store(directory);
  require(store.save("match-snapshot", snapshot, &error), "snapshot save");
  arena::battle::SnapshotDocument loaded;
  require(store.load("match-snapshot", loaded, &error), "snapshot load");
  require(loaded.match_id == "match-snapshot" &&
          loaded.snapshot.revision == snapshot.revision &&
          loaded.snapshot.state.rng_state == snapshot.state.rng_state &&
          loaded.snapshot.state.players[0].hand == snapshot.state.players[0].hand &&
          loaded.snapshot.state.players[1].deck == snapshot.state.players[1].deck,
          "snapshot round trip");

  arena::battle::BattleEngine restored(catalog);
  require(restored.restore(loaded.snapshot, &error), "engine restore");
  require(restored.state().turn_id == engine.state().turn_id &&
          restored.state().last_actions == engine.state().last_actions &&
          restored.state().players[0].hand == engine.state().players[0].hand &&
          restored.rng_state() == engine.rng_state(), "restored engine state");
  auto invalid = loaded.snapshot;
  invalid.version++;
  require(!restored.restore(invalid, &error) && error == "unsupported_snapshot_version",
          "reject unknown snapshot version");
  require(!store.save("../escape", snapshot, &error) && error == "invalid_match_id",
          "reject snapshot path traversal");

  std::error_code ignored;
  std::filesystem::remove_all(directory, ignored);
  std::cout << "battle snapshot/RNG persistence passed\n";
  return 0;
}
