#pragma once

#include <atomic>
#include <cstdint>
#include <string>

namespace arena::app {

std::string make_token();
std::string make_match_id(std::atomic<unsigned long long>& sequence);
std::uint64_t replay_seed_for(const std::string& value);

}
