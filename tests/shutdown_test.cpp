#include <atomic>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

#include "proto/messages.h"
#include "server/gateway/frame_codec.h"
#include "server/gateway/tcp_server.h"

namespace {
using namespace std::chrono_literals;
using arena::gateway::Socket;
using arena::gateway::kInvalidSocket;
using Clock = std::chrono::steady_clock;

void require(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

std::uint16_t free_port() {
  const auto probe = ::socket(AF_INET, SOCK_STREAM, 0);
  require(probe != kInvalidSocket, "port probe socket");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  arena::gateway::SocketLength size = sizeof(address);
  const bool ok = ::bind(probe, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0 &&
      ::getsockname(probe, reinterpret_cast<sockaddr*>(&address), &size) == 0;
  arena::gateway::close_socket(probe);
  require(ok, "ephemeral port probe");
  return ntohs(address.sin_port);
}

Socket connect_to(std::uint16_t port) {
  const auto socket = ::socket(AF_INET, SOCK_STREAM, 0);
  if (socket == kInvalidSocket) return kInvalidSocket;
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  address.sin_port = htons(port);
  if (::connect(socket, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
    arena::gateway::close_socket(socket);
    return kInvalidSocket;
  }
  return socket;
}

struct Peer {
  Socket socket = kInvalidSocket;
  ~Peer() { arena::gateway::close_socket(socket); }
};

class Handler : public arena::gateway::SessionHandler {
 public:
  void on_connected(const std::shared_ptr<arena::gateway::Session>& session) override {
    std::lock_guard<std::mutex> lock(mutex);
    connection = session;
    connected = true;
    cv.notify_all();
  }
  void on_request(arena::proto::MessageType, const std::string&, const std::string&) override {
    std::unique_lock<std::mutex> lock(mutex);
    entered = true;
    cv.notify_all();
    if (blocked) cv.wait(lock, [this] { return released; });
  }
  void on_disconnected() override {
    if (!frozen.load()) disconnect_before_freeze.store(true);
    disconnects.fetch_add(1);
  }
  bool wait_connected() {
    std::unique_lock<std::mutex> lock(mutex);
    return cv.wait_for(lock, 2s, [this] { return connected; });
  }
  bool wait_entered() {
    std::unique_lock<std::mutex> lock(mutex);
    return cv.wait_for(lock, 2s, [this] { return entered; });
  }
  bool transport_open() {
    std::lock_guard<std::mutex> lock(mutex);
    const auto session = connection.lock();
    return session && !session->closed();
  }
  void release() {
    std::lock_guard<std::mutex> lock(mutex);
    released = true;
    cv.notify_all();
  }
  std::atomic<bool> frozen{false};
  std::atomic<bool> disconnect_before_freeze{false};
  std::atomic<int> disconnects{0};
  bool blocked = false;
 private:
  std::mutex mutex;
  std::condition_variable cv;
  std::weak_ptr<arena::gateway::Session> connection;
  bool connected = false;
  bool entered = false;
  bool released = false;
};

class RunningServer {
 public:
  RunningServer(const std::shared_ptr<Handler>& handler,
                arena::gateway::TcpServer::ShutdownCallback callback,
                std::chrono::milliseconds timeout)
      : handler_(handler), port(free_port()),
        server_(port, "127.0.0.1", [handler] { return handler; }, metrics, arena::gateway::SessionOptions{}),
        thread_([this, callback, timeout] {
          result.store(server_.run([this] { return stopping.load(); }, callback, timeout));
        }) {}
  ~RunningServer() {
    handler_->release();
    stop();
    const auto deadline = Clock::now() + 2s;
    while (metrics.active_sessions.load() != 0 && Clock::now() < deadline)
      std::this_thread::sleep_for(1ms);
    if (metrics.active_sessions.load() != 0) std::terminate();
  }
  void connect(Peer& peer) {
    const auto deadline = Clock::now() + 2s;
    while (Clock::now() < deadline) {
      peer.socket = connect_to(port);
      if (peer.socket != kInvalidSocket) {
        require(arena::gateway::set_nonblocking(peer.socket), "nonblocking client");
        return;
      }
      std::this_thread::sleep_for(5ms);
    }
    throw std::runtime_error("listener did not start");
  }
  void stop() {
    stopping.store(true);
    if (thread_.joinable()) thread_.join();
  }
  arena::gateway::Metrics metrics;
  std::shared_ptr<Handler> handler_;
  std::uint16_t port;
  std::atomic<bool> stopping{false};
  std::atomic<int> result{-1};
 private:
  arena::gateway::TcpServer server_;
  std::thread thread_;
};

void live_transport_drains() {
  auto handler = std::make_shared<Handler>();
  std::atomic<int> callbacks{0};
  bool open_at_freeze = false;
  Clock::time_point deadline;
  RunningServer server(handler, [&](Clock::time_point value) {
    deadline = value;
    open_at_freeze = handler->transport_open();
    handler->frozen.store(true);
    callbacks.fetch_add(1);
  }, 1s);
  Peer peer;
  server.connect(peer);
  require(handler->wait_connected(), "live transport connected");
  server.stop();
  require(server.result.load() == 0 && server.metrics.active_sessions.load() == 0,
          "stop drains reader/writer/captures/socket before return");
  require(callbacks.load() == 1 && open_at_freeze && deadline > Clock::now() &&
              handler->disconnects.load() == 1 && !handler->disconnect_before_freeze.load(),
          "app freeze precedes transport close and exactly one disconnect");
  const auto replacement = connect_to(server.port);
  arena::gateway::close_socket(replacement);
  require(replacement == kInvalidSocket, "stopped listener rejects new connections");
  arena::gateway::Frame frame;
  require(arena::gateway::receive_frame(peer.socket, frame, nullptr, Clock::now() + 1s) ==
              arena::gateway::FrameResult::Disconnected, "live client observes shutdown");
}

void empty_listener_stops() {
  auto handler = std::make_shared<Handler>();
  std::atomic<int> callbacks{0};
  RunningServer server(handler, [&](Clock::time_point) { callbacks.fetch_add(1); }, 1s);
  server.stop();
  require(server.result.load() == 0 && callbacks.load() == 1 &&
              server.metrics.active_sessions.load() == 0, "empty listener stops without an accept wakeup");
}

void blocked_callback_respects_shared_deadline() {
  auto handler = std::make_shared<Handler>();
  handler->blocked = true;
  RunningServer server(handler, [&](Clock::time_point) { handler->frozen.store(true); }, 80ms);
  Peer peer;
  server.connect(peer);
  const auto login = arena::proto::encode(arena::proto::MessageType::LoginReq, "blocked");
  require(arena::gateway::send_all(peer.socket, login.data(), login.size()), "send blocked callback request");
  require(handler->wait_entered(), "blocked request callback entered");
  const auto start = Clock::now();
  server.stop();
  const auto elapsed = Clock::now() - start;
  handler->release(); // Always release before assertions or fixture destruction.
  require(server.result.load() == 2 && elapsed < 600ms,
          "blocked callback returns deadline status within one global budget");
  const auto deadline = Clock::now() + 1s;
  while (server.metrics.active_sessions.load() != 0 && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  require(server.metrics.active_sessions.load() == 0 && handler->disconnects.load() == 1 &&
              !handler->disconnect_before_freeze.load(), "released callback safely drains after deadline");
}
}  // namespace

int main() {
  try {
    require(arena::gateway::initialize_sockets(), "initialize sockets");
    live_transport_drains();
    empty_listener_stops();
    blocked_callback_respects_shared_deadline();
    std::cout << "Gateway shutdown: 3 focused cases passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
