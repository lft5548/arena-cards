#include "server/gateway/session.h"

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <utility>

#include "server/gateway/frame_codec.h"
#if ARENA_WITH_PROTOBUF
#include "server/gateway/protobuf_codec.h"
#endif

namespace arena::gateway {

namespace {
bool request_type(proto::MessageType type) {
  switch (type) {
    case proto::MessageType::LoginReq:
    case proto::MessageType::MatchJoinReq:
    case proto::MessageType::MatchCancelReq:
    case proto::MessageType::PlayCardReq:
    case proto::MessageType::Heartbeat:
    case proto::MessageType::ReconnectReq:
    case proto::MessageType::EndTurnReq:
    case proto::MessageType::AdminRoomsReq:
    case proto::MessageType::LeaderboardReq: return true;
    default: return false;
  }
}

bool valid_utf8(const std::string& text) {
  for (std::size_t offset = 0; offset < text.size();) {
    const auto first = static_cast<unsigned char>(text[offset++]);
    if (first < 128) {
      if (first < 32 || first == 127) return false;
      continue;
    }
    unsigned int value = 0;
    unsigned int remaining = 0;
    unsigned int minimum = 0;
    if (first >= 0xc2 && first <= 0xdf) { value = first & 0x1f; remaining = 1; minimum = 0x80; }
    else if (first >= 0xe0 && first <= 0xef) { value = first & 0x0f; remaining = 2; minimum = 0x800; }
    else if (first >= 0xf0 && first <= 0xf4) { value = first & 0x07; remaining = 3; minimum = 0x10000; }
    else return false;
    if (offset + remaining > text.size()) return false;
    while (remaining--) {
      const auto byte = static_cast<unsigned char>(text[offset++]);
      if ((byte & 0xc0) != 0x80) return false;
      value = (value << 6) | (byte & 0x3f);
    }
    if (value < minimum || value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff)) return false;
  }
  return true;
}

bool valid_text_request(proto::MessageType type, const std::string& payload) {
  if (!valid_utf8(payload)) return false;
  switch (type) {
    case proto::MessageType::Heartbeat:
    case proto::MessageType::MatchJoinReq:
    case proto::MessageType::MatchCancelReq: return payload.empty();
    case proto::MessageType::LoginReq:
      return payload.size() <= 64 && payload.find_first_of(";=\r\n") == std::string::npos;
    case proto::MessageType::ReconnectReq:
      return !payload.empty() && payload.size() <= 128 && payload.find_first_of(";= ") == std::string::npos;
    case proto::MessageType::AdminRoomsReq:
    case proto::MessageType::LeaderboardReq:
      if (payload.empty()) return true;
      [[fallthrough]];
    case proto::MessageType::PlayCardReq:
    case proto::MessageType::EndTurnReq: {
      std::istringstream stream(payload);
      std::string field;
      std::unordered_map<std::string, std::string> fields;
      while (std::getline(stream, field, ';')) {
        const auto separator = field.find('=');
        if (separator == std::string::npos || separator == 0) return false;
        if (!fields.emplace(field.substr(0, separator), field.substr(separator + 1)).second) return false;
      }
      if (fields.empty()) return false;
      const auto identifier = [&fields](const char* key, bool required) {
        const auto found = fields.find(key);
        if (found == fields.end()) return !required;
        if (found->second.size() > 128 || (required && found->second.empty())) return false;
        for (const unsigned char byte : found->second)
          if (byte < 33 || byte > 126 || byte == ';' || byte == '=') return false;
        return true;
      };
      if (!identifier("request_id", false)) return false;
      if (type == proto::MessageType::AdminRoomsReq) {
        return fields.size() == 1 && fields.find("request_id") != fields.end();
      }
      if (type == proto::MessageType::LeaderboardReq) {
        const auto limit = fields.find("limit");
        if (limit == fields.end()) return true;
        unsigned int value = 0;
        const auto parsed = std::from_chars(limit->second.data(), limit->second.data() + limit->second.size(), value);
        return parsed.ec == std::errc() && parsed.ptr == limit->second.data() + limit->second.size();
      }
      const auto unsigned_decimal = [&fields](const char* key) {
        const auto found = fields.find(key);
        if (found == fields.end()) return false;
        std::uint64_t value = 0;
        const auto& text = found->second;
        const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
        return parsed.ec == std::errc() && parsed.ptr == text.data() + text.size();
      };
      if (!identifier("match_id", true) || !unsigned_decimal("turn_id") || !unsigned_decimal("action_id")) return false;
      if (type == proto::MessageType::EndTurnReq) return true;
      const auto card = fields.find("card");
      if (card == fields.end()) return false;
      int value = 0;
      const auto& text = card->second;
      const auto parsed = std::from_chars(text.data(), text.data() + text.size(), value);
      return parsed.ec == std::errc() && parsed.ptr == text.data() + text.size();
    }
    default: return false;
  }
}

std::uint32_t network_limit(const char* name, std::uint32_t fallback, std::uint32_t maximum,
                            std::uint32_t minimum = 1) {
#ifdef _WIN32
  char* allocated = nullptr;
  std::size_t size = 0;
  if (_dupenv_s(&allocated, &size, name) != 0)
    throw std::runtime_error(std::string("cannot read ") + name);
  const std::string text = allocated ? allocated : "";
  std::free(allocated);
#else
  const char* raw = std::getenv(name);
  const std::string text = raw ? raw : "";
#endif
  if (text.empty()) return fallback;
  const auto error = std::string(name) + " must be in " + std::to_string(minimum) + ".." + std::to_string(maximum);
  std::uint32_t value = 0;
  for (const char digit : text) {
    if (digit < '0' || digit > '9' || value > maximum / 10)
      throw std::invalid_argument(error);
    value = value * 10 + static_cast<unsigned int>(digit - '0');
    if (value > maximum)
      throw std::invalid_argument(error);
  }
  if (value < minimum)
    throw std::invalid_argument(error);
  return value;
}
}  // namespace

SessionOptions SessionOptions::from_environment() {
  SessionOptions options;
  options.max_connections = network_limit("ARENA_MAX_CONNECTIONS", 512, 4096);
  options.requests_per_second = network_limit("ARENA_REQUEST_RATE_PER_SECOND", 200, 10000);
  options.request_burst = network_limit("ARENA_REQUEST_BURST", 400, 20000);
  options.heartbeat_timeout_ms = network_limit("ARENA_HEARTBEAT_TIMEOUT_MS", 30000, 300000, 100);
#ifdef _WIN32
  char* allocated = nullptr;
  std::size_t size = 0;
  if (_dupenv_s(&allocated, &size, "ARENA_SEND_QUEUE_MAX_BYTES") != 0) return options;
  const char* raw = allocated;
#else
  const char* raw = std::getenv("ARENA_SEND_QUEUE_MAX_BYTES");
#endif
  if (raw && *raw) {
    char* end = nullptr;
    const auto value = std::strtoull(raw, &end, 10);
    if (end != raw && value != 0)
      options.max_bytes = std::min<std::size_t>(static_cast<std::size_t>(value), options.max_bytes);
  }
#ifdef _WIN32
  std::free(allocated);
#endif
  return options;
}

Session::Session(Socket socket, std::shared_ptr<SessionHandler> handler, Metrics& metrics,
                 SessionOptions options)
    : socket_(socket), handler_(std::move(handler)), metrics_(metrics), options_(options),
      request_limiter_(options.requests_per_second, options.request_burst) {
  if (!handler_) throw std::invalid_argument("session handler is required");
  if (options_.heartbeat_timeout_ms < 100 || options_.heartbeat_timeout_ms > 300000)
    throw std::invalid_argument("heartbeat timeout must be in 100..300000 ms");
  if (!set_nonblocking(socket_)) throw std::runtime_error("cannot configure session socket");
  refresh_heartbeat();
  metrics_.active_sessions.fetch_add(1, std::memory_order_relaxed);
}

Session::~Session() {
  close();
  close_socket(socket_);
  metrics_.active_sessions.fetch_sub(1, std::memory_order_relaxed);
}

void Session::start() {
  auto self = shared_from_this();
  try {
    handler_->on_connected(self);
    workers_.store(2, std::memory_order_release);
    try {
      std::thread([self]() { self->writer_loop(); self->worker_finished(); }).detach();
    } catch (...) { worker_finished(); worker_finished(); throw; }
    try {
      std::thread([self]() { self->run(); self->worker_finished(); }).detach();
    } catch (...) { worker_finished(); throw; }
  } catch (...) {
    close();
    throw;
  }
}

void Session::send(proto::MessageType type, const std::string& payload, std::uint64_t revision,
                   const std::string& request_id) {
#if !ARENA_WITH_PROTOBUF
  static_cast<void>(revision);
  static_cast<void>(request_id);
#endif
  std::vector<std::uint8_t> frame;
  try {
#if ARENA_WITH_PROTOBUF
    if (type != proto::MessageType::ProtocolHelloResp && protocol() == proto::ProtocolId::ProtoV1)
      frame = encode_protobuf_response(type, payload, revision, request_id);
    else
#endif
      frame = proto::encode(type, payload);
  } catch (const std::exception& error) {
    std::cerr << "protocol encode failed: " << error.what() << "\n";
    close();
    return;
  }
  std::lock_guard<std::mutex> lock(send_mutex_);
  if (closed_) return;
  if (frame.size() > options_.max_bytes || send_queue_.size() >= options_.max_frames ||
      queued_send_bytes_ + frame.size() > options_.max_bytes) {
    metrics_.send_frames_dropped.fetch_add(1, std::memory_order_relaxed);
    closed_ = true;
    send_queue_ = std::queue<std::vector<std::uint8_t>>();
    queued_send_bytes_ = 0;
    shutdown_socket(socket_);
    send_cv_.notify_all();
    return;
  }
  queued_send_bytes_ += frame.size();
  send_queue_.push(std::move(frame));
  metrics_.send_frames_enqueued.fetch_add(1, std::memory_order_relaxed);
  const auto bytes = static_cast<unsigned long long>(queued_send_bytes_);
  auto previous = metrics_.send_queue_high_watermark_bytes.load(std::memory_order_relaxed);
  while (previous < bytes && !metrics_.send_queue_high_watermark_bytes.compare_exchange_weak(
      previous, bytes, std::memory_order_relaxed, std::memory_order_relaxed)) {}
  send_cv_.notify_one();
}

bool Session::close_once() {
  std::lock_guard<std::mutex> lock(send_mutex_);
  return close_locked();
}

bool Session::close_locked() {
  if (closed_.exchange(true, std::memory_order_acq_rel)) return false;
  send_queue_ = std::queue<std::vector<std::uint8_t>>();
  queued_send_bytes_ = 0;
  shutdown_socket(socket_);
  send_cv_.notify_all();
  return true;
}

void Session::close() { close_once(); }

void Session::refresh_heartbeat() {
  std::lock_guard<std::mutex> lock(send_mutex_);
  if (closed()) return;
  const auto now = std::chrono::steady_clock::now();
  if (heartbeat_deadline_us_.load(std::memory_order_relaxed) != 0 && now >= heartbeat_deadline()) return;
  const auto deadline = now + std::chrono::milliseconds(options_.heartbeat_timeout_ms);
  heartbeat_deadline_us_.store(std::chrono::duration_cast<std::chrono::microseconds>(deadline.time_since_epoch()).count(),
                               std::memory_order_release);
}

std::chrono::steady_clock::time_point Session::heartbeat_deadline() const {
  return std::chrono::steady_clock::time_point(std::chrono::microseconds(
      heartbeat_deadline_us_.load(std::memory_order_acquire)));
}

bool Session::check_heartbeat() {
  std::lock_guard<std::mutex> lock(send_mutex_);
  if (closed() || std::chrono::steady_clock::now() < heartbeat_deadline()) return false;
  if (!close_locked()) return false;
  metrics_.heartbeat_timeouts.fetch_add(1, std::memory_order_relaxed);
  return true;
}

void Session::worker_finished() {
  // Pair the predicate update with the wait mutex to avoid a missed wakeup.
  std::lock_guard<std::mutex> lock(idle_mutex_);
  workers_.fetch_sub(1, std::memory_order_release);
  idle_cv_.notify_all();
}

bool Session::wait_idle(std::chrono::steady_clock::time_point deadline) {
  std::unique_lock<std::mutex> lock(idle_mutex_);
  return idle_cv_.wait_until(lock, deadline, [this] { return idle(); });
}

void Session::writer_loop() {
  for (;;) {
    std::vector<std::uint8_t> frame;
    {
      std::unique_lock<std::mutex> lock(send_mutex_);
      send_cv_.wait(lock, [this]() { return closed_ || !send_queue_.empty(); });
      if (closed_) return;
      frame = std::move(send_queue_.front());
      send_queue_.pop();
      queued_send_bytes_ -= frame.size();
    }
    if (!send_all(socket_, frame.data(), frame.size(), &closed_)) { close(); return; }
  }
}

void Session::run() {
  try {
    while (!closed_) {
      Frame frame;
      const auto result = receive_frame(socket_, frame, &closed_, heartbeat_deadline());
      if (result != FrameResult::Ready) {
        check_heartbeat();
        if (result == FrameResult::InvalidLength) send(proto::MessageType::Error, "code=bad_frame");
        break;
      }
      if (closed_) break;
      if (check_heartbeat()) break;
      if (!request_limiter_.consume()) {
        metrics_.requests_rate_limited.fetch_add(1, std::memory_order_relaxed);
        break;  // Normal close/disconnect path; no business dispatch or ACK.
      }
      handle_frame(frame.message_id, frame.payload);
    }
  } catch (const std::exception& error) {
    std::cerr << "session request failed: " << error.what() << "\n";
  }
  close();
  try {
    handler_->on_disconnected();
  } catch (const std::exception& error) {
    std::cerr << "session disconnect failed: " << error.what() << "\n";
  }
}

void Session::negotiate(const std::string& payload) {
  std::string selected;
  std::string version;
  bool selected_seen = false;
  bool version_seen = false;
  std::istringstream stream(payload);
  std::string item;
  while (std::getline(stream, item, ';')) {
    const auto separator = item.find('=');
    if (separator == std::string::npos) { send(proto::MessageType::Error, "code=invalid_protocol_hello"); return; }
    const auto key = item.substr(0, separator);
    const auto value = item.substr(separator + 1);
    if (key == "protocol" && !selected_seen && !value.empty()) { selected = value; selected_seen = true; }
    else if (key == "version" && !version_seen && !value.empty()) { version = value; version_seen = true; }
    else { send(proto::MessageType::Error, "code=invalid_protocol_hello"); return; }
  }
  if (!version.empty() && version != "1") { send(proto::MessageType::Error, "code=unsupported_protocol_version"); return; }
  if (!selected.empty()) {
    if (selected != "proto_v1" && selected != "text_v1") { send(proto::MessageType::Error, "code=unsupported_protocol"); return; }
    const auto desired = selected == "proto_v1" ? proto::ProtocolId::ProtoV1 : proto::ProtocolId::TextV1;
    if (protocol_locked_ && desired != protocol_.load()) { send(proto::MessageType::Error, "code=protocol_locked"); return; }
#if !ARENA_WITH_PROTOBUF
    if (desired == proto::ProtocolId::ProtoV1) { send(proto::MessageType::Error, "code=protocol_unavailable"); return; }
#endif
    protocol_.store(desired);
    protocol_locked_ = true;
  }
  send(proto::MessageType::ProtocolHelloResp, std::string("text_v1=1;proto_v1=1;proto_v1_runtime=") +
      (ARENA_WITH_PROTOBUF ? "1" : "0") + ";selected=" +
      (protocol_.load() == proto::ProtocolId::ProtoV1 ? "proto_v1" : "text_v1") +
      ";version=1;proto_message_base=16384");
  refresh_heartbeat();
}

void Session::handle_frame(std::uint16_t message_id, const std::string& bytes) {
  if (message_id == static_cast<std::uint16_t>(proto::MessageType::ProtocolHelloReq)) { negotiate(bytes); return; }
  const bool binary = message_id >= proto::kProtoV1MessageBase;
  if (binary && protocol_.load() != proto::ProtocolId::ProtoV1) {
    send(proto::MessageType::Error, "code=protocol_not_negotiated"); return;
  }
  if (!binary && protocol_.load() == proto::ProtocolId::ProtoV1) {
    send(proto::MessageType::Error, "code=protocol_mismatch"); return;
  }
  if (binary) {
#if ARENA_WITH_PROTOBUF
    gateway::ProtobufRequest request;
    std::string error;
    if (!gateway::decode_protobuf_request(message_id, bytes, request, error)) {
      send(proto::MessageType::Error, "code=" + error, 0, request.request_id); return;
    }
    protocol_locked_ = true;
    refresh_heartbeat();
    if (check_heartbeat() || closed()) return;
    if (request.type == proto::MessageType::Heartbeat)
      send(proto::MessageType::Pong, "", 0, request.request_id);
    else handler_->on_request(request.type, request.payload, request.request_id);
#endif
  } else {
    protocol_locked_ = true;
    const auto type = static_cast<proto::MessageType>(message_id);
    if (!request_type(type)) { send(proto::MessageType::Error, "code=unknown_message"); return; }
    if (valid_text_request(type, bytes)) refresh_heartbeat();
    if (check_heartbeat() || closed()) return;
    if (type == proto::MessageType::Heartbeat) send(proto::MessageType::Pong, "");
    else handler_->on_request(type, bytes, {});
  }
}


}
