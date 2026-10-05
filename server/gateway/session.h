#pragma once

#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <vector>

#include "proto/messages.h"
#include "server/gateway/socket.h"
#include "server/gateway/request_limiter.h"

namespace arena::gateway {

class Session;

class SessionHandler {
 public:
  virtual ~SessionHandler() = default;
  virtual void on_connected(const std::shared_ptr<Session>& session) = 0;
  virtual void on_request(proto::MessageType type, const std::string& payload,
                          const std::string& request_id) = 0;
  virtual void on_disconnected() = 0;
};

struct Metrics {
  std::atomic<unsigned int> active_sessions{0};
  std::atomic<unsigned long long> connections_rejected{0};
  std::atomic<unsigned long long> requests_rate_limited{0};
  std::atomic<unsigned long long> heartbeat_timeouts{0};
  std::atomic<unsigned long long> send_frames_enqueued{0};
  std::atomic<unsigned long long> send_frames_dropped{0};
  std::atomic<unsigned long long> send_queue_high_watermark_bytes{0};
};

struct SessionOptions {
  std::size_t max_frames = 128;
  std::size_t max_bytes = 256 * 1024;
  std::uint32_t max_connections = 512;
  std::uint32_t requests_per_second = 200;
  std::uint32_t request_burst = 400;
  std::uint32_t heartbeat_timeout_ms = 30000;
  static SessionOptions from_environment();
};

class Session : public std::enable_shared_from_this<Session> {
 public:
  Session(Socket socket, std::shared_ptr<SessionHandler> handler, Metrics& metrics,
          SessionOptions options = {});
  ~Session();
  void start();
  void send(proto::MessageType type, const std::string& payload, std::uint64_t revision = 0,
            const std::string& request_id = {});
  void close();
  // TcpServer also checks this while the reader is inside an application callback.
  bool check_heartbeat();
  bool idle() const { return workers_.load(std::memory_order_acquire) == 0; }
  // Includes both I/O loops and the disconnect callback; final shared_ptr
  // captures may be released immediately after idle is signaled.
  bool wait_idle(std::chrono::steady_clock::time_point deadline);
  bool closed() const { return closed_.load(std::memory_order_acquire); }
  proto::ProtocolId protocol() const { return protocol_.load(std::memory_order_acquire); }

 private:
  Socket socket_;
  std::shared_ptr<SessionHandler> handler_;
  Metrics& metrics_;
  SessionOptions options_;
  RequestLimiter request_limiter_;
  std::mutex send_mutex_;
  std::condition_variable send_cv_;
  std::queue<std::vector<std::uint8_t>> send_queue_;
  std::size_t queued_send_bytes_ = 0;
  std::atomic<bool> closed_{false};
  std::atomic<std::int64_t> heartbeat_deadline_us_{0};
  std::atomic<unsigned int> workers_{0};
  std::mutex idle_mutex_;
  std::condition_variable idle_cv_;
  std::atomic<proto::ProtocolId> protocol_{proto::ProtocolId::TextV1};
  bool protocol_locked_ = false;
  void run();
  void writer_loop();
  void handle_frame(std::uint16_t message_id, const std::string& payload);
  void negotiate(const std::string& payload);
  void refresh_heartbeat();
  std::chrono::steady_clock::time_point heartbeat_deadline() const;
  void worker_finished();
  bool close_once();
  bool close_locked();
};

}
