#include <cassert>
#include <chrono>

#include "server/persistence/retry_backoff.h"

int main() {
  using Backoff = arena::persistence::RetryBackoff;
  const auto start = Backoff::TimePoint{};
  Backoff backoff;

  assert(backoff.ready(start));
  backoff.fail(start);
  assert(!backoff.ready(start + std::chrono::milliseconds(999)));
  assert(backoff.ready(start + std::chrono::seconds(1)));
  backoff.fail(start + std::chrono::seconds(1));
  assert(!backoff.ready(start + std::chrono::milliseconds(2999)));
  assert(backoff.ready(start + std::chrono::seconds(3)));

  for (int failure = 0; failure < 8; ++failure) {
    backoff.fail(start + std::chrono::seconds(100 + failure));
  }
  assert(backoff.current_delay_seconds() == 30);
  backoff.reset();
  assert(backoff.ready(start));
  assert(backoff.current_delay_seconds() == 1);
  return 0;
}
