#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <optional>
#include <queue>
#include <string>
#include <unordered_map>

#include "proto/messages.h"
#include "server/battle/battle_engine.h"
#include "server/battle/battle_replay.h"
#include "server/config/card_catalog.h"
#include "server/room/room_command.h"
#include "server/room/recovery_checkpoint.h"

namespace arena {
struct Runtime;

class Room : public std::enable_shared_from_this<Room> {
 public:
  Room(std::shared_ptr<Session> first, std::shared_ptr<Session> second,
        std::shared_ptr<const config::CardCatalog> catalog, std::shared_ptr<Runtime> runtime);
  void start();
  void launch_actor();
  void request_shutdown();
  void enqueue_play(const std::shared_ptr<Session>& session, const std::string& match,
                    unsigned long long turn, int card, unsigned long long action, const std::string& request_id);
  void enqueue_end_turn(const std::shared_ptr<Session>& session, const std::string& match,
                        unsigned long long turn, unsigned long long action, const std::string& request_id);
  void enqueue_disconnect(const std::shared_ptr<Session>& session);
  void enqueue_reconnect(int slot, const std::string& token, const std::shared_ptr<Session>& session,
                         const std::string& request_id);
  void assign_token(int slot, const std::string& token, const std::string& user);
  void send_snapshot();
  bool settlement_pending() const { return settlement_pending_.load(std::memory_order_acquire); }
  const std::string& match_id() const { return match_id_; }
  bool restore_checkpoint(const room::RecoveryCheckpoint& checkpoint, std::string& error,
                          std::uint64_t sequence = 0, std::uint64_t snapshot_sequence = 0,
                          bool tail_format = false);
  bool save_checkpoint(std::string& error);
  void attach_recovered_tokens();

 private:
  struct ActionReceipt { std::string signature; std::string response; };
  struct DeferredMessage {
    std::weak_ptr<Session> session;
    proto::MessageType type;
    std::string payload;
    std::uint64_t revision = 0;
    std::string request_id;
  };
  struct PendingDisconnect {
    std::shared_ptr<Session> session;
    std::chrono::steady_clock::time_point disconnected_at;
    std::int64_t deadline_ms = 0;
  };
  std::mutex command_mu_;
  std::condition_variable command_cv_;
  std::queue<room::Command> commands_;
  std::weak_ptr<Session> p_[2];
  std::shared_ptr<const config::CardCatalog> catalog_;
  std::shared_ptr<Runtime> runtime_;
  battle::BattleEngine battle_;
  battle::BattleReplay replay_;
  std::string token_[2];
  std::string player_user_[2];
  std::chrono::steady_clock::time_point disconnected_at_[2]{};
  std::unordered_map<unsigned long long, ActionReceipt> actions_[2];
  std::queue<unsigned long long> action_order_[2];
  bool done_ = false;
  bool shutdown_requested_ = false;
  bool terminal_replay_recorded_ = false;
  bool replay_valid_ = true;
  bool replay_persist_started_ = false;
  bool replay_persist_in_flight_ = false;
  std::uint64_t snapshot_interval_ = 5;
  std::uint64_t last_snapshot_revision_ = 0;
  std::atomic<bool> settlement_pending_{false};
  bool settlement_in_flight_ = false;
  bool settlement_error_notified_ = false;
  std::chrono::steady_clock::time_point settlement_retry_at_{};
  std::chrono::steady_clock::time_point settlement_started_at_{};
  std::string pending_result_;
  std::string match_id_;
  std::chrono::steady_clock::time_point turn_started_ = std::chrono::steady_clock::now();
  std::int64_t turn_deadline_ms_ = 0;
  std::array<std::int64_t, 2> disconnect_deadline_ms_{0, 0};
  bool recovery_write_pending_ = false;
  bool recovery_initial_write_pending_ = false;
  std::uint64_t recovery_sequence_ = 0;
  std::uint64_t recovery_snapshot_sequence_ = 0;
  bool recovery_tail_format_ = false;
  std::optional<room::RecoveryCheckpoint> recovery_committed_;
  std::chrono::steady_clock::time_point recovery_retry_at_{};
  std::chrono::steady_clock::time_point finished_resume_until_{};
  std::vector<DeferredMessage> deferred_messages_;
  std::vector<PendingDisconnect> pending_disconnects_;

  void broadcast(proto::MessageType type, const std::string& payload);
  void record_battle_event(const std::string& payload);
  void defer_broadcast(proto::MessageType type, const std::string& payload);
  void publish_checkpoint();
  bool persist_checkpoint();
  room::RecoveryCheckpoint recovery_checkpoint() const;
  void reset_turn_clock();
  void prepare_result(const std::string& payload);
  void snap();
  void finish(proto::MessageType type, const std::string& payload);
  void settlement_complete(bool ok, const std::string& error);
  void start_settlement();
  void start_replay_persist();
  void persist_snapshot(bool force);
  void replay_persist_complete(bool ok, const std::string& path, const std::string& error);
  void start_impl();
  void append_replay_event(int player, unsigned long long turn, unsigned long long action,
                           const std::string& type, const std::string& payload);
  bool replay_action(int player, unsigned long long action, const std::string& signature,
                      const std::shared_ptr<Session>& session, const std::string& request_id);
  void remember_action(int player, unsigned long long action, const std::string& signature,
                        const std::string& response);
  void post(room::Command command);
  void actor_loop();
  void play(const std::shared_ptr<Session>& session, const std::string& match, unsigned long long turn,
             int card, unsigned long long action, const std::string& request_id);
  void end_turn(const std::shared_ptr<Session>& session, const std::string& match,
                 unsigned long long turn, unsigned long long action, const std::string& request_id);
  void apply_action(const std::shared_ptr<Session>& session, const std::string& match,
                     battle::BattleCommand command, const std::string& request_id);
  void disconnect(const std::shared_ptr<Session>& session);
  void disconnect_at(const PendingDisconnect& disconnect);
  void reconnect(int slot, const std::string& token, const std::shared_ptr<Session>& session,
                 const std::string& request_id);
};

}
