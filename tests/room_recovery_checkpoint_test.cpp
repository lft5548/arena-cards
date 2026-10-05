#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>

#include "server/battle/battle_engine.h"
#include "server/room/recovery_checkpoint.h"

static void check(bool condition, const char* message) {
  if (!condition) { std::cerr << message << "\n"; std::exit(1); }
}

int main(int argc, char** argv) {
  check(argc == 2, "card config required");
  auto catalog = std::make_shared<arena::config::CardCatalog>();
  std::string error;
  check(catalog->load_csv(argv[1], error), error.c_str());
  arena::battle::BattleEngine engine(catalog);
  engine.set_rng_seed(811);
  const auto outcome = engine.apply({arena::battle::CommandKind::PlayCard, 0, 1, 1, 1});
  check(outcome.error.empty(), "initial card failed");
  arena::room::RecoveryCheckpoint checkpoint;
  checkpoint.match_id = "recovery-test";
  checkpoint.rules_fingerprint = arena::room::recovery_rules_fingerprint(*catalog);
  checkpoint.snapshot = engine.snapshot(1);
  checkpoint.replay = arena::battle::BattleReplay(811);
  check(checkpoint.replay.append({1, 1, 0, -1, "match_start", "deck=1,2,3"}), "replay append failed");
  checkpoint.users = {"alice", "bob"};
  checkpoint.tokens = {"token-a", "token-b"};
  checkpoint.turn_deadline_ms = 1888111222333;
  checkpoint.disconnect_deadline_ms = {0, 1888111221111};
  checkpoint.receipts[0].push_back({1, "play:1:1", "match_id=recovery-test;request_id=test\nquoted\""});
  auto document = arena::room::RecoveryCodec::encode(checkpoint);
  arena::room::RecoveryCheckpoint restored;
  check(arena::room::RecoveryCodec::decode(document, restored, error), error.c_str());
  check(arena::room::RecoveryCodec::encode(restored) == document, "codec roundtrip changed document");
  check(restored.snapshot.state.players[1].hp == engine.state().players[1].hp, "HP changed");
  check(restored.snapshot.state.rng_state == engine.rng_state(), "RNG changed");
  check(restored.receipts[0][0].response == checkpoint.receipts[0][0].response, "ACK changed");
  check(restored.turn_deadline_ms == checkpoint.turn_deadline_ms, "turn deadline changed");
  check(restored.disconnect_deadline_ms == checkpoint.disconnect_deadline_ms, "disconnect deadline changed");
  check(engine.restore(restored.snapshot, &error), "restored battle rejected");
  const auto again = engine.apply({arena::battle::CommandKind::EndTurn, 1, 2, 1, 0});
  if (!again.error.empty()) std::cerr << "restored outcome: " << again.error << "\n";
  check(again.error.empty() && engine.state().turn == 0, "restored battle cannot continue");
  document.back() ^= 1;
  check(!arena::room::RecoveryCodec::decode(document, restored, error), "corrupt checkpoint accepted");
  document.pop_back();
  check(!arena::room::RecoveryCodec::decode(document, restored, error), "truncated checkpoint accepted");
  checkpoint.receipts[0].resize(129, {1, "play:1:1", "ack"});
  check(!arena::room::RecoveryCodec::decode(arena::room::RecoveryCodec::encode(checkpoint), restored, error),
        "receipt overflow accepted");
  checkpoint.receipts[0].resize(1);
  checkpoint.pending_result = "winner=0";
  check(!arena::room::RecoveryCodec::decode(arena::room::RecoveryCodec::encode(checkpoint), restored, error),
        "unrecorded terminal result accepted");
  std::cout << "room recovery checkpoint test passed\n";
}
