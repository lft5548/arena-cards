#include "server/match/matchmaker.h"

#include <utility>

#include "server/app/runtime.h"
#include "server/app/session.h"
#include "server/room/room.h"

namespace arena {

Matchmaker::Matchmaker(std::shared_ptr<const config::CardCatalog> catalog, std::shared_ptr<Runtime> runtime)
    : catalog_(std::move(catalog)), runtime_(std::move(runtime)) {}

void Matchmaker::enqueue(const std::shared_ptr<Session>& session) {
  std::shared_ptr<Session> opponent;
  std::lock_guard<std::mutex> lock(mu_);
  if (runtime_->shutdown.stopping()) return;
  if (queued_.count(session.get()) != 0) return;
  while (!q_.empty() && !opponent) {
    auto candidate = q_.front().lock();
    q_.pop();
    if (!candidate) continue;
    queued_.erase(candidate.get());
    if (candidate == session) continue;
    opponent = std::move(candidate);
  }
  if (!opponent) {
    q_.push(session);
    queued_.insert(session.get());
    return;
  }
  auto room = std::make_shared<Room>(opponent, session, catalog_, runtime_);
  runtime_->counters.active_rooms.fetch_add(1);
  opponent->set_room(room);
  session->set_room(room);
  room->assign_token(0, opponent->token(), opponent->user());
  room->assign_token(1, session->token(), session->user());
  room->start();
  room->launch_actor();
}

void Matchmaker::stop() {
  std::lock_guard<std::mutex> lock(mu_);
  q_ = {};
  queued_.clear();
}

void Matchmaker::cancel(const std::shared_ptr<Session>& session) {
  std::lock_guard<std::mutex> lock(mu_);
  queued_.erase(session.get());
  std::queue<std::weak_ptr<Session>> remaining;
  while (!q_.empty()) {
    auto candidate = q_.front();
    q_.pop();
    if (candidate.lock() != session) remaining.push(candidate);
  }
  q_.swap(remaining);
}

}
