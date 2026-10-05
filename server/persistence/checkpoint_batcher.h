#pragma once

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <exception>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace arena::persistence {

enum class RoomRecoveryWriteKind { FullSnapshot, Snapshot, Tail };

struct RoomRecoveryWrite {
  RoomRecoveryWriteKind kind = RoomRecoveryWriteKind::FullSnapshot;
  std::uint64_t expected_sequence = 0;
  std::uint64_t sequence = 0;
  std::string payload;
};

struct CheckpointBatchResult {
  bool ok = false;
  std::string error;
  unsigned long long queue_wait_us = 0;
  unsigned long long lock_wait_us = 0;
  unsigned long long connection_us = 0;
  unsigned long long sql_us = 0;
  unsigned long long commit_us = 0;
  unsigned int batch_count = 1;
  bool batch_leader = false;
};

class CheckpointBatcher {
 public:
  using Clock = std::chrono::steady_clock;
  struct Request {
    std::string match_id;
    std::string checkpoint;
    RoomRecoveryWriteKind write_kind = RoomRecoveryWriteKind::FullSnapshot;
    std::uint64_t expected_sequence = 0;
    std::uint64_t sequence = 0;
    bool sequenced = false;
    Clock::time_point queued_at;
    Clock::time_point started_at;
    std::promise<CheckpointBatchResult> completion;
  };
  using Batch = std::vector<std::shared_ptr<Request>>;
  using Handler = std::function<CheckpointBatchResult(const Batch&)>;

  static constexpr std::size_t kMaximumCount = 32;
  static constexpr std::size_t kMaximumQueuedCount = 128;
  static constexpr std::size_t kMaximumQueuedBytes = 16 * 1024 * 1024;
  static constexpr std::size_t kMaximumBatchBytes = 1024 * 1024;
  static constexpr std::chrono::milliseconds kGatherWait{2};

  CheckpointBatcher(std::size_t max_count, Handler handler)
      : max_count_(std::clamp(max_count, std::size_t{1}, kMaximumCount)),
        handler_(std::move(handler)), worker_([this] { run(); }) {}

  CheckpointBatcher(const CheckpointBatcher&) = delete;
  CheckpointBatcher& operator=(const CheckpointBatcher&) = delete;

  ~CheckpointBatcher() { stop(); }

  CheckpointBatchResult submit(const std::string& match_id, const std::string& checkpoint) {
    return enqueue(match_id, checkpoint, RoomRecoveryWriteKind::FullSnapshot, 0, 0, false);
  }

  CheckpointBatchResult submit(const std::string& match_id, const RoomRecoveryWrite& write) {
    return enqueue(match_id, write.payload, write.kind, write.expected_sequence, write.sequence, true);
  }

 private:
  CheckpointBatchResult enqueue(const std::string& match_id, const std::string& checkpoint,
                                RoomRecoveryWriteKind kind, std::uint64_t expected_sequence,
                                std::uint64_t sequence, bool sequenced) {
    const auto queued_at = Clock::now();
    if (checkpoint.size() > kMaximumQueuedBytes)
      return failed("checkpoint exceeds batch queue byte limit");
    std::unique_lock<std::mutex> lock(mutex_);
    space_available_.wait(lock, [&] {
      return stopping_ || (requests_.size() < kMaximumQueuedCount &&
                          checkpoint.size() <= kMaximumQueuedBytes - queued_bytes_);
    });
    if (stopping_) return failed("checkpoint batcher is stopping");
    auto request = std::make_shared<Request>();
    request->match_id = match_id;
    request->checkpoint = checkpoint;
    request->write_kind = kind;
    request->expected_sequence = expected_sequence;
    request->sequence = sequence;
    request->sequenced = sequenced;
    request->queued_at = queued_at;
    auto result = request->completion.get_future();
    requests_.push_back(std::move(request));
    queued_bytes_ += checkpoint.size();
    lock.unlock();
    work_available_.notify_one();
    return result.get();
  }

 public:

  std::size_t queued_count() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return requests_.size();
  }

  // Accepted requests finish before shutdown returns. The handler must not
  // synchronously submit to, or stop, its own single-worker batcher.
  void stop() {
    std::lock_guard<std::mutex> stop_lock(stop_mutex_);
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    work_available_.notify_all();
    space_available_.notify_all();
    if (worker_.joinable()) worker_.join();
  }

 private:
  static CheckpointBatchResult failed(std::string error) {
    CheckpointBatchResult result;
    result.error = std::move(error);
    return result;
  }

  void run() {
    for (;;) {
      Batch batch;
      batch.reserve(max_count_);
      {
        std::unique_lock<std::mutex> lock(mutex_);
        work_available_.wait(lock, [&] { return stopping_ || !requests_.empty(); });
        if (requests_.empty()) return;
        const auto deadline = Clock::now() + kGatherWait;
        std::size_t batch_bytes = 0;
        for (;;) {
          if (!requests_.empty()) {
            const auto bytes = requests_.front()->checkpoint.size();
            if (!batch.empty() && bytes > kMaximumBatchBytes - batch_bytes) break;
            batch.push_back(std::move(requests_.front()));
            requests_.pop_front();
            queued_bytes_ -= bytes;
            batch_bytes += bytes;
            space_available_.notify_all();
            // A valid document larger than the batch byte limit travels alone.
            if (batch.size() == max_count_ || batch_bytes >= kMaximumBatchBytes) break;
            continue;
          }
          if (stopping_ || Clock::now() >= deadline) break;
          work_available_.wait_until(lock, deadline, [&] { return stopping_ || !requests_.empty(); });
        }
      }
      const auto started_at = Clock::now();
      for (const auto& request : batch) request->started_at = started_at;
      CheckpointBatchResult result;
      try {
        result = handler_(batch);
      } catch (const std::exception& error) {
        result = failed(std::string("checkpoint batch handler failed: ") + error.what());
      } catch (...) {
        result = failed("checkpoint batch handler failed with an unknown exception");
      }
      result.batch_count = static_cast<unsigned int>(batch.size());
      for (std::size_t index = 0; index < batch.size(); ++index) {
        result.queue_wait_us = static_cast<unsigned long long>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                started_at - batch[index]->queued_at).count());
        result.batch_leader = index == 0;
        batch[index]->completion.set_value(result);
      }
    }
  }

  const std::size_t max_count_;
  Handler handler_;
  mutable std::mutex mutex_;
  std::mutex stop_mutex_;
  std::condition_variable work_available_;
  std::condition_variable space_available_;
  std::deque<std::shared_ptr<Request>> requests_;
  std::size_t queued_bytes_ = 0;
  bool stopping_ = false;
  std::thread worker_;
};

}  // namespace arena::persistence
