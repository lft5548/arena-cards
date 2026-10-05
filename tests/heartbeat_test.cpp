#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

#include "proto/messages.h"
#include "server/gateway/frame_codec.h"
#include "server/gateway/session.h"

namespace {
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using arena::gateway::Socket;
using arena::gateway::kInvalidSocket;
using arena::proto::MessageType;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

struct Pair {
  Socket server = kInvalidSocket;
  Socket client = kInvalidSocket;
  Pair() {
    const auto listener = ::socket(AF_INET, SOCK_STREAM, 0);
    require(listener != kInvalidSocket, "create listener");
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(::bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0 &&
                ::listen(listener, 4) == 0, "listen");
    arena::gateway::SocketLength size = sizeof(address);
    require(::getsockname(listener, reinterpret_cast<sockaddr*>(&address), &size) == 0, "listener address");
    client = ::socket(AF_INET, SOCK_STREAM, 0);
    require(client != kInvalidSocket && ::connect(client, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0,
            "connect peer");
    server = ::accept(listener, nullptr, nullptr);
    arena::gateway::close_socket(listener);
    require(server != kInvalidSocket && arena::gateway::set_nonblocking(server) &&
                arena::gateway::set_nonblocking(client), "configure peer sockets");
  }
  ~Pair() { arena::gateway::close_socket(server); arena::gateway::close_socket(client); }
  Socket take_server() { const auto result = server; server = kInvalidSocket; return result; }
};

class Handler : public arena::gateway::SessionHandler {
 public:
  void on_connected(const std::shared_ptr<arena::gateway::Session>&) override {}
  void on_request(MessageType, const std::string&, const std::string&) override {
    ++requests;
    std::unique_lock<std::mutex> lock(mutex);
    entered = true;
    cv.notify_all();
    if (blocked) cv.wait(lock, [this] { return released; });
  }
  void on_disconnected() override { ++disconnects; }
  bool wait_entered(Clock::time_point deadline) {
    std::unique_lock<std::mutex> lock(mutex);
    return cv.wait_until(lock, deadline, [this] { return entered; });
  }
  void release() { std::lock_guard<std::mutex> lock(mutex); released = true; cv.notify_all(); }
  std::atomic<int> requests{0};
  std::atomic<int> disconnects{0};
  bool blocked = false;
 private:
  std::mutex mutex;
  std::condition_variable cv;
  bool entered = false;
  bool released = false;
};

void send(Socket client, MessageType type, const std::string& payload = {}) {
  const auto frame = arena::proto::encode(type, payload);
  require(arena::gateway::send_all(client, frame.data(), frame.size()), "send request");
}

void partial_frame_deadline(bool body) {
  Pair pair;
  const auto frame = arena::proto::encode(MessageType::LoginReq, "trickle");
  const auto initial = body ? std::size_t(5) : std::size_t(1);
  require(arena::gateway::send_all(pair.client, frame.data(), initial), "send partial frame");
  std::atomic<bool> done{false};
  std::thread producer([&] {
    for (std::size_t offset = initial; offset < frame.size() && !done.load(); ++offset) {
      std::this_thread::sleep_for(50ms);
      if (done.load()) return;
      if (!arena::gateway::send_all(pair.client, frame.data() + offset, 1)) return;
    }
  });
  arena::gateway::Frame received;
  const auto start = Clock::now();
  const auto result = arena::gateway::receive_frame(pair.server, received, nullptr, start + 120ms);
  done.store(true);
  producer.join();
  require(result == arena::gateway::FrameResult::Disconnected && Clock::now() - start < 350ms,
          "header and body partial bytes do not extend inbound deadline");
}

arena::gateway::SessionOptions options() {
  arena::gateway::SessionOptions value;
  value.heartbeat_timeout_ms = 200;
  return value;
}

void finish(std::shared_ptr<arena::gateway::Session>& session, arena::gateway::Metrics& metrics) {
  session->close();
  require(session->wait_idle(Clock::now() + 1s), "transport workers drain");
  session.reset();
  // wait_idle covers callback/loop completion; final worker-capture destruction
  // follows it. Keep the fixture Metrics alive until that ownership is gone.
  const auto deadline = Clock::now() + 1s;
  while (metrics.active_sessions.load() != 0 && Clock::now() < deadline) std::this_thread::sleep_for(5ms);
  require(metrics.active_sessions.load() == 0, "transport worker captures release session");
}

void heartbeat_refresh_and_outbound_silence() {
  Pair pair;
  arena::gateway::Metrics metrics;
  auto handler = std::make_shared<Handler>();
  auto session = std::make_shared<arena::gateway::Session>(pair.take_server(), handler, metrics, options());
  session->start();
  for (int i = 0; i < 3; ++i) {
    send(pair.client, MessageType::Heartbeat);
    arena::gateway::Frame response;
    require(arena::gateway::receive_frame(pair.client, response, nullptr, Clock::now() + 500ms) ==
                arena::gateway::FrameResult::Ready && response.message_id == static_cast<std::uint16_t>(MessageType::Pong),
            "valid heartbeat responds and refreshes inbound liveness");
    std::this_thread::sleep_for(100ms);
    require(!session->closed() && !session->check_heartbeat(), "heartbeats keep session live");
  }
  session->send(MessageType::BattleSnapshot, "outbound_only=1");
  std::this_thread::sleep_for(150ms);
  session->check_heartbeat();
  require(session->closed() && metrics.heartbeat_timeouts.load() == 1, "outbound response does not refresh idle deadline");
  require(!session->check_heartbeat() && session->wait_idle(Clock::now() + 1s), "timeout counted once and workers drain");
  require(handler->requests.load() == 0 && handler->disconnects.load() == 1, "heartbeat local; single disconnect callback");
  finish(session, metrics);
}

void blocked_callback_is_monitored_and_drained() {
  Pair pair;
  arena::gateway::Metrics metrics;
  auto handler = std::make_shared<Handler>();
  handler->blocked = true;
  auto session = std::make_shared<arena::gateway::Session>(pair.take_server(), handler, metrics, options());
  session->start();
  send(pair.client, MessageType::LoginReq, "blocked");
  require(handler->wait_entered(Clock::now() + 500ms), "request callback entered");
  std::this_thread::sleep_for(230ms);
  const bool expired = session->check_heartbeat();
  const bool idle_before_release = session->wait_idle(Clock::now() + 20ms);
  handler->release();  // Release even when an assertion fails, preserving worker ownership.
  require(expired && session->closed() && metrics.heartbeat_timeouts.load() == 1,
          "listener watcher can expire socket while reader callback blocks");
  require(!idle_before_release && session->wait_idle(Clock::now() + 1s),
          "drain deadline includes blocked callback and later completion");
  require(handler->disconnects.load() == 1, "blocked callback receives one disconnect");
  finish(session, metrics);
}

void valid_action_and_admin_wire_refresh() {
  Pair pair;
  arena::gateway::Metrics metrics;
  auto handler = std::make_shared<Handler>();
  auto session = std::make_shared<arena::gateway::Session>(pair.take_server(), handler, metrics, options());
  session->start();
  std::this_thread::sleep_for(100ms);
  send(pair.client, MessageType::PlayCardReq, "match_id=wire_match;turn_id=0;action_id=0;card=-1");
  require(handler->wait_entered(Clock::now() + 50ms), "syntactically valid action dispatched");
  std::this_thread::sleep_for(130ms);
  require(!session->check_heartbeat() && !session->closed(), "wire-valid action refreshes despite business-invalid values");
  send(pair.client, MessageType::AdminRoomsReq, "request_id=wire_admin");
  const auto dispatch_deadline = Clock::now() + 50ms;
  while (handler->requests.load() < 2 && Clock::now() < dispatch_deadline) std::this_thread::sleep_for(1ms);
  require(handler->requests.load() == 2, "optional Text Admin correlation dispatched");
  std::this_thread::sleep_for(130ms);
  require(!session->check_heartbeat() && !session->closed(), "valid correlated Admin request refreshes deadline");
  finish(session, metrics);
}

void malformed_text_does_not_refresh(MessageType type, const std::string& payload, bool dispatch_expected) {
  Pair pair;
  arena::gateway::Metrics metrics;
  auto handler = std::make_shared<Handler>();
  auto session = std::make_shared<arena::gateway::Session>(pair.take_server(), handler, metrics, options());
  session->start();
  const auto end = Clock::now() + 300ms;
  while (!session->closed() && Clock::now() < end) {
    const auto frame = arena::proto::encode(type, payload);
    if (!arena::gateway::send_all(pair.client, frame.data(), frame.size())) break;
    std::this_thread::sleep_for(30ms);
    session->check_heartbeat();
  }
  require(session->closed() && metrics.heartbeat_timeouts.load() == 1 &&
              (dispatch_expected ? handler->requests.load() > 0 : handler->requests.load() == 0),
          "invalid wire requests cannot keep a session live; existing dispatch behavior preserved");
  require(session->wait_idle(Clock::now() + 1s), "unknown request session drained");
  finish(session, metrics);
}

void set_environment(const char* name, const std::string& value) {
#ifdef _WIN32
  require(_putenv_s(name, value.c_str()) == 0, "set environment");
#else
  require(value.empty() ? ::unsetenv(name) == 0 : ::setenv(name, value.c_str(), 1) == 0, "set environment");
#endif
}

void strict_configuration() {
  for (const auto* name : {"ARENA_MAX_CONNECTIONS", "ARENA_REQUEST_RATE_PER_SECOND", "ARENA_REQUEST_BURST",
                            "ARENA_HEARTBEAT_TIMEOUT_MS"}) set_environment(name, "");
  require(arena::gateway::SessionOptions::from_environment().heartbeat_timeout_ms == 30000, "default heartbeat 30 s");
  set_environment("ARENA_HEARTBEAT_TIMEOUT_MS", "100");
  require(arena::gateway::SessionOptions::from_environment().heartbeat_timeout_ms == 100, "lower heartbeat bound");
  set_environment("ARENA_HEARTBEAT_TIMEOUT_MS", "300000");
  require(arena::gateway::SessionOptions::from_environment().heartbeat_timeout_ms == 300000, "upper heartbeat bound");
  for (const auto* invalid : {"0", "99", "300001", "-1", "bad", "100ms", "4294967296"}) {
    set_environment("ARENA_HEARTBEAT_TIMEOUT_MS", invalid);
    bool rejected = false;
    try { arena::gateway::SessionOptions::from_environment(); }
    catch (const std::invalid_argument&) { rejected = true; }
    require(rejected, "invalid heartbeat configuration rejected: " + std::string(invalid));
  }
}
}  // namespace

int main() {
  try {
    require(arena::gateway::initialize_sockets(), "initialize sockets");
    partial_frame_deadline(false);
    partial_frame_deadline(true);
    heartbeat_refresh_and_outbound_silence();
    blocked_callback_is_monitored_and_drained();
    malformed_text_does_not_refresh(static_cast<MessageType>(999), "bad", false);
    malformed_text_does_not_refresh(MessageType::PlayCardReq, "foo=bar", true);
    malformed_text_does_not_refresh(MessageType::EndTurnReq, "match_id=m;turn_id=1;action_id=1x", true);
    valid_action_and_admin_wire_refresh();
    strict_configuration();
    std::cout << "Heartbeat: 9 focused cases passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
