#include <chrono>
#include <cerrno>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "proto/messages.h"
#include "server/gateway/frame_codec.h"
#include "server/gateway/session.h"

namespace {

using arena::gateway::Socket;
using arena::gateway::kInvalidSocket;
using arena::proto::MessageType;

void require(bool condition, const char* message) {
  if (!condition) { std::cerr << "gateway test failed: " << message << "\n"; std::exit(1); }
}

struct SocketPair {
  Socket server;
  Socket client;
  SocketPair(Socket accepted, Socket connected) : server(accepted), client(connected) {}
  SocketPair(const SocketPair&) = delete;
  ~SocketPair() {
    arena::gateway::close_socket(server);
    arena::gateway::close_socket(client);
  }
  Socket take_server() { const auto result = server; server = kInvalidSocket; return result; }
};

SocketPair connect_pair() {
  const auto listener = ::socket(AF_INET, SOCK_STREAM, 0);
  require(listener != kInvalidSocket, "create listener");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  require(bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0 &&
          listen(listener, 4) == 0, "listen on ephemeral port");
  arena::gateway::SocketLength length = sizeof(address);
  require(getsockname(listener, reinterpret_cast<sockaddr*>(&address), &length) == 0, "resolve port");
  const auto client = ::socket(AF_INET, SOCK_STREAM, 0);
  require(client != kInvalidSocket && connect(client, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0,
          "connect peer");
  const auto accepted = accept(listener, nullptr, nullptr);
  require(accepted != kInvalidSocket, "accept peer");
  arena::gateway::close_socket(listener);
#ifdef _WIN32
  const DWORD timeout = 3000;
#else
  const timeval timeout{3, 0};
#endif
  require(setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout)) == 0,
          "bound test receive duration");
  return SocketPair(accepted, client);
}

void frame_checks() {
  using arena::gateway::Frame;
  using arena::gateway::FrameResult;
  using arena::gateway::receive_frame;
  {
    auto sockets = connect_pair();
    const auto text = arena::proto::encode(MessageType::LoginReq, "split-user");
    const std::string binary("\0\xff\x01", 3);
    const auto next = arena::proto::encode(arena::proto::ProtoMessageType::LoginReq, binary);
    std::thread producer([&]() {
      require(arena::gateway::send_all(sockets.client, text.data(), 3), "partial header");
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
      std::vector<std::uint8_t> coalesced(text.begin() + 3, text.end());
      coalesced.insert(coalesced.end(), next.begin(), next.end());
      require(arena::gateway::send_all(sockets.client, coalesced.data(), coalesced.size()), "coalesced messages");
    });
    Frame frame;
    require(receive_frame(sockets.server, frame) == FrameResult::Ready && frame.message_id == 1 &&
            frame.payload == "split-user", "partial frame reconstruction");
    require(receive_frame(sockets.server, frame) == FrameResult::Ready && frame.message_id == 0x4001 &&
            frame.payload == binary, "binary frame does not undergo text decoding");
    producer.join();
  }
  {
    auto sockets = connect_pair();
    const auto empty = arena::proto::encode(MessageType::Heartbeat, "");
    require(arena::gateway::send_all(sockets.client, empty.data(), empty.size()), "empty payload send");
    Frame frame;
    require(receive_frame(sockets.server, frame) == FrameResult::Ready && frame.payload.empty(), "empty payload");
    const std::string payload(arena::gateway::kMaxFrameBody - 2, 'x');
    const auto maximum = arena::proto::encode(MessageType::LoginReq, payload);
    std::thread producer([&]() {
      require(arena::gateway::send_all(sockets.client, maximum.data(), maximum.size()), "maximum frame send");
    });
    require(receive_frame(sockets.server, frame) == FrameResult::Ready && frame.payload == payload,
            "maximum allowed frame");
    producer.join();
  }
  for (const std::uint32_t invalid : {0u, 1u, 65537u, 0xffffffffu}) {
    auto sockets = connect_pair();
    std::uint8_t header[4];
    arena::proto::write_u32_be(header, invalid);
    require(arena::gateway::send_all(sockets.client, header, sizeof(header)), "invalid header send");
    Frame frame;
    require(receive_frame(sockets.server, frame) == FrameResult::InvalidLength, "reject invalid length before allocation");
  }
  {
    auto sockets = connect_pair();
    const std::uint8_t incomplete[]{0, 0};
    require(arena::gateway::send_all(sockets.client, incomplete, sizeof(incomplete)), "partial header send");
    arena::gateway::shutdown_socket(sockets.client);
    Frame frame;
    require(receive_frame(sockets.server, frame) == FrameResult::Disconnected, "partial header disconnect");
  }
  {
    auto sockets = connect_pair();
    const std::uint8_t incomplete[]{0, 0, 0, 5, 0, 1, 'a'};
    require(arena::gateway::send_all(sockets.client, incomplete, sizeof(incomplete)), "partial body send");
    arena::gateway::shutdown_socket(sockets.client);
    Frame frame;
    require(receive_frame(sockets.server, frame) == FrameResult::Disconnected, "partial body disconnect");
  }
  {
    auto sockets = connect_pair();
    require(arena::gateway::set_nonblocking(sockets.server), "nonblocking cancellation socket");
    std::atomic<bool> cancelled{false};
    std::thread canceller([&]() {
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
      cancelled.store(true, std::memory_order_release);
    });
    Frame frame;
    require(receive_frame(sockets.server, frame, &cancelled) == FrameResult::Disconnected,
            "idle read observes cancellation without closing descriptor");
    canceller.join();
  }
  {
    auto sockets = connect_pair();
    require(arena::gateway::set_nonblocking(sockets.server), "nonblocking write cancellation socket");
    const int small_buffer = 4096;
    require(setsockopt(sockets.client, SOL_SOCKET, SO_RCVBUF,
                      reinterpret_cast<const char*>(&small_buffer), sizeof(small_buffer)) == 0 &&
            setsockopt(sockets.server, SOL_SOCKET, SO_SNDBUF,
                       reinterpret_cast<const char*>(&small_buffer), sizeof(small_buffer)) == 0,
            "bound socket buffers for stalled peer");
    const std::vector<std::uint8_t> payload(65536, 'x');
    bool saturated = false;
    for (int attempt = 0; attempt < 2048 && !saturated; ++attempt) {
#ifdef _WIN32
      const int sent = ::send(sockets.server, reinterpret_cast<const char*>(payload.data()),
                              static_cast<int>(payload.size()), 0);
      saturated = sent < 0 && WSAGetLastError() == WSAEWOULDBLOCK;
#else
      const auto sent = ::send(sockets.server, payload.data(), payload.size(), MSG_NOSIGNAL);
      saturated = sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK);
#endif
      require(sent > 0 || saturated, "fill pending writer data");
    }
    require(saturated, "stalled peer reaches would-block before cancellation");
    std::atomic<bool> cancelled{false};
    std::thread canceller([&]() {
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
      cancelled.store(true, std::memory_order_release);
    });
    require(!arena::gateway::send_all(sockets.server, payload.data(), payload.size(), &cancelled),
            "stalled writer observes cancellation without descriptor reuse");
    canceller.join();
  }
}

class Handler : public arena::gateway::SessionHandler {
 public:
  std::weak_ptr<arena::gateway::Session> connection;
  std::mutex mutex;
  std::condition_variable changed;
  int requests = 0;
  int disconnected = 0;
  void on_connected(const std::shared_ptr<arena::gateway::Session>& session) override { connection = session; }
  void on_request(MessageType type, const std::string& payload, const std::string& request_id) override {
    require(type == MessageType::LoginReq && payload == "callback-user" && request_id.empty(), "application dispatch");
    {
      std::lock_guard<std::mutex> lock(mutex);
      ++requests;
    }
    if (auto session = connection.lock()) session->send(MessageType::LoginResp, "ok=1;user=callback-user");
    changed.notify_all();
  }
  void on_disconnected() override {
    {
      std::lock_guard<std::mutex> lock(mutex);
      ++disconnected;
    }
    changed.notify_all();
  }
  void wait_disconnected(const char* context) {
    std::unique_lock<std::mutex> lock(mutex);
    require(changed.wait_for(lock, std::chrono::seconds(3), [this]() { return disconnected != 0; }),
            context);
    require(disconnected == 1, "exactly one disconnect callback");
  }
};

void wait_destroyed(arena::gateway::Metrics& metrics) {
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  while (metrics.active_sessions.load() != 0 && std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  require(metrics.active_sessions.load() == 0, "transport lifetime does not leak");
}

void session_checks() {
  {
    auto sockets = connect_pair();
    arena::gateway::Metrics metrics;
    auto handler = std::make_shared<Handler>();
    auto session = std::make_shared<arena::gateway::Session>(sockets.take_server(), handler, metrics);
    for (int sequence = 1; sequence <= 3; ++sequence)
      session->send(MessageType::BattleEvent, "sequence=" + std::to_string(sequence));
    session->start();
    for (int sequence = 1; sequence <= 3; ++sequence) {
      arena::gateway::Frame frame;
      require(arena::gateway::receive_frame(sockets.client, frame) == arena::gateway::FrameResult::Ready &&
              frame.message_id == 8 && frame.payload == "sequence=" + std::to_string(sequence), "FIFO writer");
    }
    const auto login = arena::proto::encode(MessageType::LoginReq, "callback-user");
    require(arena::gateway::send_all(sockets.client, login.data(), login.size()), "callback request");
    arena::gateway::Frame response;
    require(arena::gateway::receive_frame(sockets.client, response) == arena::gateway::FrameResult::Ready &&
            response.message_id == 2, "application callback response");
    const auto heartbeat = arena::proto::encode(MessageType::Heartbeat, "");
    require(arena::gateway::send_all(sockets.client, heartbeat.data(), heartbeat.size()), "heartbeat send");
    require(arena::gateway::receive_frame(sockets.client, response) == arena::gateway::FrameResult::Ready &&
            response.message_id == 11, "heartbeat is transport-local");
    session->close();
    session->close();
    handler->wait_disconnected("explicit close did not wake the reader");
    require(handler->requests == 1 && metrics.send_frames_enqueued.load() == 5 &&
            metrics.send_frames_dropped.load() == 0, "unchanged transport counters");
    session->send(MessageType::Pong, "closed");
    require(metrics.send_frames_enqueued.load() == 5, "closed sends ignored");
    session.reset();
    wait_destroyed(metrics);
    require(handler->connection.expired(), "handler weak reference breaks ownership cycle");
  }
  for (const bool frame_limit : {false, true}) {
    auto sockets = connect_pair();
    arena::gateway::Metrics metrics;
    auto handler = std::make_shared<Handler>();
    arena::gateway::SessionOptions options;
    options.max_frames = frame_limit ? 2 : 128;
    options.max_bytes = frame_limit ? 4096 : 12;
    auto session = std::make_shared<arena::gateway::Session>(sockets.take_server(), handler, metrics, options);
    if (frame_limit) {
      session->send(MessageType::Pong, "1");
      session->send(MessageType::Pong, "2");
      session->send(MessageType::Pong, "3");
    } else session->send(MessageType::Pong, "payload-larger-than-limit");
    require(session->closed() && metrics.send_frames_dropped.load() == 1, "bounded queue closes slow transport");
    session->start();
    handler->wait_disconnected(frame_limit ? "frame-limit close callback missing" : "byte-limit close callback missing");
    session.reset();
    wait_destroyed(metrics);
    require(handler->requests == 0, "queue rejection never reaches game rules");
  }
}

}

int main() {
  require(arena::gateway::initialize_sockets(), "socket initialization");
  frame_checks();
  session_checks();
  std::cout << "gateway framing/FIFO/callback/backpressure/lifetime checks passed\n";
  return 0;
}
