#pragma once

#include <algorithm>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "server/battle/status_effects.h"
#include "server/battle/discard_effects.h"
#include "server/battle/bonus_effects.h"

namespace arena::config {

enum class Effect {
  Damage,
  Heal,
  Shield,
  Draw,
  Poison,
  Regen,
  Discard,
  Burn,
  AttackBoost,
  HealBoost,
};

struct CardDefinition {
  int id = 0;
  std::string name;
  int cost = 0;
  Effect effect = Effect::Damage;
  int value = 0;
  int duration = 0;
};

class CardCatalog {
 public:
  bool load_csv(const std::string& path, std::string& error) {
    cards_.clear();
    std::ifstream input(path);
    if (!input) {
      error = "cannot open card config: " + path;
      return false;
    }

    std::string line;
    bool header_seen = false;
    bool has_duration = false;
    int line_number = 0;
    while (std::getline(input, line)) {
      ++line_number;
      trim_in_place(line);
      if (line.empty() || line[0] == '#') continue;
      if (!header_seen) {
        has_duration = line == "id,name,cost,effect,value,duration";
        if (!has_duration && line != "id,name,cost,effect,value") {
          error = "invalid card config header at line " + std::to_string(line_number);
          return false;
        }
        header_seen = true;
        continue;
      }

      const auto fields = split(line);
      if (fields.size() != (has_duration ? 6u : 5u)) {
        error = "unexpected field count at line " + std::to_string(line_number);
        return false;
      }
      CardDefinition card;
      if (!parse_int(fields[0], card.id) || !parse_int(fields[2], card.cost) ||
          !parse_int(fields[4], card.value) ||
          (has_duration && !parse_int(fields[5], card.duration))) {
        error = "invalid numeric field at line " + std::to_string(line_number);
        return false;
      }
      card.name = fields[1];
      if (card.name.empty() || card.id <= 0 || card.id > 1000 || card.cost < 0 ||
          card.cost > 10 || card.value < 0 || card.value > 1000) {
        error = "card value out of range at line " + std::to_string(line_number);
        return false;
      }
      if (!parse_effect(fields[3], card.effect)) {
        error = "unknown card effect at line " + std::to_string(line_number);
        return false;
      }
      if (card.effect == Effect::Discard &&
          (!battle::valid_discard_count(card.value) || card.duration != 0)) {
        error = "invalid discard parameters at line " + std::to_string(line_number);
        return false;
      }
      const bool is_status = card.effect == Effect::Poison || card.effect == Effect::Regen || card.effect == Effect::Burn;
      const bool is_bonus = card.effect == Effect::AttackBoost || card.effect == Effect::HealBoost;
      if (is_bonus && !battle::Bonus::valid_parameters(card.value, card.duration)) {
        error = "invalid bonus parameters at line " + std::to_string(line_number);
        return false;
      }
      if ((is_status && !battle::StatusEffects::valid_parameters(card.value, card.duration)) ||
          (!is_status && !is_bonus && card.duration != 0)) {
        error = "invalid status parameters at line " + std::to_string(line_number);
        return false;
      }
      if (!cards_.emplace(card.id, card).second) {
        error = "duplicate card id at line " + std::to_string(line_number);
        return false;
      }
    }

    if (!header_seen || cards_.empty()) {
      error = "card config has no cards";
      return false;
    }
    for (int id : {1, 2, 3}) {
      if (cards_.find(id) == cards_.end()) {
        error = "starter card id missing: " + std::to_string(id);
        return false;
      }
    }
    return true;
  }

  const CardDefinition* find(int id) const {
    const auto it = cards_.find(id);
    return it == cards_.end() ? nullptr : &it->second;
  }

  std::vector<int> starter_deck() const {
    std::vector<int> cycle;
    for (const auto& entry : cards_) cycle.push_back(entry.first);
    std::sort(cycle.begin(), cycle.end());
    std::vector<int> deck;
    for (int repeat = 0; repeat < 3; ++repeat) {
      deck.insert(deck.end(), cycle.begin(), cycle.end());
    }
    return deck;
  }

  const char* rules_name() const {
    bool has_discard = false;
    bool has_burn = false;
    for (const auto& entry : cards_) {
      if (entry.second.effect == Effect::AttackBoost || entry.second.effect == Effect::HealBoost) return "bonus_v1";
      if (entry.second.effect == Effect::Burn) has_burn = true;
      if (entry.second.effect == Effect::Discard) has_discard = true;
    }
    return has_burn ? "turn_end_v1" : has_discard ? "discard_v1" : "status_v1";
  }

  static const char* effect_name(Effect effect) {
    switch (effect) {
      case Effect::Damage: return "damage";
      case Effect::Heal: return "heal";
      case Effect::Shield: return "shield";
      case Effect::Draw: return "draw";
      case Effect::Poison: return "poison";
      case Effect::Regen: return "regen";
      case Effect::Discard: return "discard";
      case Effect::Burn: return "burn";
      case Effect::AttackBoost: return "attack_boost";
      case Effect::HealBoost: return "heal_boost";
    }
    return "unknown";
  }

 private:
  static void trim_in_place(std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) {
      value.clear();
      return;
    }
    const auto last = value.find_last_not_of(" \t\r\n");
    value = value.substr(first, last - first + 1);
  }

  static std::vector<std::string> split(const std::string& line) {
    std::vector<std::string> fields;
    std::stringstream stream(line);
    std::string field;
    while (std::getline(stream, field, ',')) {
      trim_in_place(field);
      fields.push_back(field);
    }
    return fields;
  }

  static bool parse_int(const std::string& text, int& value) {
    try {
      size_t consumed = 0;
      value = std::stoi(text, &consumed);
      return consumed == text.size();
    } catch (...) {
      return false;
    }
  }

  static bool parse_effect(const std::string& text, Effect& effect) {
    if (text == "damage") effect = Effect::Damage;
    else if (text == "heal") effect = Effect::Heal;
    else if (text == "shield") effect = Effect::Shield;
    else if (text == "draw") effect = Effect::Draw;
    else if (text == "poison") effect = Effect::Poison;
    else if (text == "regen") effect = Effect::Regen;
    else if (text == "discard") effect = Effect::Discard;
    else if (text == "burn") effect = Effect::Burn;
    else if (text == "attack_boost") effect = Effect::AttackBoost;
    else if (text == "heal_boost") effect = Effect::HealBoost;
    else return false;
    return true;
  }

  std::unordered_map<int, CardDefinition> cards_;
};

}  // namespace arena::config
