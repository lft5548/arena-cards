# 协议约定

TextV1 用 UTF-8 文本便于调试，ProtoV1 用结构化 Protobuf；两者使用同一 TCP 长度帧和不同消息 ID。协商及运行库设计见 [双协议版本约定](protocol-versioning.md) 和 [Protobuf 接入](protobuf.md)。

```text
4-byte unsigned big-endian body_length (message type + payload)
2-byte unsigned big-endian message_type
payload (TextV1 为 UTF-8 key=value，ProtoV1 为 protobuf bytes)
```

消息类型：`1 LoginReq`、`2 LoginResp`、`3 MatchJoinReq`、`4 MatchCancelReq`、`5 MatchFound`、`6 PlayCardReq`、`7 BattleSnapshot`、`8 BattleEvent`、`9 Error`、`10 Heartbeat`、`11 Pong`、`12 ReconnectReq`、`13 ReconnectResp`、`14 MatchResult`、`15 EndTurnReq`、`16 AdminRoomsReq`、`17 AdminRoomsResp`、`18 ActionAck`、`19 ProtocolHelloReq`、`20 ProtocolHelloResp`、`21 LeaderboardReq`、`22 LeaderboardResp`。

`MatchFound` 和 `BattleSnapshot` 返回 `match_id`、`turn_id` 和 `player_index`。快照中的 `hand` 只属于当前连接的玩家，`revision` 用于丢弃旧快照。快照也附带 `replay_seed`、`replay_revision`、`replay_digest` 和 `replay_valid`，用于诊断与事件序列一致性检查。出牌或结束回合必须回传当前 `match_id`、`turn_id` 和递增的 `action_id`。

每个成功操作广播的 `BattleEvent` 包含 `player`、`turn_id` 和 `action_id`；包括达到最大回合数时的最后一次结束回合。终局 `MatchResult` 附带最终 `replay_seed`、`replay_revision`、`replay_digest` 和 `replay_valid`，双方应观察到相同摘要。结算 pending 期间重试已成功的 `action_id` 仍返回缓存的 `ActionAck`，新动作则收到 `settlement_pending`。终局事件可异步保存为 `ARENA_REPLAY_V1` 并由离线工具重建；在线房间重启恢复另使用 MySQL 完整检查点或周期快照＋在线尾部，保持原 token、ACK、RNG 和绝对期限，详见 [在线恢复](room-recovery.md)。

状态快照附加双方的 `pN_poison_value/pN_poison_turns`、`pN_regen_value/pN_regen_turns` 和 `pN_burn_value/pN_burn_turns`。中毒/再生在回合开始触发，灼烧在回合结束触发；系统事件 `action_id=0`，结束阶段带 `phase=end`。增强槽位公开强度/剩余使用次数，直接效果事件按规则版本带 optional bonus。详细顺序和边界见 [有限状态](battle-status-effects.md)、[回合结束](battle-turn-end.md)、[有限增益](battle-bonuses.md)。Bot 可选择文本、二进制或混合模式；默认构建 ProtoV1 runtime=1，显式禁用时为 0。

弃牌快照新增私有 `discard`（自己的强制弃牌 ID 历史）、`discard_count` 与只含数量的 `opponent_discard_count`；不发送 `opponent_discard` 或对手手牌 ID。成功 `type=discard` 事件带配置 `value`、对手 `target` 与实际 `count`，不带被弃卡牌 ID；空手牌成功时 count=0，成功动作重试不重复弃牌。含弃牌配置的新回放开局为 `rules=discard_v1`，旧规则继续保留。详见 [确定性弃牌](battle-discard.md)。

`AdminRoomsReq` 不要求登录，用于本地 FastAPI 管理接口查询运行指标；服务端返回 `AdminRoomsResp active_rooms=N;active_sessions=M`，并附带房间命令、发送队列、结算和回放存储计数。回放计数为 `replays_saved` 与 `replay_save_failures`。旧客户端只读取前两个字段即可。该接口只暴露汇总计数，不返回玩家昵称、手牌或战斗状态。

本地 FastAPI 调试接口提供 `GET /health`、`GET /metrics`、`GET /rooms` 和受限的 `POST /command`。`/command` 只允许 `AdminRoomsReq` 与 `Heartbeat`，管理 API 默认应绑定 `127.0.0.1`。`active_sessions` 统计包含当前管理接口探测连接，因此单次 `/metrics` 请求可能使数值短暂增加 1。

`LoginResp` 返回一次性会话 token。断线后 15 秒内可在新 TCP 连接上发送 `ReconnectReq`，payload 为 token 原文；服务端返回 `ReconnectResp ok=1` 后发送该玩家的完整私有快照。

成功的出牌和结束回合返回 `ActionAck`。重放同一 `action_id` 和相同操作签名会返回相同确认，不重复修改状态；在当前房间保留最近 128 条确认，重复使用 ID 提交不同操作会返回 `action_id_conflict`。

MySQL 持久化是服务端环境配置，不改变客户端线协议。启用时结算结果只有在 MySQL 事务提交成功后才发送 `MatchResult`；暂时失败会冻结房间动作并重试。

`play_card` 示例：

```text
frame_length = 2 + len("match_id=match-1;turn_id=1;card=1;action_id=19")
message_type = 6 (PlayCardReq)
payload = match_id=match-1;turn_id=1;card=1;action_id=19
```

服务端必须校验玩家身份、当前回合、动作序号、卡牌归属、费用和目标。重复 `action_id` 返回同一结果或幂等成功，不得重复扣费。
