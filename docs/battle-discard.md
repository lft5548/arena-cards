# 有限确定性弃牌

弃牌按服务端手牌顺序执行，私有快照与离线回放保存同一历史。

## 规则不变量

- 服务端严格 C++17；所有修改仍由 Room actor 串行执行。
- 对手手牌按现有顺序丢弃前 1–3 张；不足时丢实际数量，空手牌允许成功并产生 count=0。
- 原玩家/回合/费用/归属校验不变；失败动作不改状态，相同成功动作重试不重复弃牌。
- 私有快照、断线重连与终局离线重建必须保留相同的手牌、弃牌历史与数量；公开事件不得携带被弃卡牌 ID。
- 保留 TextV1、原 Bot、legacy/status_v1 回放和已有中毒/再生规则。

## 有限规则与取舍

CSV 新增 effect=discard，value 为 1–3，duration 必须为 0。旧五列卡表默认 duration=0，仍可配置弃牌。默认新增 ID 9：Disrupt，费用 2，丢弃对手前两张手牌。discard_v1 示例的六张卡初始牌库是 [1,2,3,7,8,9] 重复三轮；当前默认九张卡按 bonus_v1 使用，初始手牌仍为 [1,2,3]。

成功出牌保持原顺序：扣费 → 从自己的手牌移除该卡 → 从确定性牌库补一张 → 应用效果 → 广播事件 → 推进回合。弃牌只影响对手手牌，不抽取对手牌库，也不增加自动回合抽牌；空手牌玩家仍可 EndTurn。这样不会顺手改掉原有的一动作一回合规则。

每个玩家的 discard 只记录被强制丢弃的卡，按顺序追加；正常使用的卡不计入这份历史，丢弃卡不回收到循环牌库。每局最多 40 个玩家动作、每次最多三张，因此该新增历史受动作上限约束。历史既用于重连/调试，也用于离线状态对照，不是通用墓地/回收玩法。

中毒/再生继续按持有者回合开始触发，顺序和致死/最大回合边界不变。当前灼烧的回合结束顺序见 [battle-turn-end.md](battle-turn-end.md)。

## TextV1 状态与公开事件

不新增消息 ID，动作请求保持不变。成功弃牌沿用 BattleEvent 的 match_id、turn_id、player、action_id、card、type 和 value，另带 target 与实际 count：


默认卡牌示例载荷：

    match_id=match-1;turn_id=13;player=0;action_id=7;card=9;type=discard;value=2;target=1;count=2

其中 card 是打出的 Disrupt，而不是被丢弃的卡；value 是配置数量，count 是实际数量。无 discard 或其他被弃卡牌 ID 字段。系统状态触发仍使用 action_id=0，弃牌本身仍是一个成功玩家动作。

BattleSnapshot 增加：

| 字段 | 可见性 | 含义 |
| --- | --- | --- |
| discard | 私有 | 当前连接玩家的强制弃牌 ID 历史，逗号分隔，空时为空串 |
| discard_count | 数量 | 当前玩家历史数量；不再是固定 0 |
| opponent_discard_count | 数量 | 对手历史数量，不暴露对手 ID |

原 hand、opponent_hand_count 和 deck_count 语义不变。Python 状态模型对缺少附加字段的旧快照使用空历史/零数量；BattleEvent 不在客户端直接模拟最终状态，以服务端快照为准。原 Bot 入口不变，加入 Disrupt 的选牌优先级。Unity 同样以私有快照和数量字段展示，不猜测对手卡牌。

## 新旧离线回放

文件仍为 ARENA_REPLAY_V1，revision 和摘要算法不变；规则版本属于 match_start 载荷：

- legacy：无 rules/deck 的历史开局，沿用旧牌库与无状态触发规则。
- status_v1：不含弃牌效果的卡表仍产生该版本，保留有限中毒/再生重建。
- discard_v1：含弃牌效果的卡表产生该版本，包含此前状态触发及确定性弃牌。

重建器验证完整初始牌库与对局 CSV 一致、弃牌卡配置、对手 target、实际 count、动作身份和事件字段。伪造数量、错误目标、额外私有 ID、旧规则下的弃牌都会拒绝；JSON 增加双方的 discard 历史。所有事件仍计入连续 revision 与 FNV-1a 摘要。

历史 status_v1 回放必须配历史 CSV，不能用新增 ID 9 的默认卡表假装配置兼容；legacy 的旧初始牌库不因当前额外卡牌而改变。CSV 未完整内嵌回放，FNV-1a 不是安全签名，终局离线重建不是实时房间崩溃恢复。

## 验证入口

discard_effects_test.cpp 验证正常/不足/空手牌与顺序；
discard_integration_test.py 覆盖费用、归属、回合、私有状态、幂等/签名冲突、
重连、状态共存与最大回合。replay_battle_test.py 验证配置和事件一致，
拒绝伪造数量、错误 target、额外私有 ID 与不支持规则。

集成验证同时核对终局文件、原 ACK 和离线完整重建；真实结算使用 mysql_settlement_test.py。
当前交付结果与公开证据见 [支持范围](project-status.md) 和 [证据索引](evidence-index.md)。
