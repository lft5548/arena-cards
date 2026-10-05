#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

#include "server/gateway/session.h"

namespace arena::gateway {

class TcpServer {
 public:
  using HandlerFactory = std::function<std::shared_ptr<SessionHandler>()>;
  using Clock = std::chrono::steady_clock;
  using StopPredicate = std::function<bool()>;
  using ShutdownCallback = std::function<void(Clock::time_point)>;
  TcpServer(std::uint16_t port, std::string bind_address, HandlerFactory factory,
            Metrics& metrics, SessionOptions options = SessionOptions::from_environment());
  // The callback starts app shutdown after accept stops, before transports close.
  // Its deadline is shared with transport and application draining.
  int run(StopPredicate stop_requested = {}, ShutdownCallback begin_shutdown = {},
          std::chrono::milliseconds shutdown_timeout = std::chrono::seconds(10));

 private:
  std::uint16_t port_;
  std::string bind_address_;
  HandlerFactory factory_;
  Metrics& metrics_;
  SessionOptions options_;
};

}
