#include <atomic>
#include <chrono>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

#include "server/ranking/redis_leaderboard.h"

namespace {
using Socket = arena_redis_socket_t;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
using Leaderboard = arena::ranking::RedisLeaderboard;

void require(bool condition, const std::string& message) {
  if (!condition) throw std::runtime_error(message);
}

void close_socket(Socket socket) {
  if (socket == kArenaRedisInvalidSocket) return;
#ifdef _WIN32
  ::closesocket(socket);
#else
  ::close(socket);
#endif
}

bool readable(Socket socket, int timeout_ms) {
  fd_set set;
  FD_ZERO(&set);
  FD_SET(socket, &set);
  timeval timeout{};
  timeout.tv_sec = timeout_ms / 1000;
  timeout.tv_usec = (timeout_ms % 1000) * 1000;
  return ::select(static_cast<int>(socket + 1), &set, nullptr, nullptr, &timeout) > 0;
}

bool read_exact(Socket socket, size_t size, std::string& data) {
  data.clear();
  const auto deadline = Clock::now() + 1s;
  while (data.size() < size && Clock::now() < deadline) {
    if (!readable(socket, 20)) continue;
    char chunk[1024];
    const auto received = ::recv(socket, chunk, static_cast<int>(std::min(size - data.size(), sizeof(chunk))), 0);
    if (received <= 0) return false;
    data.append(chunk, static_cast<size_t>(received));
  }
  return data.size() == size;
}

bool read_line(Socket socket, std::string& line) {
  line.clear();
  std::string byte;
  while (line.size() < 1024) {
    if (!read_exact(socket, 1, byte)) return false;
    line += byte;
    if (line.size() >= 2 && line.substr(line.size() - 2) == "\r\n") return true;
  }
  return false;
}

bool read_command(Socket socket) {
  std::string line;
  if (!read_line(socket, line) || line.empty() || line[0] != '*') return false;
  const int count = std::stoi(line.substr(1));
  for (int i = 0; i < count; ++i) {
    if (!read_line(socket, line) || line.empty() || line[0] != '$') return false;
    const auto length = static_cast<size_t>(std::stoul(line.substr(1)));
    if (!read_exact(socket, length + 2, line) || line.substr(length) != "\r\n") return false;
  }
  return true;
}

bool send_reply(Socket socket, const std::string& reply) {
#ifdef _WIN32
  return ::send(socket, reply.data(), static_cast<int>(reply.size()), 0) == static_cast<int>(reply.size());
#else
  return ::send(socket, reply.data(), reply.size(), MSG_NOSIGNAL) == static_cast<ssize_t>(reply.size());
#endif
}

void wait_for_stop(const std::atomic<bool>& stop) {
  while (!stop.load()) std::this_thread::sleep_for(5ms);
}

// A private loopback peer makes partial progress, reply loss, and blocked writes
// deterministic without touching the shared Redis service or any database.
class Peer {
 public:
  using Handler = std::function<void(Socket, const std::atomic<bool>&)>;

  explicit Peer(std::vector<Handler> handlers, bool small_receive_buffer = false)
      : handlers_(std::move(handlers)) {
    listener_ = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    require(listener_ != kArenaRedisInvalidSocket, "create loopback listener");
    if (small_receive_buffer) {
      const int size = 1024;
#ifdef _WIN32
      require(::setsockopt(listener_, SOL_SOCKET, SO_RCVBUF, reinterpret_cast<const char*>(&size), sizeof(size)) == 0,
              "configure small receive buffer");
#else
      require(::setsockopt(listener_, SOL_SOCKET, SO_RCVBUF, &size, sizeof(size)) == 0,
              "configure small receive buffer");
#endif
    }
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(::bind(listener_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0, "bind listener");
    require(::listen(listener_, 4) == 0, "listen");
#ifdef _WIN32
    int length = sizeof(address);
#else
    socklen_t length = sizeof(address);
#endif
    require(::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length) == 0, "query listener port");
    port_ = ntohs(address.sin_port);
    worker_ = std::thread([this] {
      for (const auto& handler : handlers_) {
        while (!stop_.load() && !readable(listener_, 20)) {}
        if (stop_.load()) return;
        const Socket client = ::accept(listener_, nullptr, nullptr);
        if (client == kArenaRedisInvalidSocket) return;
        try {
          handler(client, stop_);
        } catch (...) {
          peer_failed_.store(true);
        }
        close_socket(client);
      }
    });
  }

  Peer(const Peer&) = delete;
  Peer& operator=(const Peer&) = delete;
  ~Peer() {
    stop_.store(true);
    worker_.join();
    close_socket(listener_);
  }
  unsigned short port() const { return port_; }
  bool failed() const { return peer_failed_.load(); }

 private:
  Socket listener_ = kArenaRedisInvalidSocket;
  unsigned short port_ = 0;
  std::vector<Handler> handlers_;
  std::atomic<bool> stop_{false};
  std::atomic<bool> peer_failed_{false};
  std::thread worker_;
};

void connect(Leaderboard& redis, const Peer& peer) {
  std::string error;
  require(redis.connect("127.0.0.1", peer.port(), error), "connect private peer: " + error);
}

bool apply(Leaderboard& redis, std::string& error, const std::string& winner = "winner") {
  return redis.apply("transport-test", winner, "loser", 10, -10, error);
}

void success_and_idempotent_reply() {
  Peer peer({[](Socket socket, const std::atomic<bool>&) {
    require(read_command(socket), "read first command");
    require(send_reply(socket, ":1\r\n"), "first successful reply");
    require(read_command(socket), "read retry command");
    require(send_reply(socket, ":0\r\n"), "duplicate successful reply");
  }});
  Leaderboard redis({1000, 120});
  connect(redis, peer);
  std::string error = "stale error";
  require(apply(redis, error) && error.empty(), "successful apply clears error");
  require(apply(redis, error) && redis.connected(), "idempotent :0 reply preserves socket");
  require(!peer.failed(), "private success peer failed");
}

void read_timeout_and_reconnect() {
  std::atomic<bool> request_seen{false};
  Peer peer({[&](Socket socket, const std::atomic<bool>& stop) {
    require(read_command(socket), "read blackholed command");
    request_seen.store(true);
    require(send_reply(socket, ":"), "partial reply");
    // Observe EOF so the same listener can accept the reconnect.
    while (!stop.load()) {
      if (!readable(socket, 20)) continue;
      char byte;
      if (::recv(socket, &byte, 1, 0) <= 0) return;
    }
  }, [](Socket socket, const std::atomic<bool>&) {
    require(read_command(socket), "read reconnected command");
    require(send_reply(socket, ":1\r\n"), "reconnected reply");
  }});
  Leaderboard redis({1000, 120});
  connect(redis, peer);
  std::string error;
  const auto start = Clock::now();
  require(!apply(redis, error), "partial reply must time out");
  const auto elapsed = Clock::now() - start;
  require(request_seen.load(), "blackhole actually received command");
  require(elapsed >= 80ms && elapsed < 1s, "read timeout bounded near configured budget");
  require(error == "Redis response read timed out" && !redis.connected(), "read timeout invalidates socket");
  connect(redis, peer);
  require(apply(redis, error), "reconnect clears old partial response: " + error);
  require(!peer.failed(), "private reconnect peer failed");
}

void trickle_does_not_extend_deadline() {
  std::atomic<int> sent{0};
  Peer peer({[&](Socket socket, const std::atomic<bool>& stop) {
    require(read_command(socket), "read trickle command");
    const auto end = Clock::now() + 600ms;
    while (!stop.load() && Clock::now() < end) {
      if (!send_reply(socket, ":")) return;
      ++sent;
      std::this_thread::sleep_for(30ms);
    }
  }});
  Leaderboard redis({1000, 120});
  connect(redis, peer);
  std::string error;
  const auto start = Clock::now();
  require(!apply(redis, error), "trickle reply must time out");
  require(Clock::now() - start < 350ms && sent.load() >= 2, "partial reads never renew absolute deadline");
  require(error == "Redis response read timed out" && !redis.connected(), "trickle timeout invalidates socket");
}

void blocked_write_times_out() {
  std::atomic<bool> accepted{false};
  Peer peer({[&](Socket, const std::atomic<bool>& stop) {
    accepted.store(true);
    wait_for_stop(stop);  // Never drain the receive window.
  }}, true);
  Leaderboard redis({1000, 120});
  connect(redis, peer);
  std::string error;
  const std::string winner(8 * 1024 * 1024, 'w');
  const auto start = Clock::now();
  require(!apply(redis, error, winner), "blocked large request must time out");
  require(accepted.load() && Clock::now() - start < 1500ms, "write wait is bounded despite partial sends");
  require(error == "Redis request write timed out" && !redis.connected(),
          "write timeout invalidates socket; actual error: " + error);
}

void error_reply_invalidates_socket(const std::string& reply) {
  Peer peer({[&](Socket socket, const std::atomic<bool>&) {
    require(read_command(socket), "read error command");
    require(send_reply(socket, reply), "send error reply");
  }});
  Leaderboard redis({1000, 120});
  connect(redis, peer);
  std::string error;
  require(!apply(redis, error) && !error.empty() && !redis.connected(), "error/malformed response invalidates socket");
}

void refused_connection_is_bounded() {
  unsigned short unused_port;
  {
    Peer peer({});
    unused_port = peer.port();
  }
  Leaderboard redis({120, 120});
  std::string error;
  const auto start = Clock::now();
  require(!redis.connect("127.0.0.1", unused_port, error), "refused port must fail");
  require(Clock::now() - start < 1s && !redis.connected() && !error.empty(), "failed connect bounded and invalidated");
}

#ifndef _WIN32
void pending_connection_times_out() {
  struct Guard {
    explicit Guard(Socket value) : socket(value) {}
    ~Guard() { close_socket(socket); }
    Socket socket;
  };
  Guard listener(::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP));
  require(listener.socket != kArenaRedisInvalidSocket, "create full-backlog listener");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  require(::bind(listener.socket, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == 0,
          "bind full-backlog listener");
  require(::listen(listener.socket, 1) == 0, "listen with one-slot backlog");
  socklen_t length = sizeof(address);
  require(::getsockname(listener.socket, reinterpret_cast<sockaddr*>(&address), &length) == 0,
          "query full-backlog port");
  // Linux admits backlog + 1 established, unaccepted sockets. Leave both in
  // the queue so the next SYN receives no reply and exercises EINPROGRESS.
  Guard first(::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP));
  Guard second(::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP));
  for (Socket socket : {first.socket, second.socket}) {
    require(socket != kArenaRedisInvalidSocket, "create backlog filler");
    const int flags = ::fcntl(socket, F_GETFL, 0);
    require(flags >= 0 && ::fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0, "nonblocking backlog filler");
    const int result = ::connect(socket, reinterpret_cast<const sockaddr*>(&address), sizeof(address));
    if (result != 0) {
      require(errno == EINPROGRESS, "backlog filler begins pending connect");
      pollfd writable{};
      writable.fd = socket;
      writable.events = POLLOUT;
      require(::poll(&writable, 1, 500) > 0, "initial backlog filler connects within 500 ms");
      int error = 0;
      socklen_t size = sizeof(error);
      require(::getsockopt(socket, SOL_SOCKET, SO_ERROR, &error, &size) == 0 && error == 0,
              "initial backlog filler established");
    }
  }
  std::this_thread::sleep_for(20ms);  // Let both final handshake ACKs reach the queue.
  Leaderboard redis({120, 120});
  std::string error;
  const auto start = Clock::now();
  require(!redis.connect("127.0.0.1", ntohs(address.sin_port), error), "full backlog connect must time out");
  const auto elapsed = Clock::now() - start;
  require(elapsed >= 80ms && elapsed < 1s, "pending connect obeys absolute deadline");
  require(error == "Redis connection timed out" && !redis.connected(), "connect timeout invalidates candidate socket");
}
#endif

void set_environment(const char* name, const std::string& value) {
#ifdef _WIN32
  require(_putenv_s(name, value.c_str()) == 0, "set test environment");
#else
  require(value.empty() ? ::unsetenv(name) == 0 : ::setenv(name, value.c_str(), 1) == 0, "set test environment");
#endif
}

void environment_timeout(const std::string& value, std::chrono::milliseconds minimum,
                         std::chrono::milliseconds maximum) {
  set_environment("ARENA_REDIS_IO_TIMEOUT_MS", value);
  Peer peer({[](Socket socket, const std::atomic<bool>& stop) {
    require(read_command(socket), "read environment timeout command");
    wait_for_stop(stop);
  }});
  Leaderboard redis;
  connect(redis, peer);
  std::string error;
  const auto start = Clock::now();
  require(!apply(redis, error), "environment configured blackhole must time out");
  const auto elapsed = Clock::now() - start;
  require(elapsed >= minimum && elapsed < maximum, "environment/default timeout budget");
  require(error == "Redis response read timed out" && !redis.connected(), "environment timeout invalidates socket");
}
}  // namespace

int main() {
#ifdef _WIN32
  WSADATA wsa{};
  if (::WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return 1;
#endif
  try {
    set_environment("ARENA_REDIS_FAIL_APPLY_COUNT", "0");
    set_environment("ARENA_REDIS_CONNECT_TIMEOUT_MS", "1000");
    success_and_idempotent_reply();
    read_timeout_and_reconnect();
    trickle_does_not_extend_deadline();
    blocked_write_times_out();
    error_reply_invalidates_socket("-ERR deliberate peer failure\r\n");
    error_reply_invalidates_socket("+OK\r\n");
    refused_connection_is_bounded();
#ifndef _WIN32
    pending_connection_times_out();
#endif
    environment_timeout("40", 20ms, 500ms);
    environment_timeout("0", 0ms, 250ms);  // Lower clamp is 1 ms.
    environment_timeout("invalid", 850ms, 2500ms);
    environment_timeout("", 850ms, 2500ms);  // Unset uses 1000 ms.
#ifdef _WIN32
    std::cout << "Redis transport: 11 focused cases passed (Linux backlog case not applicable)\n";
#else
    std::cout << "Redis transport: 12 focused cases passed\n";
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
