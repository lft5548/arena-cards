#pragma once

#include <algorithm>
#include <chrono>

namespace arena::persistence {

class RetryBackoff {
 public:
  using Clock = std::chrono::steady_clock;
  using TimePoint = Clock::time_point;

  explicit RetryBackoff(unsigned int initial_seconds = 1, unsigned int maximum_seconds = 30)
      : initial_seconds_(std::max(1u, initial_seconds)),
        maximum_seconds_(std::max(initial_seconds_, maximum_seconds)),
        current_seconds_(initial_seconds_) {}

  void reset() {
    current_seconds_ = initial_seconds_;
    next_attempt_ = TimePoint{};
  }

  void fail(TimePoint now) {
    next_attempt_ = now + std::chrono::seconds(current_seconds_);
    current_seconds_ = std::min(maximum_seconds_, current_seconds_ * 2);
  }

  bool ready(TimePoint now) const { return now >= next_attempt_; }

  unsigned int current_delay_seconds() const { return current_seconds_; }

  TimePoint next_attempt() const { return next_attempt_; }

 private:
  unsigned int initial_seconds_;
  unsigned int maximum_seconds_;
  unsigned int current_seconds_;
  TimePoint next_attempt_{};
};

}  // namespace arena::persistence
