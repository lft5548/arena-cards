#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace arena {
class Room;

// Own the existing actor/storage threads so shutdown can join their completion.
// Completed tasks are reaped on new work, rather than retaining a match history.
class ShutdownTracker {
 public:
  using Clock = std::chrono::steady_clock;
  ~ShutdownTracker() {
    // Runtime ownership must outlive all tasks. Main explicitly drains before
    // destroying services; the destructor joins already-finished handles.
    for (auto& task : tasks_) if (task.thread.joinable()) {
      if (task.thread.get_id() == std::this_thread::get_id()) task.thread.detach();
      else task.thread.join();
    }
  }
  bool stopping() const { return stopping_.load(std::memory_order_acquire); }
  void begin() { stopping_.store(true, std::memory_order_release); }

  void launch(std::function<void()> work, const std::shared_ptr<Room>& room = {}) {
    std::lock_guard<std::mutex> lock(mutex_);
    reap_locked();
    for (auto it = rooms_.begin(); it != rooms_.end();) {
      if (it->expired()) it = rooms_.erase(it); else ++it;
    }
    if (room) rooms_.push_back(room);
    auto done = std::make_shared<std::atomic<bool>>(false);
    // Allocate before starting the thread, so allocation failure cannot destroy
    // a joinable temporary thread and terminate the process.
    tasks_.push_back({{}, done});
    try {
      tasks_.back().thread = std::thread([this, done, work = std::move(work)]() {
        try { work(); }
        catch (const std::exception& error) {
          failed_.store(true);
          std::cerr << "application task failed: " << error.what() << "\n";
        } catch (...) { failed_.store(true); }
        {
          std::lock_guard<std::mutex> complete_lock(mutex_);
          done->store(true, std::memory_order_release);
        }
        changed_.notify_all();
      });
    } catch (...) { tasks_.pop_back(); throw; }
  }

  std::vector<std::shared_ptr<Room>> rooms() {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<std::shared_ptr<Room>> result;
    for (auto& weak : rooms_) if (auto room = weak.lock()) result.push_back(std::move(room));
    return result;
  }

  bool drain_until(Clock::time_point deadline) {
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
      reap_locked();
      if (tasks_.empty()) return !failed_.load();
      if (Clock::now() >= deadline) return false;
      changed_.wait_until(lock, deadline);
    }
  }
  void fail() { failed_.store(true); }

 private:
  struct Task { std::thread thread; std::shared_ptr<std::atomic<bool>> done; };
  void reap_locked() {
    for (auto it = tasks_.begin(); it != tasks_.end();) {
      if (it->done->load(std::memory_order_acquire)) {
        it->thread.join();
        it = tasks_.erase(it);
      } else ++it;
    }
  }
  std::atomic<bool> stopping_{false};
  std::atomic<bool> failed_{false};
  std::mutex mutex_;
  std::condition_variable changed_;
  std::vector<Task> tasks_;
  std::vector<std::weak_ptr<Room>> rooms_;
};

}  // namespace arena
