#include <cstdlib>
#include <iostream>
#include <utility>

#include "server/battle/bonus_effects.h"

int main() {
  const auto require = [](bool condition, const char* message) {
    if (!condition) { std::cerr << message << "\n"; std::exit(1); }
  };
  arena::battle::Bonus attack;
  arena::battle::Bonus heal;
  require(attack.consume() == 0 && attack.value == 0 && attack.uses == 0, "empty bonus is inert");
  require(attack.apply(3, 2) && heal.apply(4, 3), "independent slots");
  require(attack.consume() == 3 && attack.value == 3 && attack.uses == 1 && heal.uses == 3,
          "only matching action consumes");
  require(attack.apply(1, 1) && attack.consume() == 1 && attack.value == 0 && attack.uses == 0,
          "replace not stack and clear on expiry");
  require(attack.consume() == 0, "expired cannot apply again");
  require(attack.apply(10, 5), "maximum bounds");
  for (const auto parameters : {std::pair<int, int>{0, 2}, {11, 2}, {2, 0}, {2, 6}, {-1, 2}, {2, -1}}) {
    require(!attack.apply(parameters.first, parameters.second) && attack.value == 10 && attack.uses == 5,
            "invalid refresh preserves state");
  }
  for (int count = 0; count < 5; ++count) require(attack.consume() == 10, "finite consumption");
  require(attack.consume() == 0 && attack.value == 0 && attack.uses == 0, "maximum count expires");
  require(heal.consume() == 4 && heal.uses == 2, "heal independent after attack expiry");
  return 0;
}
