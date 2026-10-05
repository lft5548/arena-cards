#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "proto/messages.h"

namespace arena::gateway {

struct ProtobufRequest {
  proto::MessageType type = proto::MessageType::Error;
  std::string payload;
  std::string request_id;
};

bool decode_protobuf_request(std::uint16_t message_id, const std::string& bytes,
                             ProtobufRequest& request, std::string& error);

std::vector<std::uint8_t> encode_protobuf_response(
    proto::MessageType type, const std::string& payload,
    std::uint64_t revision = 0, const std::string& request_id = {});

}
