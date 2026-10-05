#include "server/gateway/socket.h"

#include <algorithm>
#include <cerrno>
#ifndef _WIN32
#include <fcntl.h>
#include <poll.h>
#endif

namespace arena::gateway {
namespace {

bool interrupted() {
#ifdef _WIN32
  return WSAGetLastError() == WSAEINTR;
#else
  return errno == EINTR;
#endif
}

bool would_block() {
#ifdef _WIN32
  return WSAGetLastError() == WSAEWOULDBLOCK;
#else
  return errno == EAGAIN || errno == EWOULDBLOCK;
#endif
}

bool wait_ready(Socket socket, bool writing, const std::atomic<bool>* cancelled,
                std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max()) {
  while (!cancelled || !cancelled->load(std::memory_order_acquire)) {
    const auto now = std::chrono::steady_clock::now();
    if (now >= deadline) return false;
    auto wait_ms = std::chrono::milliseconds(250);
    if (deadline != std::chrono::steady_clock::time_point::max()) {
      wait_ms = std::min(wait_ms, std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now));
      if (wait_ms.count() == 0) wait_ms = std::chrono::milliseconds(1);
    }
#ifdef _WIN32
    fd_set descriptors;
    FD_ZERO(&descriptors);
    FD_SET(socket, &descriptors);
    timeval timeout{0, static_cast<long>(wait_ms.count() * 1000)};
    const int result = select(0, writing ? nullptr : &descriptors,
                              writing ? &descriptors : nullptr, nullptr, &timeout);
#else
    pollfd descriptor{socket, static_cast<short>(writing ? POLLOUT : POLLIN), 0};
    const int result = poll(&descriptor, 1, static_cast<int>(wait_ms.count()));
#endif
    if (result > 0) return std::chrono::steady_clock::now() < deadline;
    if (result < 0 && !interrupted()) return false;
  }
  return false;
}

}

bool initialize_sockets() {
#ifdef _WIN32
  WSADATA data{};
  return WSAStartup(MAKEWORD(2, 2), &data) == 0;
#else
  return true;
#endif
}

bool set_nonblocking(Socket socket) {
#ifdef _WIN32
  u_long enabled = 1;
  return ioctlsocket(socket, FIONBIO, &enabled) == 0;
#else
  const int flags = fcntl(socket, F_GETFL, 0);
  return flags >= 0 && fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0;
#endif
}

void shutdown_socket(Socket socket) {
  if (socket == kInvalidSocket) return;
#ifdef _WIN32
  ::shutdown(socket, SD_BOTH);
#else
  ::shutdown(socket, SHUT_RDWR);
#endif
}

void close_socket(Socket socket) {
  if (socket == kInvalidSocket) return;
#ifdef _WIN32
  closesocket(socket);
#else
  ::close(socket);
#endif
}

bool send_all(Socket socket, const std::uint8_t* data, std::size_t size,
              const std::atomic<bool>* cancelled) {
  while (size != 0) {
    if (cancelled && cancelled->load(std::memory_order_acquire)) return false;
#ifdef _WIN32
    const int count = ::send(socket, reinterpret_cast<const char*>(data), static_cast<int>(size), 0);
#else
    const auto count = ::send(socket, data, size, MSG_NOSIGNAL);
#endif
    if (count < 0 && interrupted()) continue;
    if (count < 0 && would_block()) {
      if (!wait_ready(socket, true, cancelled)) return false;
      continue;
    }
    if (count <= 0) return false;
    data += count;
    size -= static_cast<std::size_t>(count);
  }
  return true;
}

bool receive_all(Socket socket, std::uint8_t* data, std::size_t size,
                 const std::atomic<bool>* cancelled, std::chrono::steady_clock::time_point deadline) {
  while (size != 0) {
    if (cancelled && cancelled->load(std::memory_order_acquire)) return false;
    if (std::chrono::steady_clock::now() >= deadline) return false;
#ifdef _WIN32
    const int count = ::recv(socket, reinterpret_cast<char*>(data), static_cast<int>(size), 0);
#else
    const auto count = ::recv(socket, data, size, 0);
#endif
    if (count < 0 && interrupted()) continue;
    if (count < 0 && would_block()) {
      if (!wait_ready(socket, false, cancelled, deadline)) return false;
      continue;
    }
    if (count <= 0) return false;
    data += count;
    size -= static_cast<std::size_t>(count);
  }
  return std::chrono::steady_clock::now() < deadline;
}

}
