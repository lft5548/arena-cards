#pragma once

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <stdexcept>

namespace arena::gateway {

// Reader-thread owned. Every complete incoming frame costs one token, before
// negotiation, decoding or application dispatch; partial refills are retained.
class RequestLimiter {
 public:
  using Clock = std::chrono::steady_clock;
  RequestLimiter(std::uint32_t per_second, std::uint32_t burst,
                 Clock::time_point now = Clock::now())
      : per_second_(per_second), burst_(burst), tokens_(burst), updated_(now) {
    if (per_second == 0 || per_second > 10000 || burst == 0 || burst > 20000)
      throw std::invalid_argument("request limits are outside the supported range");
  }

  bool consume(Clock::time_point now = Clock::now()) {
    if (now > updated_) {
      const double elapsed = std::chrono::duration<double>(now - updated_).count();
      tokens_ = std::min<double>(burst_, tokens_ + elapsed * per_second_);
      updated_ = now;
    }
    if (tokens_ < 1.0) return false;
    tokens_ -= 1.0;
    return true;
  }

 private:
  std::uint32_t per_second_;
  std::uint32_t burst_;
  double tokens_;
  Clock::time_point updated_;
};

}  // namespace arena::gateway
