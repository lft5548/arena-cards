#pragma once

namespace arena::battle {

struct Bonus {
  int value = 0;
  int uses = 0;

  static bool valid_parameters(int strength, int count) {
    return strength >= 1 && strength <= 10 && count >= 1 && count <= 5;
  }

  bool apply(int strength, int count) {
    if (!valid_parameters(strength, count)) return false;
    value = strength;
    uses = count;
    return true;
  }

  int consume() {
    if (uses == 0) return 0;
    const int bonus = value;
    --uses;
    if (uses == 0) value = 0;
    return bonus;
  }
};

}
