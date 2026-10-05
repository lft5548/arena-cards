# Battle 内核边界

## 职责与接口

`arena_battle` 是 C++17 纯规则库，不依赖 Session、Room、socket、MySQL、Redis、时钟或线程。
它读取不可变 CardCatalog，保存真实 XorShift64 RNG 状态；牌库补充时按当前状态洗牌。
Room 在对局创建后设置 seed，快照/在线恢复保存实际 RNG，离线重建使用相同序列。

| 类型/文件 | 职责 |
| --- | --- |
| BattleState / battle_state.h | 双方玩家状态、当前回合、turn_id、成功动作高水位、规则终局标记 |
| BattleCommand / battle_command.h | PlayCard 或 EndTurn 的纯值意图：玩家槽位、回合、动作序号、卡牌 ID |
| BattleEngine / battle_engine.* | 校验、扣费/补牌、效果编排、固定触发顺序和规则胜负 |
| CardEffect / card_effect.* | 直接效果、固定状态、弃牌及攻击/治疗增强的规则实现 |
| BattleEvent / battle_event.* | 有序动作/系统事件及保持旧载荷顺序的序列化 |
| BattleOutcome | 错误，或事件列表、terminal、winner、reason |

`BattleEngine::state()` 只返回 const 引用。外部不能直接改 HP 或绕过规则提交变更。
`apply(command)` 对正常业务拒绝返回错误和空事件，不修改任何规则状态；成功命令只应用一次。
引擎不保存网络 ACK：相同 action_id 的成功重试必须由 Room 的回执缓存先处理。

## Room 与 Battle 分工

Room 验证连接属于哪个槽位及 match_id，先查询最近 128 条成功回执和 settlement_pending。
引擎随后按原次序校验回合、玩家、action_id、卡牌定义、手牌归属和费用。
错误优先级和 stale_turn 响应保持原行为。

成功时引擎完成候选规则变更并返回事件批次，Room 保存 ACK 与有序事件。
开启在线恢复时先成功提交候选，再公布 ACK、事件和快照或启动终局存储；
客户端先看到 ACK 再看到事件。同一 actor 内没有命令插入规则变更与回执保存之间。

Room 保留快照可见性、回放 revision/seed/digest、真实回合计时、断线/重连、超时判负、
异步结算和文件落盘。超时/断线不是出牌，不调用 apply，也不会触发灼烧或消耗增强次数。
BattleState.finished 表示规则判定终局，Room.done 表示结算/生命周期终结：MySQL 提交前可以保留 pending，
但不能再接收新动作。已成功的动作仍能从 Room 返回原 ACK。

## 不变的规则与事件

合法出牌按扣费、移除手牌、确定性补牌、效果执行的顺序处理；合法出牌或 EndTurn 推进一回合。
直接致胜优先于出牌者灼烧；灼烧在旧回合结束触发，致死不切回合；否则切换玩家、
先检查最大回合，再重置能量和执行中毒/再生。第 40 回合结束触发先于最大回合判定，第 41 回合不触发状态。

事件类型和字段保持原协议：系统事件 action_id=0，回合结束带 phase=end；
伤害/治疗 bonus 的缺失与明确为 0 仍有区别。BattleEvent::payload 保持原字段顺序，
否则即使最终状态相同也会改变 FNV 摘要。文件仍是 ARENA_REPLAY_V1，规则版本和对应 CSV 的要求不变。
终局事件文件是离线重建证据，不是实时房间崩溃恢复。

## 验证入口

`battle_engine_test.cpp` 直接调用纯内核，覆盖错误不修改完整状态、确定性初始化/补牌、
手牌/能量边界、护盾/治疗/弃牌/增益、开始与结束触发顺序、致死和最大回合，校验精确事件载荷。
现有 TCP、混合协议、幂等/重连、Bot、C# 和回放测试继续验证 Room 适配层。
