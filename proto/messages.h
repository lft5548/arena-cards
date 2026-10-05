#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <algorithm>

namespace arena::proto {

// The envelope keeps the four-byte length and two-byte message id for both
// protocol generations.  ProtocolId is negotiated out of band (or through
// ProtocolHelloReq); message id ranges must remain disjoint so a binary
// payload can never be interpreted as a TextV1 message.
enum class ProtocolId : std::uint8_t {
  TextV1 = 1,
  ProtoV1 = 2,
};

constexpr std::uint8_t kTextV1Version = 1;
constexpr std::uint8_t kProtoV1Version = 1;
constexpr std::uint16_t kProtoV1MessageBase = 0x4000;

enum class MessageType : std::uint16_t {
  LoginReq = 1,
  LoginResp = 2,
  MatchJoinReq = 3,
  MatchCancelReq = 4,
  MatchFound = 5,
  PlayCardReq = 6,
  BattleSnapshot = 7,
  BattleEvent = 8,
  Error = 9,
  Heartbeat = 10,
  Pong = 11,
  ReconnectReq = 12,
  ReconnectResp = 13,
  MatchResult = 14,
  EndTurnReq = 15,
  AdminRoomsReq = 16,
  AdminRoomsResp = 17,
  ActionAck = 18,
  // Optional capability negotiation. Existing clients never send these ids.
  ProtocolHelloReq = 19,
  ProtocolHelloResp = 20,
  LeaderboardReq = 21,
  LeaderboardResp = 22,
};

enum class ProtoMessageType : std::uint16_t {
  LoginReq = kProtoV1MessageBase + 1,
  LoginResp = kProtoV1MessageBase + 2,
  MatchJoinReq = kProtoV1MessageBase + 3,
  MatchCancelReq = kProtoV1MessageBase + 4,
  MatchFound = kProtoV1MessageBase + 5,
  PlayCardReq = kProtoV1MessageBase + 6,
  BattleSnapshot = kProtoV1MessageBase + 7,
  BattleEvent = kProtoV1MessageBase + 8,
  Error = kProtoV1MessageBase + 9,
  Heartbeat = kProtoV1MessageBase + 10,
  Pong = kProtoV1MessageBase + 11,
  ReconnectReq = kProtoV1MessageBase + 12,
  ReconnectResp = kProtoV1MessageBase + 13,
  MatchResult = kProtoV1MessageBase + 14,
  EndTurnReq = kProtoV1MessageBase + 15,
  AdminRoomsReq = kProtoV1MessageBase + 16,
  AdminRoomsResp = kProtoV1MessageBase + 17,
  ActionAck = kProtoV1MessageBase + 18,
  LeaderboardReq = kProtoV1MessageBase + 21,
  LeaderboardResp = kProtoV1MessageBase + 22,
};

static_assert(static_cast<std::uint16_t>(MessageType::LeaderboardResp) <
                  kProtoV1MessageBase,
              "TextV1 and ProtoV1 message id ranges must stay disjoint");

// Wire format: uint32 big-endian frame size (2-byte type + payload),
// uint16 big-endian message type, UTF-8 payload bytes.
inline std::uint32_t read_u32_be(const std::uint8_t* p) {
  return (std::uint32_t(p[0]) << 24) | (std::uint32_t(p[1]) << 16) |
         (std::uint32_t(p[2]) << 8) | std::uint32_t(p[3]);
}
inline std::uint16_t read_u16_be(const std::uint8_t* p) {
  return std::uint16_t((std::uint16_t(p[0]) << 8) | std::uint16_t(p[1]));
}
inline void write_u32_be(std::uint8_t* p, std::uint32_t v) {
  p[0]=std::uint8_t(v>>24); p[1]=std::uint8_t(v>>16); p[2]=std::uint8_t(v>>8); p[3]=std::uint8_t(v);
}
inline void write_u16_be(std::uint8_t* p, std::uint16_t v) { p[0]=std::uint8_t(v>>8); p[1]=std::uint8_t(v); }

inline std::vector<std::uint8_t> encode(MessageType type, const std::string& payload) {
  const std::uint32_t body = static_cast<std::uint32_t>(2 + payload.size());
  std::vector<std::uint8_t> out(4 + body);
  write_u32_be(out.data(), body);
  write_u16_be(out.data()+4, static_cast<std::uint16_t>(type));
  std::copy(payload.begin(), payload.end(), out.begin()+6);
  return out;
}

inline std::vector<std::uint8_t> encode(ProtoMessageType type,
                                        const std::string& payload) {
  const std::uint32_t body = static_cast<std::uint32_t>(2 + payload.size());
  std::vector<std::uint8_t> out(4 + body);
  write_u32_be(out.data(), body);
  write_u16_be(out.data() + 4, static_cast<std::uint16_t>(type));
  std::copy(payload.begin(), payload.end(), out.begin() + 6);
  return out;
}

} // namespace arena::proto

