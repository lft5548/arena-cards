#include "server/app/identifiers.h"

#include <random>
#include <sstream>

namespace arena::app {

std::string make_token() {
  std::random_device source;
  std::ostringstream output;
  output << std::hex;
  for (int count = 0; count < 8; ++count) output << source();
  return output.str();
}

std::string make_match_id(std::atomic<unsigned long long>& sequence) {
  return "match-" + make_token().substr(0, 32) + "-" + std::to_string(sequence.fetch_add(1));
}

std::uint64_t replay_seed_for(const std::string& value) {
  std::uint64_t hash = 14695981039346656037ull;
  for (const unsigned char byte : value) { hash ^= byte; hash *= 1099511628211ull; }
  return hash;
}

}
