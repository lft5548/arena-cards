#include <chrono>
#include <cstdlib>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "server/persistence/checkpoint_batcher.h"

namespace {
using arena::persistence::CheckpointBatcher;
using arena::persistence::CheckpointBatchResult;
using arena::persistence::RoomRecoveryWrite;
using arena::persistence::RoomRecoveryWriteKind;
using namespace std::chrono_literals;

void check(bool condition, const char* message) {
  if (!condition) {
    std::cerr << message << '\n';
    std::exit(1);
  }
}

template <typename Predicate> void wait_until(Predicate predicate, const char* message) {
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (!predicate()) {
    check(std::chrono::steady_clock::now() < deadline, message);
    std::this_thread::sleep_for(1ms);
  }
}

CheckpointBatchResult successful() {
  CheckpointBatchResult result;
  result.ok = true;
  result.sql_us = 500;
  result.commit_us = 1000;
  return result;
}

std::future<CheckpointBatchResult> submit(CheckpointBatcher& batcher, std::string id,
                                        std::string checkpoint = "document") {
  return std::async(std::launch::async,
                    [&batcher, id = std::move(id), checkpoint = std::move(checkpoint)] {
                      return batcher.submit(id, checkpoint);
                    });
}

CheckpointBatchResult completed(std::future<CheckpointBatchResult>& future) {
  check(future.wait_for(5s) == std::future_status::ready, "checkpoint caller was not completed");
  return future.get();
}

void coalescing_and_count_limit() {
  std::promise<void> entered;
  std::promise<void> release;
  auto gate = release.get_future();
  std::vector<std::size_t> batch_sizes;
  CheckpointBatcher batcher(3, [&](const CheckpointBatcher::Batch& batch) {
    batch_sizes.push_back(batch.size());
    if (batch.front()->match_id == "sentinel") {
      entered.set_value();
      gate.wait();
    }
    return successful();
  });
  auto sentinel = submit(batcher, "sentinel");
  check(entered.get_future().wait_for(5s) == std::future_status::ready, "handler did not start");
  std::vector<std::future<CheckpointBatchResult>> requests;
  for (int index = 0; index < 11; ++index)
    requests.push_back(submit(batcher, std::to_string(index)));
  wait_until([&] { return batcher.queued_count() == 11; }, "requests did not enter queue");
  check(sentinel.wait_for(0ms) == std::future_status::timeout,
        "checkpoint completed before its handler returned");
  for (auto& request : requests)
    check(request.wait_for(0ms) == std::future_status::timeout,
          "queued checkpoint completed before durable handling");
  release.set_value();
  check(completed(sentinel).ok, "sentinel failed");
  unsigned int leaders = 0;
  unsigned int coalesced = 0;
  for (auto& request : requests) {
    const auto result = completed(request);
    check(result.ok && result.error.empty(), "successful batch lost its result");
    check(result.sql_us == 500 && result.commit_us == 1000, "handler timings changed");
    check(result.queue_wait_us > 0 && result.batch_count <= 3, "queue timing or count is invalid");
    leaders += result.batch_leader ? 1 : 0;
    coalesced += result.batch_count > 1 ? 1 : 0;
  }
  batcher.stop();
  check(leaders == 4 && coalesced == 11, "queued requests were not coalesced into bounded batches");
  check(batch_sizes == std::vector<std::size_t>({1, 3, 3, 3, 2}), "batch count limit changed");
}

void batch_bytes_and_oversized_document() {
  std::promise<void> entered;
  std::promise<void> release;
  auto gate = release.get_future();
  std::vector<std::vector<std::size_t>> sizes;
  CheckpointBatcher batcher(32, [&](const CheckpointBatcher::Batch& batch) {
    std::vector<std::size_t> bytes;
    for (const auto& request : batch) bytes.push_back(request->checkpoint.size());
    sizes.push_back(std::move(bytes));
    if (batch.front()->match_id == "sentinel") {
      entered.set_value();
      gate.wait();
    }
    return successful();
  });
  auto sentinel = submit(batcher, "sentinel");
  check(entered.get_future().wait_for(5s) == std::future_status::ready, "handler did not start");
  const auto half = CheckpointBatcher::kMaximumBatchBytes / 2;
  auto first = submit(batcher, "first", std::string(half, 'a'));
  wait_until([&] { return batcher.queued_count() == 1; }, "first request not queued");
  auto second = submit(batcher, "second", std::string(half, 'b'));
  wait_until([&] { return batcher.queued_count() == 2; }, "second request not queued");
  auto oversized = submit(batcher, "oversized", std::string(2 * half + 1, 'c'));
  wait_until([&] { return batcher.queued_count() == 3; }, "oversized request not queued");
  auto tail = submit(batcher, "tail");
  wait_until([&] { return batcher.queued_count() == 4; }, "tail request not queued");
  release.set_value();
  check(completed(sentinel).ok && completed(first).ok && completed(second).ok &&
            completed(oversized).ok && completed(tail).ok,
        "byte-limited batch lost a checkpoint");
  batcher.stop();
  check(sizes.size() == 4 && sizes[1] == std::vector<std::size_t>({half, half}) &&
            sizes[2] == std::vector<std::size_t>({2 * half + 1}) && sizes[3].size() == 1,
        "batch byte boundary or oversized-document handling changed");
}

void queue_backpressure_and_stop() {
  std::promise<void> entered;
  std::promise<void> release;
  auto gate = release.get_future();
  CheckpointBatcher batcher(32, [&](const CheckpointBatcher::Batch& batch) {
    if (batch.front()->match_id == "sentinel") {
      entered.set_value();
      gate.wait();
    }
    return successful();
  });
  auto sentinel = submit(batcher, "sentinel");
  check(entered.get_future().wait_for(5s) == std::future_status::ready, "handler did not start");
  const std::string large(CheckpointBatcher::kMaximumQueuedBytes / 2, 'x');
  auto first = submit(batcher, "first", large);
  auto second = submit(batcher, "second", large);
  wait_until([&] { return batcher.queued_count() == 2; }, "queue byte budget was not filled");
  auto waiting = submit(batcher, "waiting", large);
  check(waiting.wait_for(20ms) == std::future_status::timeout && batcher.queued_count() == 2,
        "queue admitted data above its byte budget");
  auto stopping = std::async(std::launch::async, [&] { batcher.stop(); });
  const auto rejected = completed(waiting);
  check(!rejected.ok && !rejected.error.empty(), "shutdown did not reject blocked admission");
  check(stopping.wait_for(20ms) == std::future_status::timeout, "shutdown did not drain its handler");
  release.set_value();
  check(completed(sentinel).ok && completed(first).ok && completed(second).ok,
        "shutdown discarded an accepted checkpoint");
  check(stopping.wait_for(5s) == std::future_status::ready, "shutdown did not finish draining");
  stopping.get();
  check(!batcher.submit("after-stop", "document").ok, "stopped batcher accepted a request");
}

void handler_exception_completes_callers() {
  CheckpointBatcher batcher(8, [](const CheckpointBatcher::Batch&) -> CheckpointBatchResult {
    throw std::runtime_error("simulated SQL failure");
  });
  std::vector<std::future<CheckpointBatchResult>> requests;
  for (int index = 0; index < 16; ++index)
    requests.push_back(submit(batcher, std::to_string(index)));
  for (auto& request : requests) {
    const auto result = completed(request);
    check(!result.ok && result.error.find("simulated SQL failure") != std::string::npos,
          "handler exception did not fail every caller");
  }
  batcher.stop();
}

void mixed_recovery_operations_wait_for_the_same_commit() {
  std::promise<void> entered;
  std::promise<void> release;
  auto gate = release.get_future();
  std::vector<std::shared_ptr<CheckpointBatcher::Request>> observed;
  CheckpointBatcher batcher(16, [&](const CheckpointBatcher::Batch& batch) {
    if (batch.front()->match_id == "sentinel") {
      entered.set_value();
      gate.wait();
    } else observed.insert(observed.end(), batch.begin(), batch.end());
    return successful();
  });
  auto sentinel = submit(batcher, "sentinel");
  check(entered.get_future().wait_for(5s) == std::future_status::ready, "handler did not start");
  std::vector<std::future<CheckpointBatchResult>> pending;
  const std::vector<RoomRecoveryWrite> writes{
      {RoomRecoveryWriteKind::FullSnapshot, 12, 13, std::string("full\0binary", 11)},
      {RoomRecoveryWriteKind::Tail, 20, 21, "tail"},
      {RoomRecoveryWriteKind::Snapshot, 31, 32, "compact"}};
  for (std::size_t index = 0; index < writes.size(); ++index) {
    pending.push_back(std::async(std::launch::async, [&, index] {
      return batcher.submit("sequenced-" + std::to_string(index), writes[index]);
    }));
    wait_until([&] { return batcher.queued_count() == index + 1; }, "sequenced write did not queue");
  }
  for (auto& future : pending)
    check(future.wait_for(0ms) == std::future_status::timeout, "recovery write completed before COMMIT handler");
  release.set_value();
  check(completed(sentinel).ok, "sentinel failed");
  for (auto& future : pending) {
    const auto result = completed(future);
    check(result.ok && result.batch_count == writes.size(), "mixed writes did not share one batch result");
  }
  batcher.stop();
  check(observed.size() == writes.size(), "mixed batch lost a recovery operation");
  for (std::size_t index = 0; index < writes.size(); ++index)
    check(observed[index]->sequenced && observed[index]->write_kind == writes[index].kind &&
          observed[index]->expected_sequence == writes[index].expected_sequence &&
          observed[index]->sequence == writes[index].sequence && observed[index]->checkpoint == writes[index].payload,
          "batch queue changed recovery kind, sequence, or binary payload");
}
}  // namespace

int main() {
  coalescing_and_count_limit();
  batch_bytes_and_oversized_document();
  queue_backpressure_and_stop();
  handler_exception_completes_callers();
  mixed_recovery_operations_wait_for_the_same_commit();
  std::cout << "checkpoint batcher tests passed\n";
}
