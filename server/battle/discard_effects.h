#pragma once

#include <algorithm>
#include <vector>

namespace arena::battle {

inline bool valid_discard_count(int count) {
  return count >= 1 && count <= 3;
}

inline int discard_front(std::vector<int>& hand, std::vector<int>& discard, int count) {
  if (!valid_discard_count(count)) return 0;
  const int actual = static_cast<int>(std::min(hand.size(), static_cast<std::size_t>(count)));
  discard.insert(discard.end(), hand.begin(), hand.begin() + actual);
  hand.erase(hand.begin(), hand.begin() + actual);
  return actual;
}

}
