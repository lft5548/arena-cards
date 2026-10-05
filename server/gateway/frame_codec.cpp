#include "server/gateway/frame_codec.h"

#include <vector>

#include "proto/messages.h"

namespace arena::gateway {

FrameResult receive_frame(Socket socket, Frame& frame, const std::atomic<bool>* cancelled,
                          std::chrono::steady_clock::time_point deadline) {
  std::uint8_t header[4];
  if (!receive_all(socket, header, sizeof(header), cancelled, deadline)) return FrameResult::Disconnected;
  const auto length = proto::read_u32_be(header);
  if (length < 2 || length > kMaxFrameBody) return FrameResult::InvalidLength;
  std::vector<std::uint8_t> body(length);
  if (!receive_all(socket, body.data(), body.size(), cancelled, deadline)) return FrameResult::Disconnected;
  frame.message_id = proto::read_u16_be(body.data());
  frame.payload.assign(reinterpret_cast<const char*>(body.data() + 2), length - 2);
  return FrameResult::Ready;
}

}
