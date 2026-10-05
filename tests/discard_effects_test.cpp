#include <cstdlib>
#include <iostream>
#include <vector>

#include "server/battle/discard_effects.h"

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) {
      std::cerr << "discard effects test failed: " << message << "\n";
      std::exit(1);
    }
  };
  std::vector<int> hand{7, 7, 2, 3};
  std::vector<int> discard{8};
  require(arena::battle::discard_front(hand, discard, 2) == 2, "actual count");
  require(hand == std::vector<int>({2, 3}), "remove duplicates in hand order");
  require(discard == std::vector<int>({8, 7, 7}), "append history in order");
  require(arena::battle::discard_front(hand, discard, 3) == 2, "short hand");
  require(hand.empty() && discard == std::vector<int>({8, 7, 7, 2, 3}), "short hand history");
  require(arena::battle::discard_front(hand, discard, 1) == 0, "empty hand is legal");
  require(discard.size() == 5, "empty hand preserves history");
  hand = {9, 2, 1};
  for (int count : {-1, 0, 4, 1000}) {
    require(!arena::battle::valid_discard_count(count), "invalid count");
    require(arena::battle::discard_front(hand, discard, count) == 0, "reject invalid count");
    require(hand == std::vector<int>({9, 2, 1}) && discard.size() == 5, "rejection is inert");
  }
  for (int count : {1, 2, 3}) {
    hand = {9, 2, 1};
    discard.clear();
    require(arena::battle::valid_discard_count(count), "bounded count");
    require(arena::battle::discard_front(hand, discard, count) == count, "each supported count");
    require(hand.size() == static_cast<std::size_t>(3 - count), "bounded hand size");
    require(discard.size() == static_cast<std::size_t>(count), "bounded history size");
  }
  return 0;
}
