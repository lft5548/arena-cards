#pragma once

#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>

namespace arena {
class Room;
namespace room {

struct ResumeEntry {
  std::weak_ptr<Room> room;
  int player_index = -1;
};

class RoomRegistry {
 public:
  void attach(const std::string& token, const std::shared_ptr<Room>& target, int player_index);
  ResumeEntry find(const std::string& token) const;
  void erase(const std::string& token, const Room* expected = nullptr);

 private:
  mutable std::mutex mutex_;
  std::unordered_map<std::string, ResumeEntry> entries_;
};

}
}
