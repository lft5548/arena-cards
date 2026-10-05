#pragma once

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#include "server/room/test_fault_barrier.h"

#ifdef _WIN32
#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
using arena_redis_socket_t = SOCKET;
constexpr arena_redis_socket_t kArenaRedisInvalidSocket = INVALID_SOCKET;
#else
#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
using arena_redis_socket_t = int;
constexpr arena_redis_socket_t kArenaRedisInvalidSocket = -1;
#endif

namespace arena::ranking {

class RedisLeaderboard {
 public:
  struct Timeouts {
    unsigned int connect_ms = 1000;
    unsigned int io_ms = 1000;
  };

  RedisLeaderboard() : RedisLeaderboard(read_timeouts()) {}
  explicit RedisLeaderboard(Timeouts timeouts)
      : timeouts_{bounded_timeout(timeouts.connect_ms), bounded_timeout(timeouts.io_ms)},
        injected_failures_remaining_(read_injected_failures()) {}
  RedisLeaderboard(const RedisLeaderboard&) = delete;
  RedisLeaderboard& operator=(const RedisLeaderboard&) = delete;
  ~RedisLeaderboard() { close(); }

  bool connect(const std::string& host, unsigned short port, std::string& error) {
    std::lock_guard<std::mutex> lock(mutex_);
    close_unlocked();
    const auto deadline = Clock::now() + std::chrono::milliseconds(timeouts_.connect_ms);
#ifdef _WIN32
    if (!winsock_) winsock_ = std::make_shared<WinsockLease>();
    if (!winsock_->initialized) {
      error = "Redis Winsock initialization failed";
      return false;
    }
#endif
    const auto port_text = std::to_string(port);
    std::vector<Address> addresses;
    if (!resolve(host, port_text, deadline, addresses, error)) return false;
    for (const auto& address : addresses) {
      if (Clock::now() >= deadline) {
        error = "Redis connection timed out";
        return false;
      }
      auto candidate = ::socket(address.family, address.type, address.protocol);
      if (candidate == kArenaRedisInvalidSocket) continue;
      if (!set_nonblocking(candidate)) {
        close_socket(candidate);
        continue;
      }
      const auto result = ::connect(candidate, reinterpret_cast<const sockaddr*>(&address.storage),
                                    static_cast<int>(address.length));
      bool established = result == 0;
      if (!established && connect_pending(last_socket_error()) &&
          wait_ready(candidate, true, deadline, "connection", error)) {
        int socket_error = 0;
#ifdef _WIN32
        int length = sizeof(socket_error);
        established = ::getsockopt(candidate, SOL_SOCKET, SO_ERROR,
                                    reinterpret_cast<char*>(&socket_error), &length) == 0 && socket_error == 0;
#else
        socklen_t length = sizeof(socket_error);
        established = ::getsockopt(candidate, SOL_SOCKET, SO_ERROR, &socket_error, &length) == 0 && socket_error == 0;
#endif
      }
      if (established && Clock::now() < deadline) {
        socket_ = candidate;
        error.clear();
        return true;
      }
      close_socket(candidate);
    }
    error = Clock::now() >= deadline ? "Redis connection timed out" :
        "Redis connection failed to " + host + ":" + port_text;
    return false;
  }

  bool connected() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return socket_ != kArenaRedisInvalidSocket;
  }

  // Apply a committed match result exactly once. Redis is a cache, so a
  // failed update is returned to the caller but never treated as settlement
  // failure by the game server.
  bool apply(const std::string& match_id, const std::string& winner,
             const std::string& loser, int winner_delta, int loser_delta,
             std::string& error) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (socket_ == kArenaRedisInvalidSocket) {
      error = "Redis connection is not open";
      return false;
    }
    if (match_id.empty() || winner.empty() || loser.empty()) {
      error = "Redis leaderboard update requires match and player ids";
      return false;
    }
    // The mutex serializes cache writes and the opt-in pre-apply fault barrier.
    room::test_fault_barrier("redis_before_apply", match_id);
    if (injected_failures_remaining_ > 0) {
      --injected_failures_remaining_;
      error = "injected Redis leaderboard update failure";
      return false;
    }
    static const std::string script =
        "local applied=redis.call('SETNX',KEYS[1],'1');"
        "if applied==0 then return 0 end;"
        "redis.call('EXPIRE',KEYS[1],31536000);"
        "if not redis.call('ZSCORE',KEYS[2],ARGV[2]) then redis.call('ZADD',KEYS[2],1000,ARGV[2]) end;"
        "if not redis.call('ZSCORE',KEYS[2],ARGV[4]) then redis.call('ZADD',KEYS[2],1000,ARGV[4]) end;"
        "redis.call('ZINCRBY',KEYS[2],ARGV[1],ARGV[2]);"
        "redis.call('ZINCRBY',KEYS[2],ARGV[3],ARGV[4]);"
        "return 1";
    const std::vector<std::string> command = {
        "EVAL", script, "2", "arena:match:rank:" + match_id,
        "arena:leaderboard:rating", std::to_string(winner_delta), winner,
        std::to_string(loser_delta), loser};
    std::string reply;
    if (!command_reply(command, reply, error)) return false;
    if (reply != ":1" && reply != ":0") {
      error = "unexpected Redis leaderboard response: " + reply;
      close_unlocked();
      return false;
    }
    return true;
  }

 private:
  using Clock = std::chrono::steady_clock;
  using Deadline = Clock::time_point;

#ifdef _WIN32
  struct WinsockLease {
    WinsockLease() {
      WSADATA data{};
      initialized = ::WSAStartup(MAKEWORD(2, 2), &data) == 0;
    }
    ~WinsockLease() { if (initialized) ::WSACleanup(); }
    bool initialized = false;
  };
#endif

  struct Address {
    sockaddr_storage storage{};
    size_t length = 0;
    int family = 0;
    int type = 0;
    int protocol = 0;
  };

  struct Resolution {
    std::string host;
    std::string port;
    std::mutex mutex;
    std::condition_variable ready;
    bool complete = false;
    bool success = false;
    std::vector<Address> addresses;
#ifdef _WIN32
    std::shared_ptr<WinsockLease> winsock;
#endif
  };

  // getaddrinfo itself is blocking. A single shared task bounds the caller's
  // wait without spawning another resolver on every timeout/retry. It owns
  // all data (including Winsock lifetime) and never accesses this object.
  bool resolve(const std::string& host, const std::string& port, Deadline deadline,
               std::vector<Address>& addresses, std::string& error) {
    if (resolution_ && (resolution_->host != host || resolution_->port != port)) {
      std::unique_lock<std::mutex> lock(resolution_->mutex);
      if (!resolution_->ready.wait_until(lock, deadline, [&] { return resolution_->complete; })) {
        error = "Redis DNS lookup timed out";
        return false;
      }
      lock.unlock();
      resolution_.reset();
    }
    if (!resolution_) {
      auto state = std::make_shared<Resolution>();
      state->host = host;
      state->port = port;
#ifdef _WIN32
      state->winsock = winsock_;
#endif
      try {
        std::thread([state] {
          addrinfo hints{};
          hints.ai_socktype = SOCK_STREAM;
          hints.ai_family = AF_UNSPEC;
          addrinfo* raw = nullptr;
          const int result = ::getaddrinfo(state->host.c_str(), state->port.c_str(), &hints, &raw);
          const std::unique_ptr<addrinfo, decltype(&::freeaddrinfo)> owner(raw, ::freeaddrinfo);
          std::vector<Address> resolved;
          if (result == 0) {
            for (auto* current = raw; current != nullptr; current = current->ai_next) {
              if (current->ai_addrlen > sizeof(sockaddr_storage)) continue;
              Address address;
              std::memcpy(&address.storage, current->ai_addr, current->ai_addrlen);
              address.length = current->ai_addrlen;
              address.family = current->ai_family;
              address.type = current->ai_socktype;
              address.protocol = current->ai_protocol;
              resolved.push_back(address);
            }
          }
          {
            std::lock_guard<std::mutex> lock(state->mutex);
            state->success = result == 0 && !resolved.empty();
            state->addresses = std::move(resolved);
            state->complete = true;
          }
          state->ready.notify_all();
        }).detach();
      } catch (const std::system_error&) {
        error = "Redis DNS resolver could not start";
        return false;
      }
      resolution_ = state;
    }
    const auto state = resolution_;
    std::unique_lock<std::mutex> lock(state->mutex);
    if (!state->ready.wait_until(lock, deadline, [&] { return state->complete; })) {
      error = "Redis DNS lookup timed out";
      return false;
    }
    const bool success = state->success;
    addresses = state->addresses;
    lock.unlock();
    resolution_.reset();  // Next connection resolves again, honoring DNS changes.
    if (!success) error = "Redis DNS lookup failed for " + host;
    return success;
  }

  static int last_socket_error() {
#ifdef _WIN32
    return ::WSAGetLastError();
#else
    return errno;
#endif
  }

  static bool interrupted(int error) {
#ifdef _WIN32
    return error == WSAEINTR;
#else
    return error == EINTR;
#endif
  }

  static bool would_block(int error) {
#ifdef _WIN32
    return error == WSAEWOULDBLOCK;
#else
    return error == EAGAIN || error == EWOULDBLOCK;
#endif
  }

  static bool connect_pending(int error) {
#ifdef _WIN32
    return would_block(error) || error == WSAEINPROGRESS || interrupted(error);
#else
    return error == EINPROGRESS || would_block(error) || interrupted(error);
#endif
  }

  static bool set_nonblocking(arena_redis_socket_t socket) {
#ifdef _WIN32
    u_long enabled = 1;
    return ::ioctlsocket(socket, FIONBIO, &enabled) == 0;
#else
    const int flags = ::fcntl(socket, F_GETFL, 0);
    return flags >= 0 && ::fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0;
#endif
  }

  static bool wait_ready(arena_redis_socket_t socket, bool writing, Deadline deadline,
                         const char* operation, std::string& error) {
    while (Clock::now() < deadline) {
      const auto remaining = deadline - Clock::now();
#ifdef _WIN32
      auto micros = std::chrono::duration_cast<std::chrono::microseconds>(remaining).count();
      if (micros <= 0) break;
      timeval timeout{};
      timeout.tv_sec = static_cast<long>(micros / 1000000);
      timeout.tv_usec = static_cast<long>(micros % 1000000);
      fd_set ready;
      fd_set exceptional;
      FD_ZERO(&ready);
      FD_ZERO(&exceptional);
      FD_SET(socket, &ready);
      FD_SET(socket, &exceptional);
      const int result = ::select(0, writing ? nullptr : &ready, writing ? &ready : nullptr,
                                   &exceptional, &timeout);
#else
      const auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(remaining).count();
      if (remaining <= Clock::duration::zero()) break;
      pollfd ready{};
      ready.fd = socket;
      ready.events = writing ? POLLOUT : POLLIN;
      const int result = ::poll(&ready, 1, static_cast<int>(millis + 1));
#endif
      if (result > 0) {
        // A ready fd observed after the deadline must not renew the budget.
        if (Clock::now() < deadline) return true;
        break;
      }
      if (result < 0 && !interrupted(last_socket_error())) {
        error = std::string("Redis ") + operation + " wait failed";
        return false;
      }
    }
    error = std::string("Redis ") + operation + " timed out";
    return false;
  }

  static void close_socket(arena_redis_socket_t socket) {
    if (socket == kArenaRedisInvalidSocket) return;
#ifdef _WIN32
    ::closesocket(socket);
#else
    ::close(socket);
#endif
  }

  static std::string environment(const char* name) {
#ifdef _WIN32
    char* raw = nullptr;
    size_t size = 0;
    if (_dupenv_s(&raw, &size, name) != 0 || raw == nullptr) return {};
    const std::string value(raw);
    std::free(raw);
#else
    const char* raw = std::getenv(name);
    if (!raw || !*raw) return {};
    const std::string value(raw);
#endif
    return value;
  }

  static unsigned int bounded_timeout(unsigned int value) {
    return std::max(1u, std::min(value, 30000u));
  }

  static unsigned int read_timeout(const char* name) {
    const auto value = environment(name);
    if (value.empty() || value.find_first_not_of("0123456789") != std::string::npos) return 1000;
    char* end = nullptr;
    const auto parsed = std::strtoul(value.c_str(), &end, 10);
    return bounded_timeout(static_cast<unsigned int>(std::min<unsigned long>(parsed, 30000)));
  }

  static Timeouts read_timeouts() {
    return {read_timeout("ARENA_REDIS_CONNECT_TIMEOUT_MS"), read_timeout("ARENA_REDIS_IO_TIMEOUT_MS")};
  }

  static unsigned int read_injected_failures() {
    const auto value = environment("ARENA_REDIS_FAIL_APPLY_COUNT");
    if (value.empty()) return 0;
    char* end = nullptr;
    const auto parsed = std::strtoul(value.c_str(), &end, 10);
    return end != value.c_str() && *end == '\0' ? static_cast<unsigned int>(std::min<unsigned long>(parsed, 1000)) : 0;
  }

  void close_unlocked() {
    close_socket(socket_);
    socket_ = kArenaRedisInvalidSocket;
    receive_buffer_.clear();
  }

  void close() {
    std::lock_guard<std::mutex> lock(mutex_);
    close_unlocked();
  }

  static bool send_all(arena_redis_socket_t socket, const std::string& data,
                       Deadline deadline, std::string& error) {
    size_t offset = 0;
    while (offset < data.size()) {
      if (!wait_ready(socket, true, deadline, "request write", error)) return false;
      // Winsock may report WSAENOBUFS for a very large nonblocking send instead
      // of accepting a partial write. Small chunks exercise normal backpressure
      // and keep each syscall bounded on both platforms.
      const auto chunk_size = std::min<size_t>(data.size() - offset, 16 * 1024);
#ifdef _WIN32
      const int sent = ::send(socket, data.data() + offset,
                              static_cast<int>(chunk_size), 0);
#else
      const auto sent = ::send(socket, data.data() + offset, chunk_size, MSG_NOSIGNAL);
#endif
      if (sent < 0 && (would_block(last_socket_error()) || interrupted(last_socket_error()))) continue;
      if (sent <= 0) {
        error = "Redis request write failed";
        return false;
      }
      offset += static_cast<size_t>(sent);
    }
    return true;
  }

  bool read_line(std::string& line, Deadline deadline, std::string& error) {
    while (true) {
      if (Clock::now() >= deadline) {
        error = "Redis response read timed out";
        return false;
      }
      const auto end = receive_buffer_.find("\r\n");
      if (end != std::string::npos) {
        line = receive_buffer_.substr(0, end);
        receive_buffer_.erase(0, end + 2);
        return true;
      }
      if (!wait_ready(socket_, false, deadline, "response read", error)) return false;
      char chunk[512];
#ifdef _WIN32
      const int received = ::recv(socket_, chunk, sizeof(chunk), 0);
#else
      const auto received = ::recv(socket_, chunk, sizeof(chunk), 0);
#endif
      if (received < 0 && (would_block(last_socket_error()) || interrupted(last_socket_error()))) continue;
      if (received <= 0) {
        error = "Redis response read failed";
        return false;
      }
      receive_buffer_.append(chunk, static_cast<size_t>(received));
      if (receive_buffer_.size() > 1024 * 1024) {
        error = "Redis response exceeded 1 MiB";
        return false;
      }
    }
  }

  bool command_reply(const std::vector<std::string>& parts, std::string& reply,
                     std::string& error) {
    std::string request = "*" + std::to_string(parts.size()) + "\r\n";
    for (const auto& part : parts) {
      request += "$" + std::to_string(part.size()) + "\r\n" + part + "\r\n";
    }
    // One deadline covers all partial sends and the complete response. Neither
    // partial progress nor a peer trickling bytes can extend the command wait.
    const auto deadline = Clock::now() + std::chrono::milliseconds(timeouts_.io_ms);
    if (!send_all(socket_, request, deadline, error)) {
      close_unlocked();
      return false;
    }
    if (!read_line(reply, deadline, error)) {
      close_unlocked();
      return false;
    }
    if (!reply.empty() && reply[0] == '-') {
      error = reply.substr(1);
      close_unlocked();
      return false;
    }
    error.clear();
    return true;
  }

  arena_redis_socket_t socket_ = kArenaRedisInvalidSocket;
  std::string receive_buffer_;
  mutable std::mutex mutex_;
  Timeouts timeouts_;
  std::shared_ptr<Resolution> resolution_;
#ifdef _WIN32
  std::shared_ptr<WinsockLease> winsock_;
#endif
  unsigned int injected_failures_remaining_ = 0;
};

}  // namespace arena::ranking
