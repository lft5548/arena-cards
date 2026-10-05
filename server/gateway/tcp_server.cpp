#include "server/gateway/tcp_server.h"

#include <algorithm>
#include <cerrno>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>
#ifndef _WIN32
#include <poll.h>
#endif

namespace arena::gateway {

TcpServer::TcpServer(std::uint16_t port, std::string bind_address, HandlerFactory factory,
                     Metrics& metrics, SessionOptions options)
    : port_(port), bind_address_(std::move(bind_address)), factory_(std::move(factory)),
      metrics_(metrics), options_(options) {
  if (options_.max_connections == 0 || options_.max_connections > 4096)
    throw std::invalid_argument("max connections must be in 1..4096");
}

int TcpServer::run(StopPredicate stop_requested, ShutdownCallback begin_shutdown,
                   std::chrono::milliseconds shutdown_timeout) {
  if (shutdown_timeout.count() <= 0)
    throw std::invalid_argument("shutdown timeout must be positive");
  std::vector<std::weak_ptr<Session>> sessions;
  sessions.reserve(options_.max_connections);
  if (!initialize_sockets()) return 1;
  const Socket listener = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listener == kInvalidSocket) return 1;
  int reuse = 1;
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&reuse), sizeof(reuse));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port_);
  if (bind_address_.empty() || ::inet_pton(AF_INET, bind_address_.c_str(), &address.sin_addr) != 1 ||
      bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
      listen(listener, 128) < 0 || !set_nonblocking(listener)) {
    close_socket(listener);
    return 1;
  }
  std::cout << "arena_server listening on " << bind_address_ << ":" << port_ << "\n";
  int result = 0;
  for (;;) {
    if (stop_requested && stop_requested()) break;
    sessions.erase(std::remove_if(sessions.begin(), sessions.end(), [](const auto& weak) {
      if (auto session = weak.lock()) {
        session->check_heartbeat();
        return false;
      }
      return true;
    }), sessions.end());
#ifdef _WIN32
    fd_set readable;
    FD_ZERO(&readable);
    FD_SET(listener, &readable);
    timeval timeout{0, 250000};
    const int ready = select(0, &readable, nullptr, nullptr, &timeout);
    const bool interrupted = ready < 0 && WSAGetLastError() == WSAEINTR;
#else
    pollfd descriptor{listener, POLLIN, 0};
    const int ready = poll(&descriptor, 1, 250);
    const bool interrupted = ready < 0 && errno == EINTR;
#endif
    if (ready < 0 && !interrupted) {
      std::cerr << "listener wait failed\n";
      result = 1;
      break;
    }
#ifndef _WIN32
    if (ready > 0 && (descriptor.revents & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
      std::cerr << "listener socket failed\n";
      result = 1;
      break;
    }
#endif
    if (ready <= 0) continue;
    if (stop_requested && stop_requested()) break;
    sockaddr_in peer{};
    SocketLength length = sizeof(peer);
    const Socket accepted = accept(listener, reinterpret_cast<sockaddr*>(&peer), &length);
    if (accepted == kInvalidSocket) continue;
    // This accept loop is the sole Session creator; destructors only release slots.
    // Reserve resources through Session lifetime, including its retiring I/O threads.
    if (metrics_.active_sessions.load(std::memory_order_relaxed) >= options_.max_connections) {
      metrics_.connections_rejected.fetch_add(1, std::memory_order_relaxed);
      close_socket(accepted);
      continue;
    }
    std::shared_ptr<Session> session;
    try {
      auto handler = factory_();
      if (!handler) { close_socket(accepted); continue; }
      session = std::make_shared<Session>(accepted, std::move(handler), metrics_, options_);
      sessions.emplace_back(session);
      session->start();
    } catch (const std::exception& error) {
      if (session) session->close();
      else close_socket(accepted);
      std::cerr << "session start failed: " << error.what() << "\n";
    }
  }
  close_socket(listener);
  const auto deadline = Clock::now() + shutdown_timeout;
  std::cout << "shutdown: accept stopped\n" << std::flush;
  if (begin_shutdown) {
    try { begin_shutdown(deadline); }
    catch (const std::exception& error) {
      std::cerr << "application shutdown start failed: " << error.what() << "\n";
      result = 1;
    }
  }
  for (const auto& weak : sessions)
    if (auto session = weak.lock()) session->close();
  std::cout << "shutdown: transports closed\n" << std::flush;
  for (const auto& weak : sessions) {
    if (auto session = weak.lock()) {
      if (!session->wait_idle(deadline)) return 2;
    }
  }
  // Worker captures release the final Session ownership after signaling idle.
  // Keep its Metrics reference valid until socket destruction has completed.
  while (metrics_.active_sessions.load(std::memory_order_acquire) != 0) {
    if (Clock::now() >= deadline) return 2;
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  std::cout << "shutdown: transports drained\n" << std::flush;
  return result;
}

}
