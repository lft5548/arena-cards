#include "server/app/session.h"

#include <cstdlib>
#include <charconv>
#include <sstream>
#include <utility>

#include "server/app/runtime.h"
#include "server/app/identifiers.h"
#include "server/match/matchmaker.h"
#include "server/persistence/mysql_store.h"
#include "server/room/room.h"

namespace arena {
using proto::MessageType;

namespace {
std::string escape_leaderboard_field(const std::string& value) {
  std::string escaped;
  for (const auto character : value) {
    if (character == '%') escaped += "%25";
    else if (character == ';') escaped += "%3B";
    else escaped += character;
  }
  return escaped;
}
}

Session::Session(std::shared_ptr<Matchmaker> matchmaker, std::shared_ptr<Runtime> runtime)
    : mm_(std::move(matchmaker)), runtime_(std::move(runtime)) {}

void Session::on_connected(const std::shared_ptr<gateway::Session>& connection) { connection_ = connection; }

void Session::on_request(MessageType type, const std::string& payload, const std::string& request_id) {
  if (runtime_->shutdown.stopping()) {
    send(MessageType::Error, "code=server_stopping", 0, request_id);
    return;
  }
  handle(type, payload, request_id);
}

void Session::on_disconnected() {
  if (auto target = room()) {
    // A planned server stop preserves online resume metadata instead of
    // manufacturing a player disconnect deadline or a timeout defeat.
    if (!runtime_->shutdown.stopping()) target->enqueue_disconnect(shared_from_this());
    clear_room(target.get());
  } else mm_->cancel(shared_from_this());
}

void Session::send(MessageType type, const std::string& payload, std::uint64_t revision,
                   const std::string& request_id) {
  if (auto connection = connection_.lock()) connection->send(type, payload, revision, request_id);
}

bool Session::closed() const {
  auto connection = connection_.lock();
  return !connection || connection->closed();
}

void Session::set_room(const std::shared_ptr<Room>& target) { std::lock_guard<std::mutex> lock(mu_); room_ = target; }
std::shared_ptr<Room> Session::room() { std::lock_guard<std::mutex> lock(mu_); return room_; }
void Session::clear_room(const Room* expected) { std::lock_guard<std::mutex> lock(mu_); if (room_.get() == expected) room_.reset(); }
void Session::set_identity(const std::string& user, const std::string& token) {
  std::lock_guard<std::mutex> lock(mu_); user_ = user; token_ = token; login_ = true;
}
std::string Session::token() const { std::lock_guard<std::mutex> lock(mu_); return token_; }
std::string Session::user() const { std::lock_guard<std::mutex> lock(mu_); return user_; }
bool Session::logged_in() const { std::lock_guard<std::mutex> lock(mu_); return login_; }

void Session::handle(MessageType type, const std::string& payload, const std::string& request_id) {
  if (type == MessageType::LeaderboardReq) {
    unsigned int limit = 10;
    bool valid_limit = true;
    bool limit_seen = false;
    std::string correlation = request_id;
    std::istringstream stream(payload);
    std::string field;
    while (std::getline(stream, field, ';')) {
      if (field.empty()) continue;
      const auto separator = field.find('=');
      if (separator == std::string::npos) { valid_limit = false; continue; }
      const auto key = field.substr(0, separator);
      const auto value = field.substr(separator + 1);
      if (key == "limit") {
        const auto parsed = std::from_chars(value.data(), value.data() + value.size(), limit);
        if (limit_seen || parsed.ec != std::errc() || parsed.ptr != value.data() + value.size()) valid_limit = false;
        limit_seen = true;
      } else if (key == "request_id" && request_id.empty()) {
        correlation = value;
      }
    }
    bool valid_correlation = correlation.size() <= 128;
    for (const unsigned char character : correlation) {
      if (character < 33 || character > 126 || character == ';' || character == '=') valid_correlation = false;
    }
    if (!valid_correlation) {
      send(MessageType::Error, "code=invalid_request_id");
      return;
    }
    const auto respond = [this, &correlation](std::string response) {
      if (!correlation.empty()) response += ";request_id=" + escape_leaderboard_field(correlation);
      send(MessageType::LeaderboardResp, response, 0, correlation);
    };
    if (!logged_in()) { respond("ok=0;code=login_required;count=0"); return; }
    if (!valid_limit || limit < 1 || limit > 20) { respond("ok=0;code=invalid_limit;count=0"); return; }
    std::vector<persistence::LeaderboardEntry> entries;
    std::string error;
    if (!runtime_->mysql_store || !runtime_->mysql_store->leaderboard(limit, entries, error)) {
      respond("ok=0;code=leaderboard_unavailable;count=0");
      return;
    }
    std::string response = "ok=1;source=mysql;count=" + std::to_string(entries.size());
    for (std::size_t index = 0; index < entries.size(); ++index) {
      const auto& entry = entries[index];
      const auto user = escape_leaderboard_field(entry.player_id);
      const auto prefix = ";entry_" + std::to_string(index) + "_";
      response += prefix + "rank=" + std::to_string(index + 1) + prefix + "user=" + user +
          prefix + "rating=" + std::to_string(entry.rating) + prefix + "wins=" + std::to_string(entry.wins) +
          prefix + "losses=" + std::to_string(entry.losses);
    }
    respond(std::move(response));
    return;
  }
  if (type == MessageType::AdminRoomsReq) {
    if (runtime_->mysql_store) {
      const auto mysql = runtime_->mysql_store->metrics();
      // Concurrent admin requests must not publish an older cumulative snapshot.
      metrics::update_high_watermark(runtime_->counters.mysql_connection_attempts, mysql.connection_attempts);
      metrics::update_high_watermark(runtime_->counters.mysql_connection_successes, mysql.connection_successes);
      metrics::update_high_watermark(runtime_->counters.mysql_connection_failures, mysql.connection_failures);
      metrics::update_high_watermark(runtime_->counters.mysql_connection_losses, mysql.connection_losses);
    }
    send(MessageType::AdminRoomsResp,
         metrics::rooms_payload(runtime_->counters, runtime_->gateway_metrics), 0, request_id);
    return;
  }
  if (type == MessageType::LoginReq) {
    if (payload.size() > 64 || payload.find_first_of(";=\r\n") != std::string::npos ||
        payload.find('\0') != std::string::npos) {
      send(MessageType::Error, "code=invalid_username", 0, request_id);
      return;
    }
    std::lock_guard<std::mutex> lock(mu_);
    user_ = payload.empty() ? "guest" : payload;
    token_ = app::make_token();
    login_ = true;
    send(MessageType::LoginResp, "ok=1;user=" + user_ + ";token=" + token_, 0, request_id);
    return;
  }
  if (type == MessageType::MatchJoinReq) {
    if (!logged_in()) { send(MessageType::Error, "code=login_required", 0, request_id); return; }
    if (room()) { send(MessageType::Error, "code=already_in_room", 0, request_id); return; }
    mm_->enqueue(shared_from_this());
    send(MessageType::MatchJoinReq, "queued=1", 0, request_id);
    return;
  }
  if (type == MessageType::MatchCancelReq) {
    mm_->cancel(shared_from_this());
    send(MessageType::MatchCancelReq, "cancelled=1", 0, request_id);
    return;
  }
  if (type == MessageType::PlayCardReq || type == MessageType::EndTurnReq) {
    auto target = room();
    if (!target) { send(MessageType::Error, "code=no_room", 0, request_id); return; }
    int card = 0;
    unsigned long long action_id = 0;
    unsigned long long turn_id = 0;
    std::string match_id;
    std::istringstream stream(payload);
    std::string field;
    while (std::getline(stream, field, ';')) {
      const auto separator = field.find('=');
      if (separator == std::string::npos) continue;
      const auto key = field.substr(0, separator);
      const auto value = field.substr(separator + 1);
      if (key == "card") card = std::atoi(value.c_str());
      else if (key == "action_id") action_id = std::strtoull(value.c_str(), nullptr, 10);
      else if (key == "turn_id") turn_id = std::strtoull(value.c_str(), nullptr, 10);
      else if (key == "match_id") match_id = value;
    }
    if (type == MessageType::EndTurnReq)
      target->enqueue_end_turn(shared_from_this(), match_id, turn_id, action_id, request_id);
    else target->enqueue_play(shared_from_this(), match_id, turn_id, card, action_id, request_id);
    return;
  }
  if (type == MessageType::ReconnectReq) {
    if (room()) { send(MessageType::ReconnectResp, "ok=0;code=already_connected", 0, request_id); return; }
    const auto entry = runtime_->rooms.find(payload);
    auto target = entry.room.lock();
    if (target) target->enqueue_reconnect(entry.player_index, payload, shared_from_this(), request_id);
    else send(MessageType::ReconnectResp, "ok=0;code=resume_expired", 0, request_id);
  }
}

}
