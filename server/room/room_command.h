#pragma once

#include <cstdint>
#include <future>
#include <memory>
#include <string>

namespace arena {
class Session;
namespace room {

// Every room state transition enters through this command vocabulary. The
// actor owns battle state; producers only append commands to the queue.
enum class CommandType : std::uint8_t {
  Start,
  PlayCard,
  EndTurn,
  Disconnect,
  Reconnect,
  Snapshot,
  Tick,
  SettlementComplete,
  ReplayPersistComplete,
  Stop,
};

struct Command {
  CommandType type = CommandType::Tick;
  std::weak_ptr<arena::Session> session;
  std::shared_ptr<arena::Session> disconnect_session;
  std::string match_id;
  std::string request_id;
  std::string token;
  std::uint64_t turn_id = 0;
  std::uint64_t action_id = 0;
  int card = 0;
  int player_index = -1;
  bool settlement_ok = false;
  std::string settlement_error;
  bool replay_persist_ok = false;
  std::string replay_path;
  std::string replay_persist_error;
  std::shared_ptr<std::promise<bool>> result;
};

}  // namespace room
}  // namespace arena
