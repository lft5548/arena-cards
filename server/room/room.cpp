#include "server/room/room.h"

#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <sstream>
#include <thread>
#include <utility>

#include "server/app/runtime.h"
#include "server/app/session.h"
#include "server/app/identifiers.h"
#include "server/persistence/mysql_store.h"
#include "server/battle/replay_store.h"
#include "server/battle/snapshot_store.h"
#include "server/room/test_fault_barrier.h"
#include "server/room/recovery_tail.h"

namespace arena {
using proto::MessageType;
using config::CardCatalog;
static constexpr int kTurnTimeoutSeconds = 30;
static constexpr int kReconnectWindowSeconds = 15;

Room::Room(std::shared_ptr<Session> first, std::shared_ptr<Session> second,
           std::shared_ptr<const CardCatalog> catalog, std::shared_ptr<Runtime> runtime)
    : catalog_(std::move(catalog)), runtime_(std::move(runtime)), battle_(catalog_) {
  p_[0] = first;
  p_[1] = second;
  match_id_ = app::make_match_id(runtime_->match_sequence);
  replay_ = battle::BattleReplay(app::replay_seed_for(match_id_));
#if defined(ARENA_ENABLE_TEST_FAULTS) && ARENA_ENABLE_TEST_FAULTS
  // Paired persistence experiments use identical rules and RNG. Production
  // builds do not recognize this test-only override.
  if (const char* seed = std::getenv("ARENA_TEST_REPLAY_SEED"))
    replay_ = battle::BattleReplay(std::strtoull(seed, nullptr, 10));
#endif
  battle_.set_rng_seed(replay_.seed());
  if (const char* configured = std::getenv("ARENA_SNAPSHOT_INTERVAL")) {
    const auto value = std::strtoull(configured, nullptr, 10);
    if (value > 0) snapshot_interval_ = value;
  }
}

void Room::record_battle_event(const std::string& p) {
    int player=-1;unsigned long long event_turn=battle_.state().turn_id;unsigned long long action_id=0;
    std::stringstream fields(p);std::string field;
    while(std::getline(fields,field,';')){const auto split=field.find('=');if(split==std::string::npos)continue;const auto key=field.substr(0,split);const auto value=field.substr(split+1);if(key=="player")player=std::atoi(value.c_str());else if(key=="turn_id")event_turn=std::strtoull(value.c_str(),nullptr,10);else if(key=="action_id")action_id=std::strtoull(value.c_str(),nullptr,10);}
    append_replay_event(player,event_turn,action_id,"battle_event",p);
}
void Room::broadcast(MessageType t,const std::string&p){
  for(int viewer=0;viewer<2;++viewer)if(auto session=p_[viewer].lock())session->send(t,t==MessageType::MatchFound?p+";player_index="+std::to_string(viewer):p,replay_.events().size());
  if(t==MessageType::MatchResult){runtime_->counters.active_rooms.fetch_sub(1);for(auto&w:p_)if(auto s=w.lock())s->clear_room(this);if(runtime_->room_recovery_enabled)finished_resume_until_=std::chrono::steady_clock::now()+std::chrono::seconds(kReconnectWindowSeconds);else for(const auto&token:token_)if(!token.empty())runtime_->rooms.erase(token,this);}
}
void Room::reset_turn_clock() {
  turn_started_ = std::chrono::steady_clock::now();
  turn_deadline_ms_ = room::unix_milliseconds() + kTurnTimeoutSeconds * 1000;
}

room::RecoveryCheckpoint Room::recovery_checkpoint() const {
  room::RecoveryCheckpoint checkpoint;
  checkpoint.match_id = match_id_;
  checkpoint.rules_fingerprint = room::recovery_rules_fingerprint(*catalog_);
  checkpoint.snapshot = battle_.snapshot(replay_.events().size());
  checkpoint.replay = replay_;
  checkpoint.turn_deadline_ms = turn_deadline_ms_;
  checkpoint.disconnect_deadline_ms = disconnect_deadline_ms_;
  checkpoint.pending_result = pending_result_;
  checkpoint.replay_valid = replay_valid_;
  checkpoint.terminal_replay_recorded = terminal_replay_recorded_;
  for (int player = 0; player < 2; ++player) {
    checkpoint.users[player] = player_user_[player];
    checkpoint.tokens[player] = token_[player];
    auto order = action_order_[player];
    while (!order.empty()) {
      const auto id = order.front();
      order.pop();
      const auto& receipt = actions_[player].at(id);
      checkpoint.receipts[player].push_back({id, receipt.signature, receipt.response});
    }
  }
  return checkpoint;
}

bool Room::save_checkpoint(std::string& error) {
  if (!runtime_->room_recovery_enabled) return true;
  if (!runtime_->mysql_store) { error = "room recovery requires MySQL"; return false; }
  const auto started = std::chrono::steady_clock::now();
  auto checkpoint = recovery_checkpoint();
  persistence::RoomRecoveryWrite write;
  write.expected_sequence = recovery_sequence_;
  if (!recovery_initial_write_pending_ && recovery_sequence_ == std::numeric_limits<std::uint64_t>::max()) {
    error = "room recovery sequence exhausted";
    return false;
  }
  write.sequence = recovery_initial_write_pending_ ? 0 : recovery_sequence_ + 1;
  const bool append_tail = !recovery_initial_write_pending_ && runtime_->room_recovery_tail &&
      recovery_tail_format_ && recovery_committed_ &&
      write.sequence - recovery_snapshot_sequence_ < runtime_->recovery_snapshot_interval;
  write.kind = !runtime_->room_recovery_tail ? persistence::RoomRecoveryWriteKind::FullSnapshot :
      append_tail ? persistence::RoomRecoveryWriteKind::Tail : persistence::RoomRecoveryWriteKind::Snapshot;
  if (append_tail) {
    if (!room::RecoveryTailCodec::encode(*recovery_committed_, checkpoint, recovery_sequence_,
                                         write.sequence, write.payload, error)) return false;
  } else write.payload = room::RecoveryCodec::encode(checkpoint);
  const auto& document = write.payload;
  if (document.size() > room::RecoveryCodec::kMaximumBytes) {
    error = "room recovery checkpoint exceeds size limit";
    return false;
  }
  const auto encoded = std::chrono::steady_clock::now();
  auto& counters = runtime_->counters;
  counters.recovery_checkpoint_attempts.fetch_add(1, std::memory_order_relaxed);
  counters.recovery_checkpoint_bytes_total.fetch_add(document.size(), std::memory_order_relaxed);
  metrics::update_high_watermark(counters.recovery_checkpoint_bytes_max,
                                 static_cast<unsigned long long>(document.size()));
  counters.recovery_checkpoint_serialize_us_total.fetch_add(
      std::chrono::duration_cast<std::chrono::microseconds>(encoded - started).count(), std::memory_order_relaxed);
  bool saved;
  persistence::CheckpointTimings timings;
  if (recovery_initial_write_pending_) {
    saved = runtime_->mysql_store->begin_recoverable_match(
        match_id_, player_user_[0], player_user_[1], document, error, &timings, runtime_->room_recovery_tail);
    if (saved) recovery_initial_write_pending_ = false;
  } else saved = runtime_->mysql_store->save_room_recovery(match_id_, write, error, &timings);
  if (saved) {
    recovery_sequence_ = write.sequence;
    if (!append_tail) recovery_snapshot_sequence_ = write.sequence;
    recovery_tail_format_ = runtime_->room_recovery_tail;
    if (runtime_->room_recovery_tail) recovery_committed_ = std::move(checkpoint);
  }
  counters.recovery_checkpoint_lock_wait_us_total.fetch_add(timings.lock_wait_us, std::memory_order_relaxed);
  counters.recovery_checkpoint_connection_us_total.fetch_add(timings.connection_us, std::memory_order_relaxed);
  counters.recovery_checkpoint_sql_us_total.fetch_add(timings.sql_us, std::memory_order_relaxed);
  counters.recovery_checkpoint_commit_us_total.fetch_add(timings.commit_us, std::memory_order_relaxed);
  counters.recovery_checkpoint_queue_wait_us_total.fetch_add(timings.queue_wait_us, std::memory_order_relaxed);
  if (timings.batch_leader) counters.recovery_checkpoint_batches.fetch_add(1, std::memory_order_relaxed);
  metrics::update_high_watermark(counters.recovery_checkpoint_batch_items_max,
                                 static_cast<unsigned long long>(timings.batch_size));
  const auto elapsed = static_cast<unsigned long long>(std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now() - encoded).count());
  counters.recovery_checkpoint_write_us_total.fetch_add(elapsed, std::memory_order_relaxed);
  metrics::update_high_watermark(counters.recovery_checkpoint_write_us_max, elapsed);
  (saved ? counters.recovery_checkpoint_successes : counters.recovery_checkpoint_failures)
      .fetch_add(1, std::memory_order_relaxed);
  return saved;
}

bool Room::restore_checkpoint(const room::RecoveryCheckpoint& checkpoint, std::string& error,
                              std::uint64_t sequence, std::uint64_t snapshot_sequence, bool tail_format) {
  if (checkpoint.rules_fingerprint != room::recovery_rules_fingerprint(*catalog_)) {
    error = "recovery card configuration or rules version differs";
    return false;
  }
  if (!battle_.restore(checkpoint.snapshot, &error)) return false;
  recovery_sequence_ = sequence;
  recovery_snapshot_sequence_ = snapshot_sequence;
  recovery_tail_format_ = tail_format;
  if (runtime_->room_recovery_tail) recovery_committed_ = checkpoint;
  for (const auto& player : checkpoint.snapshot.state.players)
    for (const auto* list : {&player.hand, &player.deck, &player.refill_deck, &player.discard})
      for (const int id : *list) if (!catalog_->find(id)) {
        error = "recovery references an unknown card";
        return false;
      }
  match_id_ = checkpoint.match_id;
  replay_ = checkpoint.replay;
  replay_valid_ = checkpoint.replay_valid;
  terminal_replay_recorded_ = checkpoint.terminal_replay_recorded;
  pending_result_ = checkpoint.pending_result;
  settlement_pending_.store(!pending_result_.empty(), std::memory_order_release);
  turn_deadline_ms_ = checkpoint.turn_deadline_ms;
  const auto wall_now = room::unix_milliseconds();
  const auto steady_now = std::chrono::steady_clock::now();
  turn_started_ = steady_now + std::chrono::milliseconds(turn_deadline_ms_ - wall_now) -
                  std::chrono::seconds(kTurnTimeoutSeconds);
  for (int player = 0; player < 2; ++player) {
    token_[player] = checkpoint.tokens[player];
    player_user_[player] = checkpoint.users[player];
    for (const auto& receipt : checkpoint.receipts[player])
      remember_action(player, receipt.action_id, receipt.signature, receipt.response);
    // Connections that existed before the crash get one restart grace period.
    // An already disconnected player's durable deadline is retained exactly.
    disconnect_deadline_ms_[player] = checkpoint.disconnect_deadline_ms[player] == 0 ?
        wall_now + kReconnectWindowSeconds * 1000 : checkpoint.disconnect_deadline_ms[player];
    disconnected_at_[player] = steady_now +
        std::chrono::milliseconds(disconnect_deadline_ms_[player] - wall_now) -
        std::chrono::seconds(kReconnectWindowSeconds);
  }
  return true;
}

void Room::attach_recovered_tokens() {
  for (int player = 0; player < 2; ++player)
    runtime_->rooms.attach(token_[player], shared_from_this(), player);
}

void Room::defer_broadcast(MessageType type, const std::string& payload) {
  for (int viewer = 0; viewer < 2; ++viewer) {
    if (auto session = p_[viewer].lock())
      deferred_messages_.push_back({session, type,
          type == MessageType::MatchFound ? payload + ";player_index=" + std::to_string(viewer) : payload,
          static_cast<std::uint64_t>(replay_.events().size()), ""});
  }
}

bool Room::persist_checkpoint() {
  std::string error;
  if (save_checkpoint(error)) {
    recovery_write_pending_ = false;
    if (std::any_of(deferred_messages_.begin(), deferred_messages_.end(),
                    [](const auto& message) { return message.type == MessageType::ActionAck; }))
      room::test_fault_barrier("checkpoint_before_ack", match_id_);
    return true;
  }
  recovery_write_pending_ = true;
  recovery_retry_at_ = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  std::cerr << "room checkpoint pending for " << match_id_ << ": " << error << "\n";
  return false;
}

void Room::publish_checkpoint() {
  for (const auto& message : deferred_messages_)
    if (auto session = message.session.lock())
      session->send(message.type, message.payload, message.revision, message.request_id);
  deferred_messages_.clear();
  if (!pending_result_.empty()) {
    persist_snapshot(true);
    start_replay_persist();
    if (runtime_->mysql_store) {
      if (!settlement_in_flight_) start_settlement();
    } else {
      done_ = true;
      broadcast(MessageType::MatchResult, pending_result_);
    }
  } else snap();
}
void Room::append_replay_event(int player,unsigned long long turn_id,unsigned long long action_id,const std::string&type,const std::string&payload){battle::ReplayEvent event;event.revision=static_cast<std::uint64_t>(replay_.events().size())+1;event.turn_id=turn_id;event.action_id=action_id;event.player_index=player;event.type=type;event.payload=payload;std::string error;if(!replay_.append(event,&error)){replay_valid_=false;std::cerr<<"replay append failed for "<<match_id_<<": "<<error<<"\n";}}
void Room::persist_snapshot(bool force) {
  const std::string directory = persistence::environment("ARENA_REPLAY_DIR");
  if (directory.empty()) return;
  const auto revision = static_cast<std::uint64_t>(replay_.events().size());
  if (revision == 0 || (!force && revision - last_snapshot_revision_ < snapshot_interval_)) return;
  battle::SnapshotStore store(directory);
  std::string error;
  if (!store.save(match_id_, battle_.snapshot(revision), &error)) {
    std::cerr << "snapshot persist failed for " << match_id_ << ": " << error << "\n";
    return;
  }
  last_snapshot_revision_ = revision;
}
void Room::start_replay_persist(){
  if(replay_persist_started_)return;
  replay_persist_started_=true;
  const std::string directory=persistence::environment("ARENA_REPLAY_DIR");
  if(directory.empty())return;
  if(!replay_valid_){runtime_->counters.replay_save_failures.fetch_add(1,std::memory_order_relaxed);std::cerr<<"replay persist skipped for invalid log "<<match_id_<<"\n";return;}
  replay_persist_in_flight_=true;
  auto self=shared_from_this();
  const auto replay=replay_;
  const auto match_id=match_id_;
  runtime_->shutdown.launch([self,directory,replay,match_id](){
    bool ok=false;
    std::string error;
    try {
      battle::ReplayStore store(directory);
      ok=store.save(match_id,replay,&error);
    } catch(const std::exception& exception) {
      error=exception.what();
    } catch(...) {
      error="unknown replay persistence error";
    }
    room::Command command;
    command.type=room::CommandType::ReplayPersistComplete;
    command.replay_persist_ok=ok;
    command.replay_path=ok?battle::ReplayStore::path_for(directory,match_id).string():std::string();
    command.replay_persist_error=error;
    self->post(std::move(command));
  });
}
void Room::replay_persist_complete(bool ok,const std::string&path,const std::string&error){
  if(!replay_persist_in_flight_)return;
  replay_persist_in_flight_=false;
  if(ok){runtime_->counters.replays_saved.fetch_add(1,std::memory_order_relaxed);std::clog<<"replay persisted for "<<match_id_<<" at "<<path<<"\n";}
  else{runtime_->counters.replay_save_failures.fetch_add(1,std::memory_order_relaxed);std::cerr<<"replay persist failed for "<<match_id_<<": "<<error<<"\n";}
}
void Room::start_settlement() {
  if (settlement_in_flight_ || !runtime_->mysql_store) return;
  settlement_in_flight_ = true;
  settlement_started_at_ = std::chrono::steady_clock::now();
  runtime_->counters.settlements_started.fetch_add(1, std::memory_order_relaxed);
  if (settlement_error_notified_) runtime_->counters.settlement_retries.fetch_add(1, std::memory_order_relaxed);
  auto self = shared_from_this();
  auto store = runtime_->mysql_store;
  const auto match_id = match_id_;
  const auto player_a = player_user_[0];
  const auto player_b = player_user_[1];
  const auto result = pending_result_;
  const auto turns = static_cast<unsigned int>(battle_.state().turn_id);
  runtime_->shutdown.launch([self, store, match_id, player_a, player_b, result, turns]() {
    auto read = [&](const std::string& key) {
      const std::string prefix = key + "=";
      size_t pos = result.find(prefix);
      if (pos == std::string::npos) return std::string();
      pos += prefix.size();
      const size_t end = result.find(';', pos);
      return result.substr(pos, end == std::string::npos ? std::string::npos : end - pos);
    };
    const int winner = std::atoi(read("winner").c_str());
    std::string error;
    const bool ok = store->settle(match_id, player_a, player_b, winner, turns, read("reason"), error);
    if (ok && self->runtime_->room_recovery_enabled)
      room::test_fault_barrier("settlement_before_result", match_id);
    // The transaction already owns the durable Outbox row. Cache I/O and its
    // connection mutex belong to the Outbox worker, never to result delivery.
    room::Command command;
    command.type = room::CommandType::SettlementComplete;
    command.settlement_ok = ok;
    command.settlement_error = error;
    self->post(std::move(command));
  });
}
void Room::settlement_complete(bool ok, const std::string& error) {
  if (!settlement_in_flight_) return;
  settlement_in_flight_ = false;
  const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - settlement_started_at_).count();
  runtime_->counters.settlement_latency_ms_total.fetch_add(
      static_cast<unsigned long long>(std::max<long long>(0, elapsed)), std::memory_order_relaxed);
  metrics::update_high_watermark(runtime_->counters.settlement_latency_ms_max,
                                 static_cast<unsigned long long>(std::max<long long>(0, elapsed)));
  if (!ok) {
    runtime_->counters.settlements_failed.fetch_add(1, std::memory_order_relaxed);
    settlement_retry_at_ = std::chrono::steady_clock::now() + std::chrono::seconds(1);
    if (!settlement_error_notified_) {
      settlement_error_notified_ = true;
      for (auto& w : p_)
        if (auto s = w.lock()) s->send(MessageType::Error, "code=settlement_pending;match_id=" + match_id_);
    }
    std::cerr << "match settlement failed for " << match_id_ << ": " << error << "\n";
    return;
  }
  runtime_->counters.settlements_succeeded.fetch_add(1, std::memory_order_relaxed);
  settlement_pending_.store(false, std::memory_order_release);
  done_ = true;
  broadcast(MessageType::MatchResult, pending_result_);
  for (auto& w : p_)
    if (auto s = w.lock()) s->clear_room(this);
}
void Room::prepare_result(const std::string& payload) {
  if (!terminal_replay_recorded_) {
    append_replay_event(-1, battle_.state().turn_id, 0, "match_result", payload);
    terminal_replay_recorded_ = true;
    pending_result_ = payload + ";replay_seed=" + std::to_string(replay_.seed()) +
        ";replay_revision=" + std::to_string(replay_.events().size()) +
        ";replay_digest=" + std::to_string(replay_.digest()) +
        ";replay_valid=" + (replay_valid_ ? "1" : "0");
  }
  settlement_pending_.store(runtime_->mysql_store != nullptr, std::memory_order_release);
}
void Room::finish(MessageType type, const std::string& payload) {
  if (done_ || recovery_write_pending_) return;
  if (type == MessageType::MatchResult) {
    prepare_result(payload);
    if (!persist_checkpoint()) return;
    persist_snapshot(true);
    start_replay_persist();
    if (runtime_->mysql_store) { if (!settlement_in_flight_) start_settlement(); return; }
  }
  settlement_pending_.store(false, std::memory_order_release);
  done_ = true;
  broadcast(type, type == MessageType::MatchResult ? pending_result_ : payload);
  for (auto& player : p_) if (auto session = player.lock()) session->clear_room(this);
}
bool Room::replay_action(int player,unsigned long long action_id,const std::string&signature,const std::shared_ptr<Session>&session,const std::string&request_id){auto it=actions_[player].find(action_id);if(it==actions_[player].end()){if(settlement_pending_.load(std::memory_order_acquire)){session->send(MessageType::Error,"code=settlement_pending;match_id="+match_id_+";action_id="+std::to_string(action_id),replay_.events().size(),request_id);return true;}return false;}if(it->second.signature==signature)session->send(MessageType::ActionAck,it->second.response);else session->send(MessageType::Error,"code=action_id_conflict;action_id="+std::to_string(action_id),replay_.events().size(),request_id);return true;}
void Room::remember_action(int player,unsigned long long action_id,const std::string&signature,const std::string&response){actions_[player][action_id]={signature,response};action_order_[player].push(action_id);while(action_order_[player].size()>128){actions_[player].erase(action_order_[player].front());action_order_[player].pop();}}
void Room::assign_token(int slot,const std::string&token,const std::string&user){if(slot<0||slot>1)return;token_[slot]=token;player_user_[slot]=user;runtime_->rooms.attach(token,shared_from_this(),slot);}
void Room::disconnect(const std::shared_ptr<Session>& session) {
  disconnect_at({session, std::chrono::steady_clock::now(),
                 room::unix_milliseconds() + kReconnectWindowSeconds * 1000});
}
void Room::disconnect_at(const PendingDisconnect& disconnect) {
  if (done_) return;
  for (int slot = 0; slot < 2; ++slot) if (p_[slot].lock() == disconnect.session) {
    // The previous COMMIT may already be durable. Keep its candidate immutable
    // until an exact retry succeeds; this disconnect then gets its own sequence.
    // Capture the original deadline so waiting for MySQL grants no extra time.
    if (recovery_write_pending_) {
      if (std::none_of(pending_disconnects_.begin(), pending_disconnects_.end(),
                       [&](const auto& queued) { return queued.session == disconnect.session; }))
        pending_disconnects_.push_back(disconnect);
      return;
    }
    p_[slot].reset();
    disconnected_at_[slot] = disconnect.disconnected_at;
    disconnect_deadline_ms_[slot] = disconnect.deadline_ms;
    if (persist_checkpoint() && !deferred_messages_.empty()) publish_checkpoint();
    return;
  }
}
void Room::reconnect(int slot, const std::string& token, const std::shared_ptr<Session>& session,
                     const std::string& request_id) {
  const auto reject = [&](const char* code) {
    session->send(MessageType::ReconnectResp, "ok=0;code=" + std::string(code), 0, request_id);
  };
  if (slot < 0 || slot > 1 || token_[slot] != token) { reject("resume_expired"); return; }
  if (recovery_write_pending_) { reject("recovery_pending"); return; }
  if (done_) {
    if (!runtime_->room_recovery_enabled || std::chrono::steady_clock::now() >= finished_resume_until_) {
      reject("resume_expired"); return;
    }
    p_[slot] = session;
    session->set_identity(player_user_[slot], token);
    session->send(MessageType::ReconnectResp, "ok=1;match_id=" + match_id_ +
        ";player_index=" + std::to_string(slot), 0, request_id);
    snap();
    return;
  }
  const bool disconnected = disconnected_at_[slot].time_since_epoch().count() != 0;
  if (!disconnected) {
    auto current = p_[slot].lock();
    if (current == session || (current && !current->closed())) { reject("already_connected"); return; }
  } else if (std::chrono::steady_clock::now() - disconnected_at_[slot] >=
             std::chrono::seconds(kReconnectWindowSeconds)) { reject("resume_expired"); return; }
  p_[slot] = session;
  disconnected_at_[slot] = {};
  disconnect_deadline_ms_[slot] = 0;
  session->set_identity(player_user_[slot], token);
  session->set_room(shared_from_this());
  deferred_messages_.push_back({session, MessageType::ReconnectResp,
      "ok=1;match_id=" + match_id_ + ";player_index=" + std::to_string(slot), 0, request_id});
  if (persist_checkpoint()) publish_checkpoint();
}
void Room::snap() {
  const auto now = std::chrono::steady_clock::now();
  const auto age = std::chrono::duration_cast<std::chrono::milliseconds>(
      now - turn_started_).count();
  const auto remaining = std::max<long long>(0, kTurnTimeoutSeconds * 1000 - age);
  for (int viewer = 0; viewer < 2; ++viewer) {
    auto session = p_[viewer].lock();
    if (!session) continue;
    std::ostringstream payload;
    payload << "match_id=" << match_id_ << ";player_index=" << viewer
            << ";turn=" << battle_.state().turn << ";turn_id=" << battle_.state().turn_id
            << ";revision=" << battle_.state().turn_id << ";replay_seed=" << replay_.seed()
            << ";replay_revision=" << replay_.events().size()
            << ";replay_digest=" << replay_.digest()
            << ";replay_valid=" << (replay_valid_ ? 1 : 0)
            << ";remaining_ms=" << remaining << ";done=" << (done_ ? 1 : 0)
            << ";last_action_id=" << battle_.state().last_actions[viewer]
            << ";p0_hp=" << battle_.state().players[0].hp << ";p1_hp=" << battle_.state().players[1].hp
            << ";p0_energy=" << battle_.state().players[0].energy << ";p1_energy=" << battle_.state().players[1].energy
            << ";p0_shield=" << battle_.state().players[0].shield << ";p1_shield=" << battle_.state().players[1].shield;
    for (int index = 0; index < 2; ++index) {
      const auto& statuses = battle_.state().players[index].statuses;
      payload << ";p" << index << "_poison_value=" << statuses.poison.value
              << ";p" << index << "_poison_turns=" << statuses.poison.turns
              << ";p" << index << "_regen_value=" << statuses.regen.value
              << ";p" << index << "_regen_turns=" << statuses.regen.turns
              << ";p" << index << "_burn_value=" << statuses.burn.value
              << ";p" << index << "_burn_turns=" << statuses.burn.turns;
      payload << ";p" << index << "_attack_boost_value=" << battle_.state().players[index].attack_boost.value
              << ";p" << index << "_attack_boost_uses=" << battle_.state().players[index].attack_boost.uses
              << ";p" << index << "_heal_boost_value=" << battle_.state().players[index].heal_boost.value
              << ";p" << index << "_heal_boost_uses=" << battle_.state().players[index].heal_boost.uses;
    }
    payload << ";hand=";
    for (size_t index = 0; index < battle_.state().players[viewer].hand.size(); ++index) {
      if (index > 0) payload << ",";
      payload << battle_.state().players[viewer].hand[index];
    }
    payload << ";opponent_hand_count=" << battle_.state().players[1 - viewer].hand.size()
            << ";deck_count=" << battle_.state().players[viewer].deck.size()
            << ";discard_count=" << battle_.state().players[viewer].discard.size()
            << ";opponent_discard_count=" << battle_.state().players[1 - viewer].discard.size()
            << ";discard=";
    for (size_t index = 0; index < battle_.state().players[viewer].discard.size(); ++index) {
      if (index > 0) payload << ",";
      payload << battle_.state().players[viewer].discard[index];
    }
    session->send(MessageType::BattleSnapshot, payload.str());
    if (done_ && !pending_result_.empty()) session->send(MessageType::MatchResult, pending_result_);
  }
  persist_snapshot(false);
}
void Room::send_snapshot(){room::Command command;command.type=room::CommandType::Snapshot;post(std::move(command));}
void Room::post(room::Command command){
  runtime_->counters.room_commands_enqueued.fetch_add(1,std::memory_order_relaxed);
  {
    std::lock_guard<std::mutex> lock(command_mu_);
    commands_.push(std::move(command));
    metrics::update_high_watermark(runtime_->counters.room_command_queue_high_watermark,
                          static_cast<unsigned long long>(commands_.size()));
  }
  command_cv_.notify_one();
}
void Room::launch_actor(){auto self=shared_from_this();runtime_->shutdown.launch([self](){self->actor_loop();},self);}
void Room::request_shutdown(){room::Command command;command.type=room::CommandType::Stop;post(std::move(command));}
void Room::actor_loop(){
  for(;;){
    const auto maintenance_now = std::chrono::steady_clock::now();
    if (recovery_write_pending_ && maintenance_now >= recovery_retry_at_) {
      if (persist_checkpoint()) publish_checkpoint();
    }
    while (!recovery_write_pending_ && !pending_disconnects_.empty()) {
      const auto disconnected = std::move(pending_disconnects_.front());
      pending_disconnects_.erase(pending_disconnects_.begin());
      disconnect_at(disconnected);
    }
    if (!runtime_->shutdown.stopping() && !done_ && !recovery_write_pending_) {
      if (settlement_pending_.load(std::memory_order_acquire)) {
        if (!settlement_in_flight_ && maintenance_now >= settlement_retry_at_)
          finish(MessageType::MatchResult, pending_result_);
      } else {
        auto deadline = turn_started_ + std::chrono::seconds(kTurnTimeoutSeconds);
        int timed_out_player = battle_.state().turn;
        std::string reason = "turn_timeout";
        for (int slot = 0; slot < 2; ++slot) {
          if (disconnected_at_[slot].time_since_epoch().count() == 0) continue;
          const auto disconnected_deadline = disconnected_at_[slot] +
              std::chrono::seconds(kReconnectWindowSeconds);
          if (disconnected_deadline < deadline) {
            deadline = disconnected_deadline;
            timed_out_player = slot;
            reason = "reconnect_timeout";
          }
        }
        if (maintenance_now >= deadline)
          finish(MessageType::MatchResult, "match_id=" + match_id_ + ";winner=" +
              std::to_string(1 - timed_out_player) + ";reason=" + reason +
              ";turn_id=" + std::to_string(battle_.state().turn_id));
      }
    }
    if (shutdown_requested_ && !recovery_write_pending_ && !settlement_in_flight_ && !replay_persist_in_flight_) {
      // Every acknowledged action is already durable. An uncommitted candidate
      // keeps retrying above, while terminal SQL already in flight must finish.
      if (!runtime_->room_recovery_enabled && !done_ && runtime_->mysql_store) {
        std::string error;
        if (!runtime_->mysql_store->abort_match(match_id_, error)) {
          runtime_->shutdown.fail();
          std::cerr << "shutdown match abort failed for " << match_id_ << ": " << error << "\n";
        }
      }
      for (auto& weak : p_) if (auto session = weak.lock()) session->clear_room(this);
      return;
    }
    if (!runtime_->shutdown.stopping() && done_ && !replay_persist_in_flight_) {
      if (!runtime_->room_recovery_enabled) return;
      if (maintenance_now >= finished_resume_until_) {
        std::string error;
        if (runtime_->mysql_store->delete_room_checkpoint(match_id_, error)) {
          for (const auto& token : token_) runtime_->rooms.erase(token, this);
          return;
        }
      }
    }
    room::Command command;
    {
      std::unique_lock<std::mutex> lock(command_mu_);
      const bool has_command=command_cv_.wait_for(lock,std::chrono::milliseconds(250),[&]{return !commands_.empty();});
      if(!has_command&&commands_.empty()){
        lock.unlock();
        continue;
      }
      command=std::move(commands_.front());commands_.pop();
    }
    runtime_->counters.room_commands_processed.fetch_add(1,std::memory_order_relaxed);
    switch(command.type){
      case room::CommandType::PlayCard:if(auto s=command.session.lock())play(s,command.match_id,command.turn_id,command.card,command.action_id,command.request_id);break;
      case room::CommandType::EndTurn:if(auto s=command.session.lock())end_turn(s,command.match_id,command.turn_id,command.action_id,command.request_id);break;
      case room::CommandType::Disconnect:if(command.disconnect_session)disconnect(command.disconnect_session);break;
      case room::CommandType::Reconnect:if(auto s=command.session.lock())reconnect(command.player_index,command.token,s,command.request_id);break;
      case room::CommandType::Snapshot:if(!recovery_write_pending_)snap();break;
      case room::CommandType::SettlementComplete:
        settlement_complete(command.settlement_ok, command.settlement_error);
        break;
      case room::CommandType::ReplayPersistComplete:replay_persist_complete(command.replay_persist_ok,command.replay_path,command.replay_persist_error);break;
      case room::CommandType::Stop:shutdown_requested_=true;break;
      case room::CommandType::Start:start_impl();break;
      case room::CommandType::Tick:break;
    }
  }
}
void Room::enqueue_play(const std::shared_ptr<Session>&s,const std::string&match,unsigned long long turn,int card,unsigned long long action,const std::string&request_id){room::Command c;c.type=room::CommandType::PlayCard;c.session=s;c.match_id=match;c.turn_id=turn;c.card=card;c.action_id=action;c.request_id=request_id;post(std::move(c));}
void Room::enqueue_end_turn(const std::shared_ptr<Session>&s,const std::string&match,unsigned long long turn,unsigned long long action,const std::string&request_id){room::Command c;c.type=room::CommandType::EndTurn;c.session=s;c.match_id=match;c.turn_id=turn;c.action_id=action;c.request_id=request_id;post(std::move(c));}
void Room::enqueue_disconnect(const std::shared_ptr<Session>&s){room::Command c;c.type=room::CommandType::Disconnect;c.disconnect_session=s;post(std::move(c));}
void Room::enqueue_reconnect(int slot,const std::string&token,const std::shared_ptr<Session>&s,const std::string&request_id){room::Command c;c.type=room::CommandType::Reconnect;c.session=s;c.token=token;c.player_index=slot;c.request_id=request_id;post(std::move(c));}
void Room::start(){room::Command command;command.type=room::CommandType::Start;post(std::move(command));}
void Room::start_impl() {
  if (done_) return;
  std::ostringstream start_payload;
  start_payload << "match_id=" << match_id_ << ";rules=" << catalog_->rules_name() << ";deck=";
  for (size_t index = 0; index < battle_.state().players[0].refill_deck.size(); ++index) {
    if (index > 0) start_payload << ",";
    start_payload << battle_.state().players[0].refill_deck[index];
  }
  append_replay_event(-1, battle_.state().turn_id, 0, "match_start", start_payload.str());
  reset_turn_clock();
  if (runtime_->mysql_store) {
    std::string error;
    if (runtime_->room_recovery_enabled && player_user_[0] != player_user_[1]) {
      recovery_initial_write_pending_ = true;
      defer_broadcast(MessageType::MatchFound, "match_id=" + match_id_ +
          ";room=1;turn=" + std::to_string(battle_.state().turn) +
          ";turn_id=" + std::to_string(battle_.state().turn_id));
      if (persist_checkpoint()) publish_checkpoint();
      return;
    }
    const bool created = runtime_->room_recovery_enabled ?
        runtime_->mysql_store->begin_recoverable_match(match_id_, player_user_[0], player_user_[1],
            room::RecoveryCodec::encode(recovery_checkpoint()), error) :
        runtime_->mysql_store->begin_match(match_id_, player_user_[0], player_user_[1], error);
    if (!created) {
      runtime_->counters.active_rooms.fetch_sub(1);
      done_ = true;
      for (auto& player : p_) {
        if (auto session = player.lock()) {
          session->send(MessageType::Error, "code=match_persistence_failed");
          session->clear_room(this);
        }
      }
      for (const auto& token : token_) if (!token.empty()) runtime_->rooms.erase(token);
      std::cerr << "match creation failed for " << match_id_ << ": " << error << "\n";
      return;
    }
  }
  broadcast(MessageType::MatchFound, "match_id=" + match_id_ +
      ";room=1;turn=" + std::to_string(battle_.state().turn) + ";turn_id=" + std::to_string(battle_.state().turn_id));
  snap();
}
void Room::play(const std::shared_ptr<Session>& session, const std::string& match,
                unsigned long long turn, int card, unsigned long long action_id,
                const std::string& request_id) {
  apply_action(session, match, {battle::CommandKind::PlayCard, -1, turn, action_id, card}, request_id);
}

void Room::end_turn(const std::shared_ptr<Session>& session, const std::string& match,
                    unsigned long long turn, unsigned long long action_id,
                    const std::string& request_id) {
  apply_action(session, match, {battle::CommandKind::EndTurn, -1, turn, action_id, 0}, request_id);
}

void Room::apply_action(const std::shared_ptr<Session>& session, const std::string& match,
                       battle::BattleCommand command, const std::string& request_id) {
  if (done_) return;
  for (int player = 0; player < 2; ++player) {
    if (p_[player].lock() == session) command.player = player;
  }
  if (command.player < 0) return;
  const auto reject = [&](const std::string& error) {
    session->send(MessageType::Error, error + ";match_id=" + match_id_ +
        ";action_id=" + std::to_string(command.action_id), replay_.events().size(), request_id);
  };
  if (recovery_write_pending_) { reject("code=recovery_pending"); return; }
  if (match != match_id_) { reject("code=match_mismatch"); return; }
  const std::string signature = command.kind == battle::CommandKind::PlayCard ?
      "play:" + std::to_string(command.turn_id) + ":" + std::to_string(command.card) :
      "end:" + std::to_string(command.turn_id);
  if (replay_action(command.player, command.action_id, signature, session, request_id)) return;
  const auto previous_turn = battle_.state().turn_id;
  auto outcome = battle_.apply(command);
  if (!outcome.error.empty()) {
    const auto error = "code=" + outcome.error +
        (outcome.error == "stale_turn" ? ";turn_id=" + std::to_string(battle_.state().turn_id) : "");
    reject(error);
    return;
  }
  const std::string ack = "match_id=" + match_id_ + ";turn_id=" +
      std::to_string(command.turn_id) + ";action_id=" + std::to_string(command.action_id) +
      ";status=applied;revision=" + std::to_string(replay_.events().size() + 1) +
      ";request_id=" + request_id;
  remember_action(command.player, command.action_id, signature, ack);
  deferred_messages_.push_back({session, MessageType::ActionAck, ack,
                               static_cast<std::uint64_t>(replay_.events().size() + 1), ""});
  if (battle_.state().turn_id != previous_turn && battle_.state().turn_id <= 40)
    reset_turn_clock();
  for (const auto& event : outcome.events) {
    const auto payload = event.payload(match_id_);
    record_battle_event(payload);
    defer_broadcast(MessageType::BattleEvent, payload);
  }
  if (outcome.terminal) {
    std::string result = "match_id=" + match_id_ + ";winner=" + std::to_string(outcome.winner);
    if (!outcome.reason.empty()) result += ";reason=" + outcome.reason;
    result += ";turn_id=" + std::to_string(battle_.state().turn_id);
    prepare_result(result);
  }
  if (persist_checkpoint()) publish_checkpoint();
}

}
