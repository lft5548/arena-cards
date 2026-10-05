#pragma once

#include <cstddef>
#include <cstdint>
#include <atomic>
#include <chrono>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace arena::gateway {

#ifdef _WIN32
using Socket = SOCKET;
using SocketLength = int;
constexpr Socket kInvalidSocket = INVALID_SOCKET;
#else
using Socket = int;
using SocketLength = socklen_t;
constexpr Socket kInvalidSocket = -1;
#endif

bool initialize_sockets();
bool set_nonblocking(Socket socket);
void shutdown_socket(Socket socket);
void close_socket(Socket socket);
bool send_all(Socket socket, const std::uint8_t* data, std::size_t size,
              const std::atomic<bool>* cancelled = nullptr);
bool receive_all(Socket socket, std::uint8_t* data, std::size_t size,
                 const std::atomic<bool>* cancelled = nullptr,
                 std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::time_point::max());

}
