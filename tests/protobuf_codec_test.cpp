#include <cstdlib>
#include <iostream>
#include <string>

#include "arena_cards.pb.h"
#include "server/gateway/protobuf_codec.h"

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) { std::cerr << message << "\n"; std::exit(1); }
  };
  arena::gateway::ProtobufRequest request;
  std::string error;
  arena::v1::PlayCardRequest play;
  play.mutable_context()->set_match_id("match-test");
  play.mutable_context()->set_turn_id(3);
  play.mutable_context()->set_action_id(18446744073709551615ull);
  play.mutable_context()->set_request_id("request-1");
  play.set_card_id(9);
  require(arena::gateway::decode_protobuf_request(0x4006, play.SerializeAsString(), request, error), "decode action");
  require(request.type == arena::proto::MessageType::PlayCardReq && request.request_id == "request-1" &&
          request.payload == "match_id=match-test;turn_id=3;action_id=18446744073709551615;card=9", "typed uint64 context");
  require(!arena::gateway::decode_protobuf_request(0x4006, std::string(1, '\x80'), request, error) &&
          error == "malformed_protobuf", "reject malformed bytes");
  play.mutable_context()->set_match_id("bad;card=1");
  error.clear();
  require(!arena::gateway::decode_protobuf_request(0x4006, play.SerializeAsString(), request, error), "reject injected context");
  require(!arena::gateway::decode_protobuf_request(0x4007, "", request, error) &&
          error == "unexpected_message_direction", "reject response as request");
  require(!arena::gateway::decode_protobuf_request(0x4fff, "", request, error), "reject unknown ID");
  const auto snapshot = arena::gateway::encode_protobuf_response(arena::proto::MessageType::BattleSnapshot,
      "match_id=match-test;player_index=1;turn=0;turn_id=3;revision=3;last_action_id=2;"
      "replay_revision=8;replay_seed=18446744073709551615;replay_digest=9223372036854775808;replay_valid=1;"
      "p0_hp=28;p1_hp=30;p0_poison_value=3;p0_poison_turns=2;p1_burn_value=4;p1_burn_turns=2;hand=9,2;discard=1,3;"
      "discard_count=2;opponent_discard_count=1;opponent_hand_count=3;deck_count=9");
  require(arena::proto::read_u16_be(snapshot.data() + 4) == 0x4007, "isolated response ID");
  arena::v1::BattleSnapshot rebuilt;
  require(rebuilt.ParseFromArray(snapshot.data() + 6, static_cast<int>(snapshot.size() - 6)), "decode structured snapshot");
  require(rebuilt.player_index() == 1 && rebuilt.context().revision() == 8 &&
          rebuilt.state().snapshot_revision() == 3 && rebuilt.state().players_size() == 2 &&
          rebuilt.state().hand_size() == 2 && rebuilt.state().hand(0) == 9 &&
          rebuilt.state().discard(1) == 3 && rebuilt.state().last_action_id() == 2 &&
          rebuilt.state().players(0).poison().turns() == 2 &&
          rebuilt.state().players(1).burn().value() == 4 && rebuilt.state().players(1).burn().turns() == 2 &&
          rebuilt.state().replay().seed() == 18446744073709551615ull, "private typed state and lossless integers");
  const auto bonus_frame = arena::gateway::encode_protobuf_response(arena::proto::MessageType::BattleSnapshot,
      "match_id=match-test;player_index=0;p0_attack_boost_value=3;p0_attack_boost_uses=2;"
      "p1_heal_boost_value=4;p1_heal_boost_uses=1");
  arena::v1::BattleSnapshot bonus_snapshot;
  require(bonus_snapshot.ParseFromArray(bonus_frame.data() + 6, static_cast<int>(bonus_frame.size() - 6)) &&
          bonus_snapshot.state().players(0).attack_boost().value() == 3 &&
          bonus_snapshot.state().players(1).heal_boost().uses() == 1, "typed bonus snapshot");
  for (const auto& payload : {std::string("card=1;type=damage;value=8"),
                              std::string("card=1;type=damage;value=8;bonus=0"),
                              std::string("card=1;type=damage;value=8;bonus=3")}) {
    const auto wire = arena::gateway::encode_protobuf_response(arena::proto::MessageType::BattleEvent, payload);
    arena::v1::BattleEvent decoded;
    require(decoded.ParseFromArray(wire.data() + 6, static_cast<int>(wire.size() - 6)), "decode bonus event");
    require(decoded.has_bonus() == (payload.find(";bonus=") != std::string::npos), "optional zero bonus presence");
  }
  const auto ack = arena::gateway::encode_protobuf_response(arena::proto::MessageType::ActionAck,
      "match_id=match-test;turn_id=1;action_id=1;status=applied;revision=2;request_id=original", 99, "retry");
  arena::v1::ActionAck receipt;
  require(receipt.ParseFromArray(ack.data() + 6, static_cast<int>(ack.size() - 6)) && receipt.applied() &&
          receipt.context().revision() == 2 && receipt.context().request_id() == "original", "stable cached ACK");
  const auto tick = arena::gateway::encode_protobuf_response(arena::proto::MessageType::BattleEvent,
      "match_id=match-test;turn_id=2;player=1;action_id=0;type=status_tick;phase=end;status=burn;value=3;remaining=0;hp=22", 5);
  arena::v1::BattleEvent end_tick;
  require(end_tick.ParseFromArray(tick.data() + 6, static_cast<int>(tick.size() - 6)) &&
          end_tick.phase() == "end" && end_tick.status() == "burn" && end_tick.context().revision() == 5,
          "typed end phase and revision");
  const auto result = arena::gateway::encode_protobuf_response(arena::proto::MessageType::MatchResult,
      "match_id=match-test;turn_id=41;winner=-1;reason=max_turns");
  arena::v1::MatchResult terminal;
  require(terminal.ParseFromArray(result.data() + 6, static_cast<int>(result.size() - 6)) &&
          terminal.winner() == -1, "signed draw winner");
  const auto admin_frame = arena::gateway::encode_protobuf_response(arena::proto::MessageType::AdminRoomsResp,
      "active_rooms=2;active_sessions=3;settlement_outbox_failures=5;"
      "settlement_outbox_load_failures=17;settlement_outbox_mark_failures=18;"
      "settlement_outbox_record_failures=19;mysql_connection_attempts=18446744073709551615;"
      "mysql_connection_successes=20;mysql_connection_failures=21;mysql_connection_losses=22;"
      "redis_connection_failures=23;redis_apply_failures=9223372036854775808", 0, "admin-metrics");
  arena::v1::AdminRoomsResponse admin;
  require(arena::proto::read_u16_be(admin_frame.data() + 4) == 0x4011 &&
          admin.ParseFromArray(admin_frame.data() + 6, static_cast<int>(admin_frame.size() - 6)) &&
          admin.request_id() == "admin-metrics", "typed admin response ID and correlation");
  require(admin.counters().size() == 12 && admin.counters().at("active_rooms") == 2 &&
          admin.counters().at("active_sessions") == 3 && admin.counters().at("settlement_outbox_failures") == 5 &&
          admin.counters().at("settlement_outbox_load_failures") == 17 &&
          admin.counters().at("settlement_outbox_mark_failures") == 18 &&
          admin.counters().at("settlement_outbox_record_failures") == 19 &&
          admin.counters().at("mysql_connection_attempts") == 18446744073709551615ull &&
          admin.counters().at("mysql_connection_successes") == 20 &&
          admin.counters().at("mysql_connection_failures") == 21 &&
          admin.counters().at("mysql_connection_losses") == 22 &&
          admin.counters().at("redis_connection_failures") == 23 &&
          admin.counters().at("redis_apply_failures") == 9223372036854775808ull,
          "failure classifications and lossless uint64 ProtoV1 metrics");
  arena::v1::LeaderboardRequest leaderboard_request;
  leaderboard_request.set_request_id("leaderboard-default");
  require(arena::gateway::decode_protobuf_request(0x4015, leaderboard_request.SerializeAsString(), request, error) &&
          request.payload.empty() && request.request_id == "leaderboard-default", "leaderboard default limit presence");
  leaderboard_request.set_limit(0);
  require(arena::gateway::decode_protobuf_request(0x4015, leaderboard_request.SerializeAsString(), request, error) &&
          request.payload == "limit=0", "explicit zero must reach limit validation");
  require(!arena::gateway::decode_protobuf_request(0x4016, "", request, error) &&
          error == "unexpected_message_direction", "leaderboard response cannot be request");
  require(!arena::gateway::decode_protobuf_request(0x4013, "", request, error) &&
          error == "unknown_message", "negotiation IDs excluded from protobuf namespace");
  const auto ranking_frame = arena::gateway::encode_protobuf_response(arena::proto::MessageType::LeaderboardResp,
      "ok=1;source=mysql;count=1;entry_0_rank=1;entry_0_user=rank%3B%253B%2525player;"
      "entry_0_rating=-9223372036854775808;entry_0_wins=18446744073709551615;entry_0_losses=9223372036854775808",
      0, "rank-1");
  arena::v1::LeaderboardResponse ranking;
  require(arena::proto::read_u16_be(ranking_frame.data() + 4) == 0x4016 &&
          ranking.ParseFromArray(ranking_frame.data() + 6, static_cast<int>(ranking_frame.size() - 6)) &&
          ranking.ok() && ranking.source() == "mysql" && ranking.request_id() == "rank-1" && ranking.entries_size() == 1,
          "typed leaderboard correlation and source");
  require(ranking.entries(0).rank() == 1 && ranking.entries(0).player_id() == "rank;%3B%25player" &&
          ranking.entries(0).rating() == (-9223372036854775807ll - 1) &&
          ranking.entries(0).wins() == 18446744073709551615ull &&
          ranking.entries(0).losses() == 9223372036854775808ull, "leaderboard lossless signed and unsigned integers");
  return 0;
}
