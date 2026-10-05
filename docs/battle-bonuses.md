# 攻击与治疗增强设计

## 固定规则与取舍

每个玩家只有 `attack_boost` 和 `heal_boost` 两个公开槽位，包含 `value/uses`。
强度为 1–10、可用 1–5 次；施加对象是自己，同类重施替换强度和次数，不叠层。
默认 Focus（ID 11）和 Bless（ID 12）费用均为 1，强度 2、可用 2 次。
沿用六列 CSV，增强卡的 `duration` 表示可用次数，普通持续状态仍表示触发次数。

- 合法直接伤害卡：消耗一个攻击增强次数，将基础伤害加 bonus 后再由护盾抵挡。
- 合法直接治疗卡：消耗一个治疗增强次数，将基础治疗加 bonus 后限制 HP 上限为 30。
- 完全被护盾抵挡、基础值为零或满血治疗，合法动作仍消耗次数。
- 最后一次消耗后，强度与次数都清零；攻击和治疗槽位相互独立。
- EndTurn、抽牌、护盾、中毒、再生、灼烧不消耗，也不获得这两个增强。
- 失败动作、成功动作重试、快照与重连不改变剩余次数；次数不会随回合自动减少。

这里用有限使用次数而不是回合倒计时，便于解释“下一次直接效果”的行为。
不引入乘法、概率、通用技能脚本、状态链或循环触发。

## 权威执行与事件

Room actor 在所有身份/回合/手牌/费用校验成功后，才修改增强槽位。
施加增强沿用成功卡牌事件，带配置强度和 `uses`：

```text
card=11;type=attack_boost;value=2;uses=2
```

含增强卡表使用 `bonus_v1`。该版本的每个直接伤害/治疗事件都带 `bonus`，包括 0：

```text
card=1;type=damage;value=8;bonus=2
```

`value` 始终是配置的基础值，`bonus` 是实际消耗的增强值。
消耗是原动作的一部分，不新增虚构玩家动作或额外系统事件。
增强后直接伤害致胜仍先于出牌者的回合结束灼烧。

## 双协议与重建

TextV1 快照公开双方 `pN_attack_boost_value/pN_attack_boost_uses`、
`pN_heal_boost_value/pN_heal_boost_uses`；私有手牌/弃牌仍只发给所属玩家。
ProtoV1 PlayerState 使用两个 BonusState，BattleEvent 增加 `uses` 和 optional `bonus`。
optional 区分旧版本缺字段与新版本明确为 0，客户端按服务端快照更新，不自行算伤害。

重连快照保留次数与 last_action_id；相同成功动作返回原 ACK，不重复消耗。
重建器从卡表和动作序列推导增强值、验证每次消耗，拒绝错误/缺失 bonus 和错误 uses。
`bonus_v1` 继续支持既有状态、弃牌及回合结束触发；旧规则 JSON 不增加 boosts，
旧事件不强加 bonus，新卡表不能替代历史 CSV。文件格式仍为 ARENA_REPLAY_V1。

次数内核入口是 `bonus_effects.h`，效果由 `card_effect.cpp` 实现，`BattleEngine::apply` 编排；对应测试为
`bonus_effects_test.cpp`、`bonus_integration_test.py`、`replay_battle_test.py` 和双协议 codec 测试。
