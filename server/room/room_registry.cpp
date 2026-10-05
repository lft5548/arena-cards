#include "server/room/room_registry.h"

namespace arena::room {

void RoomRegistry::attach(const std::string& token, const std::shared_ptr<Room>& target, int player_index) {
  std::lock_guard<std::mutex> lock(mutex_);
  entries_[token] = {target, player_index};
}

ResumeEntry RoomRegistry::find(const std::string& token) const {
  std::lock_guard<std::mutex> lock(mutex_);
  const auto found = entries_.find(token);
  return found == entries_.end() ? ResumeEntry{} : found->second;
}

void RoomRegistry::erase(const std::string& token, const Room* expected) {
  std::lock_guard<std::mutex> lock(mutex_);
  const auto entry = entries_.find(token);
  if (expected && entry != entries_.end() && entry->second.room.lock().get() != expected) return;
  entries_.erase(token);
}

}
