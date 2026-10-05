#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#ifdef _WIN32
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

int delayed_getaddrinfo(const char*, const char*, const addrinfo*, addrinfo**);
void observed_freeaddrinfo(addrinfo*);

// Only this test translation unit intercepts the blocking system call. The
// production class and its resolver worker are compiled unchanged otherwise.
#define getaddrinfo delayed_getaddrinfo
#define freeaddrinfo observed_freeaddrinfo
#include "server/ranking/redis_leaderboard.h"
#undef freeaddrinfo
#undef getaddrinfo

namespace {
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
using Socket = arena_redis_socket_t;
using Leaderboard = arena::ranking::RedisLeaderboard;

void require(bool value, const std::string& message) {
  if (!value) throw std::runtime_error(message);
}

struct Delay {
  std::mutex mutex;
  std::condition_variable changed;
  bool released = false;
  std::chrono::milliseconds timed_delay{0};
  int calls = 0;
  int active = 0;
  int peak = 0;
  int finished = 0;
};
std::mutex injection_mutex;
std::shared_ptr<Delay> injection;
std::map<addrinfo*, std::shared_ptr<Delay>> pending_frees;

class Injection {
 public:
  explicit Injection(std::chrono::milliseconds timed_delay = 0ms)
      : state(std::make_shared<Delay>()) {
    state->timed_delay = timed_delay;
    std::lock_guard<std::mutex> lock(injection_mutex);
    require(!injection, "only one DNS injection at a time");
    injection = state;
  }
  ~Injection() {
    release();
    wait_finished();
    std::lock_guard<std::mutex> lock(injection_mutex);
    injection.reset();
  }
  void release() {
    std::lock_guard<std::mutex> lock(state->mutex);
    state->released = true;
    state->changed.notify_all();
  }
  bool wait_entered() {
    std::unique_lock<std::mutex> lock(state->mutex);
    return state->changed.wait_for(lock, 1s, [&] { return state->calls > 0; });
  }
  bool wait_finished() {
    std::unique_lock<std::mutex> lock(state->mutex);
    return state->changed.wait_for(lock, 2s, [&] {
      return state->active == 0 && state->finished == state->calls;
    });
  }
  void check(int calls, int active) {
    std::lock_guard<std::mutex> lock(state->mutex);
    require(state->calls == calls && state->active == active && state->peak == 1,
            "DNS injection must observe exactly one resolver, without retry accumulation");
  }
  std::shared_ptr<Delay> state;
};

void close_socket(Socket socket) {
  if (socket == kArenaRedisInvalidSocket) return;
#ifdef _WIN32
  ::closesocket(socket);
#else
  ::close(socket);
#endif
}

bool readable(Socket socket, int milliseconds) {
  fd_set ready;
  FD_ZERO(&ready);
  FD_SET(socket, &ready);
  timeval timeout{};
  timeout.tv_usec = milliseconds * 1000;
  return ::select(static_cast<int>(socket + 1), &ready, nullptr, nullptr, &timeout) > 0;
}

class Listener {
 public:
  Listener() {
    socket = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    require(socket != kArenaRedisInvalidSocket, "create private listener");
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(::bind(socket, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0,
            "bind private listener");
    require(::listen(socket, 1) == 0, "listen on private port");
#ifdef _WIN32
    int size = sizeof(address);
#else
    socklen_t size = sizeof(address);
#endif
    require(::getsockname(socket, reinterpret_cast<sockaddr*>(&address), &size) == 0, "query private port");
  }
  ~Listener() { close_socket(socket); }
  unsigned short port() const { return ntohs(address.sin_port); }
  Socket socket = kArenaRedisInvalidSocket;
  sockaddr_in address{};
};

class Peer {
 public:
  Peer() : worker([this] {
    while (!stop && !readable(listener.socket, 20)) {}
    if (stop) return;
    const auto client = ::accept(listener.socket, nullptr, nullptr);
    if (client == kArenaRedisInvalidSocket) { failed = true; return; }
    try {
      std::string line;
      require(read_line(client, line) && line[0] == '*', "read Redis array");
      const int parts = std::stoi(line.substr(1));
      for (int i = 0; i < parts; ++i) {
        require(read_line(client, line) && line[0] == '$', "read Redis bulk length");
        const int length = std::stoi(line.substr(1));
        require(length >= 0 && length < 65536 && read_bytes(client, static_cast<size_t>(length + 2), line),
                "read complete Redis command");
      }
      command_seen = true;
#ifdef _WIN32
      require(::send(client, ":1\r\n", 4, 0) == 4, "send Redis success");
#else
      require(::send(client, ":1\r\n", 4, MSG_NOSIGNAL) == 4, "send Redis success");
#endif
    } catch (...) { failed = true; }
    close_socket(client);
  }) {}
  ~Peer() { stop = true; worker.join(); }
  bool read_bytes(Socket socket, size_t count, std::string& out) {
    out.clear();
    const auto deadline = Clock::now() + 1s;
    while (!stop && out.size() < count && Clock::now() < deadline) {
      if (!readable(socket, 20)) continue;
      char buffer[1024];
      const int read = static_cast<int>(::recv(socket, buffer,
          static_cast<int>(std::min(sizeof(buffer), count - out.size())), 0));
      if (read <= 0) return false;
      out.append(buffer, static_cast<size_t>(read));
    }
    return out.size() == count;
  }
  bool read_line(Socket socket, std::string& out) {
    out.clear();
    std::string byte;
    while (out.size() < 1024 && read_bytes(socket, 1, byte)) {
      out += byte;
      if (out.size() >= 2 && out.substr(out.size() - 2) == "\r\n") return true;
    }
    return false;
  }
  Listener listener;
  std::atomic<bool> stop{false}, command_seen{false}, failed{false};
  std::thread worker;
};

void delayed_retries_and_recovery() {
  Peer peer;
  Injection delayed;
  Leaderboard redis({120, 500});
  std::string error;
  for (int retry = 0; retry < 4; ++retry) {
    const auto start = Clock::now();
    // A changed target must also wait for the existing resolver, not spawn one.
    const std::string host = retry == 3 ? "localhost" : "127.0.0.1";
    require(!redis.connect(host, peer.listener.port(), error), "delayed DNS must fail within budget");
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start);
    require(elapsed >= 80ms && elapsed < 400ms, "delayed DNS caller obeys 120ms budget");
    require(error == "Redis DNS lookup timed out" && !redis.connected(), "DNS timeout leaves no valid socket");
    require(delayed.wait_entered(), "system getaddrinfo injection was entered");
    delayed.check(1, 1);
    std::cout << "DNS blocked retry " << retry + 1 << ": " << elapsed.count() << "ms, resolver calls=1 active=1\n";
  }
  delayed.release();
  require(delayed.wait_finished(), "released resolver must finish and free addresses");
  const bool connected = redis.connect("127.0.0.1", peer.listener.port(), error);
  require(connected, "reuse released DNS result: " + error);
  delayed.check(1, 0);
  const bool applied = redis.apply("dns-test", "winner", "loser", 10, -10, error);
  require(applied, "Redis apply after DNS recovery: " + error);
  require(peer.command_seen && !peer.failed, "real TCP command and response after DNS recovery");
  std::cout << "DNS release: reused result, private TCP Redis apply succeeded\n";
}

void destroy_while_resolution_is_blocked() {
  Injection delayed;
  auto redis = std::make_unique<Leaderboard>(Leaderboard::Timeouts{100, 500});
  std::string error;
  require(!redis->connect("localhost", 9, error) && error == "Redis DNS lookup timed out",
          "destruction case must really have blocked in DNS");
  require(delayed.wait_entered(), "destruction resolver entered injection");
  delayed.check(1, 1);
  const auto start = Clock::now();
  redis.reset();
  require(Clock::now() - start < 100ms, "object destruction must not join a stalled system resolver");
  delayed.check(1, 1);
  delayed.release();
  require(delayed.wait_finished(), "late resolver completes and frees real system addresses after object destruction");
  // Address-owner destruction is the final resolver-worker operation. Give
  // detached-thread teardown time to finish before normal sanitizer exit.
  std::this_thread::sleep_for(30ms);
  std::cout << "DNS late completion: object destroyed before release, addresses freed safely\n";
}

#ifndef _WIN32
void dns_and_tcp_share_deadline() {
  Listener listener;
  struct SocketGuard { Socket value; ~SocketGuard() { close_socket(value); } };
  SocketGuard first{::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)};
  SocketGuard second{::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)};
  for (Socket socket : {first.value, second.value}) {
    require(socket != kArenaRedisInvalidSocket, "create backlog filler");
    const int flags = ::fcntl(socket, F_GETFL, 0);
    require(flags >= 0 && ::fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0, "nonblocking backlog filler");
    const int result = ::connect(socket, reinterpret_cast<const sockaddr*>(&listener.address), sizeof(listener.address));
    if (result != 0) {
      require(errno == EINPROGRESS, "backlog filler pending");
      pollfd ready{};
      ready.fd = socket;
      ready.events = POLLOUT;
      require(::poll(&ready, 1, 500) > 0, "backlog filler connected");
      int error = 0;
      socklen_t length = sizeof(error);
      require(::getsockopt(socket, SOL_SOCKET, SO_ERROR, &error, &length) == 0 && error == 0,
              "backlog filler established");
    }
  }
  std::this_thread::sleep_for(20ms);
  Injection delayed(200ms);
  Leaderboard redis({300, 500});
  std::string error;
  const auto start = Clock::now();
  require(!redis.connect("127.0.0.1", listener.port(), error), "DNS plus blackholed TCP must time out");
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start);
  // Two separate 300ms budgets would take at least 500ms after the 200ms DNS
  // delay. This upper bound includes scheduler slack but rejects that renewal.
  require(elapsed >= 270ms && elapsed < 430ms, "DNS and TCP must share one 300ms deadline");
  require(error == "Redis connection timed out" && !redis.connected(), "shared connect timeout invalidates socket");
  delayed.check(1, 0);
  require(delayed.wait_finished(), "timed DNS was actually completed before TCP wait");
  std::cout << "DNS 200ms plus full TCP backlog: " << elapsed.count() << "ms total for 300ms budget\n";
}
#endif
}  // namespace

int delayed_getaddrinfo(const char* host, const char* port, const addrinfo* hints, addrinfo** result) {
  std::shared_ptr<Delay> state;
  {
    std::lock_guard<std::mutex> lock(injection_mutex);
    state = injection;
  }
  if (state) {
    std::unique_lock<std::mutex> lock(state->mutex);
    ++state->calls;
    ++state->active;
    state->peak = std::max(state->peak, state->active);
    state->changed.notify_all();
    if (state->timed_delay.count()) state->changed.wait_for(lock, state->timed_delay, [&] { return state->released; });
    else state->changed.wait(lock, [&] { return state->released; });
  }
  const int outcome = ::getaddrinfo(host, port, hints, result);
  if (state) {
    if (*result) {
      std::lock_guard<std::mutex> lock(injection_mutex);
      pending_frees.emplace(*result, state);
    }
    std::lock_guard<std::mutex> lock(state->mutex);
    --state->active;
    if (!*result) ++state->finished;
    state->changed.notify_all();
  }
  return outcome;
}

void observed_freeaddrinfo(addrinfo* addresses) {
  std::shared_ptr<Delay> state;
  {
    std::lock_guard<std::mutex> lock(injection_mutex);
    const auto found = pending_frees.find(addresses);
    if (found != pending_frees.end()) { state = found->second; pending_frees.erase(found); }
  }
  ::freeaddrinfo(addresses);
  if (state) {
    std::lock_guard<std::mutex> lock(state->mutex);
    ++state->finished;
    state->changed.notify_all();
  }
}

int main() {
#ifdef _WIN32
  WSADATA data{};
  if (::WSAStartup(MAKEWORD(2, 2), &data) != 0) return 1;
#endif
  try {
    delayed_retries_and_recovery();
    destroy_while_resolution_is_blocked();
#ifndef _WIN32
    dns_and_tcp_share_deadline();
    std::cout << "Redis DNS: 3 focused cases passed\n";
#else
    std::cout << "Redis DNS: 2 focused cases passed (Linux full-backlog case not applicable)\n";
#endif
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
#ifdef _WIN32
  ::WSACleanup();
#endif
  return 0;
}
