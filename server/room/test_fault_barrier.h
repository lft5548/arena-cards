#pragma once
#include <string>

#if defined(ARENA_ENABLE_TEST_FAULTS) && ARENA_ENABLE_TEST_FAULTS
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <thread>

namespace arena::room {
// Only explicit test builds can pause after a durable write and before delivery.
inline void test_fault_barrier(const char* point, const std::string& match_id) {
  const char* selected = std::getenv("ARENA_TEST_FAULT_POINT");
  const char* directory = std::getenv("ARENA_TEST_FAULT_DIRECTORY");
  if (!selected || !directory || std::string(selected) != point) return;
  static std::atomic<bool> entered{false};
  if (entered.exchange(true)) return;
  const auto root = std::filesystem::path(directory);
  const auto ready = root / "ready";
  {
    std::ofstream output(root / "ready.tmp", std::ios::trunc);
    output << point << '\n' << match_id << '\n';
    output.close();
    if (!output) throw std::runtime_error("could not write test fault barrier");
  }
  std::filesystem::rename(root / "ready.tmp", ready);
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
  while (!std::filesystem::exists(root / "release")) {
    if (std::chrono::steady_clock::now() >= deadline)
      throw std::runtime_error("test fault barrier timed out");
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
}
}  // namespace arena::room
#else
namespace arena::room {
inline void test_fault_barrier(const char*, const std::string&) {}
}  // namespace arena::room
#endif
