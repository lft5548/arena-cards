#include <chrono>
#include <iostream>
#include <stdexcept>
#include <utility>

#include "server/gateway/request_limiter.h"

namespace {
void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}
}

int main() {
  using arena::gateway::RequestLimiter;
  using namespace std::chrono_literals;
  const RequestLimiter::Clock::time_point start{};
  try {
    RequestLimiter limiter(8, 3, start);
    require(limiter.consume(start) && limiter.consume(start) && limiter.consume(start), "initial burst");
    require(!limiter.consume(start), "exhausted burst rejects");
    require(!limiter.consume(start + 62500us), "fractional credit cannot accept a frame");
    require(limiter.consume(start + 125ms), "fractional credit retained across attempts");
    require(!limiter.consume(start + 125ms), "refill spent once");
    require(!limiter.consume(start), "backwards time grants no tokens");
    require(limiter.consume(start + 10s) && limiter.consume(start + 10s) && limiter.consume(start + 10s), "idle refill");
    require(!limiter.consume(start + 10s), "idle credit capped at burst");
    RequestLimiter independent(8, 1, start);
    require(independent.consume(start) && !independent.consume(start), "independent connection budget");
    int rejected = 0;
    for (const auto& settings : {std::pair<unsigned int, unsigned int>{0, 1}, {1, 0}, {10001, 1}, {1, 20001}}) {
      try { RequestLimiter invalid(settings.first, settings.second, start); }
      catch (const std::invalid_argument&) { ++rejected; }
    }
    require(rejected == 4, "reject unsupported explicit limits");
    std::cout << "request limiter burst/refill/clock/isolation/config checks passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\n";
    return 1;
  }
}
