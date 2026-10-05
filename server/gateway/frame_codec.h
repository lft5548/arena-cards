#pragma once

#include <cstdint>
#include <string>

#include "server/gateway/socket.h"

namespace arena::gateway {

constexpr std::uint32_t kMaxFrameBody = 65536;

struct Frame {
  std::uint16_t message_id = 0;
  std::string payload;
};

enum class FrameResult { Ready, Disconnected, InvalidLength };

// One absolute deadline covers both header and body; partial progress cannot
// extend a Session's inbound-liveness budget.
FrameResult receive_frame(Socket socket, Frame& frame, const std::atomic<bool>* cancelled = nullptr,
                          std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max());

}
