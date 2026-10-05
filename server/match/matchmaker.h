#pragma once

#include <memory>
#include <mutex>
#include <queue>
#include <unordered_set>

#include "server/config/card_catalog.h"

namespace arena {
class Session;
struct Runtime;

class Matchmaker {
 public:
  Matchmaker(std::shared_ptr<const config::CardCatalog> catalog, std::shared_ptr<Runtime> runtime);
  void enqueue(const std::shared_ptr<Session>& session);
  void cancel(const std::shared_ptr<Session>& session);
  void stop();

 private:
  std::mutex mu_;
  std::queue<std::weak_ptr<Session>> q_;
  std::unordered_set<Session*> queued_;
  std::shared_ptr<const config::CardCatalog> catalog_;
  std::shared_ptr<Runtime> runtime_;
};

}
