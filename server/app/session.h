#pragma once

#include <memory>
#include <mutex>
#include <string>

#include "server/gateway/session.h"

namespace arena {
class Matchmaker;
class Room;
struct Runtime;

class Session : public gateway::SessionHandler, public std::enable_shared_from_this<Session> {
 public:
  Session(std::shared_ptr<Matchmaker> matchmaker, std::shared_ptr<Runtime> runtime);
  void on_connected(const std::shared_ptr<gateway::Session>& connection) override;
  void on_request(proto::MessageType type, const std::string& payload, const std::string& request_id) override;
  void on_disconnected() override;
  void send(proto::MessageType type, const std::string& payload, std::uint64_t revision = 0,
            const std::string& request_id = {});
  bool closed() const;
  void set_room(const std::shared_ptr<Room>& target);
  std::shared_ptr<Room> room();
  void clear_room(const Room* expected);
  void set_identity(const std::string& user, const std::string& token);
  std::string token() const;
  std::string user() const;
  bool logged_in() const;

 private:
  std::shared_ptr<Matchmaker> mm_;
  std::shared_ptr<Runtime> runtime_;
  mutable std::mutex mu_;
  std::weak_ptr<gateway::Session> connection_;
  std::shared_ptr<Room> room_;
  std::string user_ = "guest";
  std::string token_;
  bool login_ = false;
  void handle(proto::MessageType type, const std::string& payload, const std::string& request_id);
};

}
