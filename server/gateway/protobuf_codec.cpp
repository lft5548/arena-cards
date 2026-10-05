#include "server/gateway/protobuf_codec.h"

#include <charconv>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

#include "arena_cards.pb.h"

namespace arena::gateway {
namespace {

using Fields = std::unordered_map<std::string, std::string>;

Fields fields_from(const std::string& payload) {
  Fields fields;
  std::istringstream stream(payload);
  std::string item;
  while (std::getline(stream, item, ';')) {
    const auto separator = item.find('=');
    if (separator != std::string::npos) fields.emplace(item.substr(0, separator), item.substr(separator + 1));
  }
  return fields;
}

std::string field(const Fields& fields, const std::string& key) {
  const auto found = fields.find(key);
  return found == fields.end() ? std::string() : found->second;
}

template <typename Integer>
Integer number(const Fields& fields, const std::string& key) {
  const auto value = field(fields, key);
  Integer result = 0;
  const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
  return parsed.ec == std::errc() && parsed.ptr == value.data() + value.size() ? result : 0;
}

bool valid_identifier(const std::string& value, std::size_t limit) {
  if (value.size() > limit) return false;
  for (const unsigned char character : value) {
    if (character < 33 || character > 126 || character == ';' || character == '=') return false;
  }
  return true;
}

void context_from(const Fields& fields, v1::RequestContext* context, std::uint64_t revision,
                  const std::string& request_id) {
  context->set_match_id(field(fields, "match_id"));
  context->set_turn_id(number<std::uint64_t>(fields, "turn_id"));
  context->set_action_id(number<std::uint64_t>(fields, "action_id"));
  const auto recorded = number<std::uint64_t>(fields, "revision");
  context->set_revision(recorded ? recorded : revision);
  const auto saved_id = fields.find("request_id");
  context->set_request_id(saved_id == fields.end() ? request_id : saved_id->second);
}

void replay_from(const Fields& fields, v1::ReplayMetadata* metadata) {
  metadata->set_seed(number<std::uint64_t>(fields, "replay_seed"));
  metadata->set_revision(number<std::uint64_t>(fields, "replay_revision"));
  metadata->set_digest(number<std::uint64_t>(fields, "replay_digest"));
  metadata->set_valid(field(fields, "replay_valid") == "1");
}

void cards_from(const std::string& value, google::protobuf::RepeatedField<std::int32_t>* cards) {
  std::istringstream stream(value);
  std::string item;
  while (std::getline(stream, item, ',')) {
    int card = 0;
    const auto parsed = std::from_chars(item.data(), item.data() + item.size(), card);
    if (parsed.ec != std::errc() || parsed.ptr != item.data() + item.size())
      throw std::runtime_error("invalid internal card list");
    cards->Add(card);
  }
}

template <typename Message>
bool parse_request(const std::string& bytes, Message& message, ProtobufRequest& request,
                    std::string& error) {
  if (!message.ParseFromString(bytes)) {
    error = "malformed_protobuf";
    return false;
  }
  request.request_id = message.request_id();
  if (!valid_identifier(request.request_id, 128)) {
    error = "invalid_request_id";
    return false;
  }
  return true;
}

template <typename Message>
std::vector<std::uint8_t> frame(proto::MessageType type, const Message& message) {
  std::string bytes;
  if (!message.SerializeToString(&bytes)) throw std::runtime_error("protobuf serialization failed");
  if (bytes.size() + 2 > 65536) throw std::runtime_error("protobuf frame too large");
  return proto::encode(static_cast<proto::ProtoMessageType>(
      proto::kProtoV1MessageBase + static_cast<std::uint16_t>(type)), bytes);
}

bool action_context(const v1::RequestContext& context, ProtobufRequest& request,
                     std::string& error) {
  if (!valid_identifier(context.match_id(), 128) || context.match_id().empty() ||
      !valid_identifier(context.request_id(), 128)) {
    error = "invalid_request_context";
    return false;
  }
  request.request_id = context.request_id();
  request.payload = "match_id=" + context.match_id() + ";turn_id=" + std::to_string(context.turn_id()) +
      ";action_id=" + std::to_string(context.action_id());
  return true;
}

}

bool decode_protobuf_request(std::uint16_t message_id, const std::string& bytes,
                             ProtobufRequest& request, std::string& error) {
  request = ProtobufRequest{};
  error.clear();
  if (message_id <= proto::kProtoV1MessageBase || message_id > proto::kProtoV1MessageBase + 22 ||
      message_id == proto::kProtoV1MessageBase + 19 || message_id == proto::kProtoV1MessageBase + 20) {
    error = "unknown_message";
    return false;
  }
  request.type = static_cast<proto::MessageType>(message_id - proto::kProtoV1MessageBase);
  switch (request.type) {
    case proto::MessageType::LoginReq: {
      v1::LoginRequest message;
      if (!parse_request(bytes, message, request, error)) return false;
      request.payload = message.user();
      return true;
    }
    case proto::MessageType::MatchJoinReq: {
      v1::MatchJoinRequest message;
      return parse_request(bytes, message, request, error);
    }
    case proto::MessageType::MatchCancelReq: {
      v1::MatchCancelRequest message;
      return parse_request(bytes, message, request, error);
    }
    case proto::MessageType::Heartbeat: {
      v1::Heartbeat message;
      return parse_request(bytes, message, request, error);
    }
    case proto::MessageType::AdminRoomsReq: {
      v1::AdminRoomsRequest message;
      return parse_request(bytes, message, request, error);
    }
    case proto::MessageType::LeaderboardReq: {
      v1::LeaderboardRequest message;
      if (!parse_request(bytes, message, request, error)) return false;
      if (message.has_limit()) request.payload = "limit=" + std::to_string(message.limit());
      return true;
    }
    case proto::MessageType::ReconnectReq: {
      v1::ReconnectRequest message;
      if (!parse_request(bytes, message, request, error)) return false;
      if (message.session_token().empty() || !valid_identifier(message.session_token(), 256)) {
        error = "invalid_session_token";
        return false;
      }
      request.payload = message.session_token();
      return true;
    }
    case proto::MessageType::PlayCardReq: {
      v1::PlayCardRequest message;
      if (!message.ParseFromString(bytes)) { error = "malformed_protobuf"; return false; }
      if (!message.has_context() || !action_context(message.context(), request, error)) {
        if (error.empty()) error = "invalid_request_context";
        return false;
      }
      request.payload += ";card=" + std::to_string(message.card_id());
      return true;
    }
    case proto::MessageType::EndTurnReq: {
      v1::EndTurnRequest message;
      if (!message.ParseFromString(bytes)) { error = "malformed_protobuf"; return false; }
      if (!message.has_context() || !action_context(message.context(), request, error)) {
        if (error.empty()) error = "invalid_request_context";
        return false;
      }
      return true;
    }
    default:
      error = "unexpected_message_direction";
      return false;
  }
}

std::vector<std::uint8_t> encode_protobuf_response(proto::MessageType type,
    const std::string& payload, std::uint64_t revision, const std::string& request_id) {
  const auto fields = fields_from(payload);
  switch (type) {
    case proto::MessageType::LoginResp: {
      v1::LoginResponse message;
      message.set_ok(field(fields, "ok") == "1");
      message.set_user(field(fields, "user"));
      message.set_session_token(field(fields, "token"));
      message.set_error_code(field(fields, "code"));
      message.set_request_id(request_id);
      return frame(type, message);
    }
    case proto::MessageType::MatchJoinReq: {
      v1::MatchJoinResponse message;
      message.set_queued(field(fields, "queued") == "1");
      message.set_request_id(request_id);
      return frame(type, message);
    }
    case proto::MessageType::MatchCancelReq: {
      v1::MatchCancelResponse message;
      message.set_cancelled(field(fields, "cancelled") == "1");
      message.set_request_id(request_id);
      return frame(type, message);
    }
    case proto::MessageType::MatchFound: {
      v1::MatchFound message;
      context_from(fields, message.mutable_context(), revision, request_id);
      message.set_player_index(number<int>(fields, "player_index"));
      message.set_turn(number<int>(fields, "turn"));
      message.set_room(number<std::uint32_t>(fields, "room"));
      return frame(type, message);
    }
    case proto::MessageType::BattleSnapshot: {
      v1::BattleSnapshot message;
      context_from(fields, message.mutable_context(), number<std::uint64_t>(fields, "replay_revision"), request_id);
      message.mutable_context()->set_revision(number<std::uint64_t>(fields, "replay_revision"));
      message.set_player_index(number<int>(fields, "player_index"));
      auto* state = message.mutable_state();
      state->set_turn(number<int>(fields, "turn"));
      state->set_snapshot_revision(number<std::uint64_t>(fields, "revision"));
      state->set_remaining_ms(number<std::uint32_t>(fields, "remaining_ms"));
      state->set_done(field(fields, "done") == "1");
      for (int index = 0; index < 2; ++index) {
        auto* player = state->add_players();
        const auto prefix = "p" + std::to_string(index) + "_";
        player->set_hp(number<int>(fields, prefix + "hp"));
        player->set_energy(number<int>(fields, prefix + "energy"));
        player->set_shield(number<int>(fields, prefix + "shield"));
        player->mutable_poison()->set_value(number<int>(fields, prefix + "poison_value"));
        player->mutable_poison()->set_turns(number<int>(fields, prefix + "poison_turns"));
        player->mutable_regen()->set_value(number<int>(fields, prefix + "regen_value"));
        player->mutable_regen()->set_turns(number<int>(fields, prefix + "regen_turns"));
        player->mutable_burn()->set_value(number<int>(fields, prefix + "burn_value"));
        player->mutable_burn()->set_turns(number<int>(fields, prefix + "burn_turns"));
        player->mutable_attack_boost()->set_value(number<int>(fields, prefix + "attack_boost_value"));
        player->mutable_attack_boost()->set_uses(number<int>(fields, prefix + "attack_boost_uses"));
        player->mutable_heal_boost()->set_value(number<int>(fields, prefix + "heal_boost_value"));
        player->mutable_heal_boost()->set_uses(number<int>(fields, prefix + "heal_boost_uses"));
      }
      cards_from(field(fields, "hand"), state->mutable_hand());
      cards_from(field(fields, "discard"), state->mutable_discard());
      state->set_opponent_hand_count(number<std::uint32_t>(fields, "opponent_hand_count"));
      state->set_deck_count(number<std::uint32_t>(fields, "deck_count"));
      state->set_discard_count(number<std::uint32_t>(fields, "discard_count"));
      state->set_opponent_discard_count(number<std::uint32_t>(fields, "opponent_discard_count"));
      state->set_last_action_id(number<std::uint64_t>(fields, "last_action_id"));
      replay_from(fields, state->mutable_replay());
      return frame(type, message);
    }
    case proto::MessageType::BattleEvent: {
      v1::BattleEvent message;
      context_from(fields, message.mutable_context(), revision, request_id);
      message.set_type(field(fields, "type"));
      message.set_player(number<int>(fields, "player"));
      message.set_card_id(number<int>(fields, "card"));
      message.set_value(number<int>(fields, "value"));
      message.set_duration(number<int>(fields, "duration"));
      message.set_status(field(fields, "status"));
      message.set_remaining(number<int>(fields, "remaining"));
      message.set_hp(number<int>(fields, "hp"));
      message.set_target(number<int>(fields, "target"));
      message.set_count(number<int>(fields, "count"));
      message.set_phase(field(fields, "phase"));
      if (fields.count("bonus")) message.set_bonus(number<int>(fields, "bonus"));
      message.set_uses(number<int>(fields, "uses"));
      return frame(type, message);
    }
    case proto::MessageType::ActionAck: {
      v1::ActionAck message;
      context_from(fields, message.mutable_context(), revision, request_id);
      message.set_applied(field(fields, "status") == "applied");
      message.set_error_code(field(fields, "code"));
      return frame(type, message);
    }
    case proto::MessageType::MatchResult: {
      v1::MatchResult message;
      context_from(fields, message.mutable_context(), number<std::uint64_t>(fields, "replay_revision"), request_id);
      message.set_winner(number<int>(fields, "winner"));
      message.set_reason(field(fields, "reason"));
      replay_from(fields, message.mutable_replay());
      return frame(type, message);
    }
    case proto::MessageType::ReconnectResp: {
      v1::ReconnectResponse message;
      message.set_ok(field(fields, "ok") == "1");
      context_from(fields, message.mutable_context(), revision, request_id);
      message.set_player_index(number<int>(fields, "player_index"));
      message.set_error_code(field(fields, "code"));
      return frame(type, message);
    }
    case proto::MessageType::Error: {
      v1::ErrorResponse message;
      message.set_code(field(fields, "code"));
      message.set_message(field(fields, "message"));
      context_from(fields, message.mutable_context(), revision, request_id);
      return frame(type, message);
    }
    case proto::MessageType::Pong: {
      v1::Pong message;
      message.set_request_id(request_id);
      return frame(type, message);
    }
    case proto::MessageType::AdminRoomsResp: {
      v1::AdminRoomsResponse message;
      for (const auto& entry : fields) (*message.mutable_counters())[entry.first] = number<std::uint64_t>(fields, entry.first);
      message.set_request_id(request_id);
      return frame(type, message);
    }
    case proto::MessageType::LeaderboardResp: {
      v1::LeaderboardResponse message;
      message.set_ok(field(fields, "ok") == "1");
      message.set_error_code(field(fields, "code"));
      message.set_request_id(request_id);
      message.set_source(field(fields, "source"));
      const auto count = number<unsigned int>(fields, "count");
      if (count > 20) throw std::runtime_error("internal leaderboard exceeds limit");
      for (unsigned int index = 0; index < count; ++index) {
        const auto prefix = "entry_" + std::to_string(index) + "_";
        auto* entry = message.add_entries();
        entry->set_rank(number<std::uint32_t>(fields, prefix + "rank"));
        std::string user = field(fields, prefix + "user");
        std::size_t separator = 0;
        while ((separator = user.find("%3B", separator)) != std::string::npos) {
          user.replace(separator, 3, ";");
          ++separator;
        }
        separator = 0;
        while ((separator = user.find("%25", separator)) != std::string::npos) {
          user.replace(separator, 3, "%");
          ++separator;
        }
        entry->set_player_id(user);
        entry->set_rating(number<std::int64_t>(fields, prefix + "rating"));
        entry->set_wins(number<std::uint64_t>(fields, prefix + "wins"));
        entry->set_losses(number<std::uint64_t>(fields, prefix + "losses"));
      }
      return frame(type, message);
    }
    default:
      throw std::runtime_error("unsupported protobuf response");
  }
}

}
