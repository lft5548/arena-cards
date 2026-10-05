# 回合结束触发设计

## 固定规则

`burn`（灼烧）作用于对手，在持有者成功出牌或 EndTurn 后扣 HP。
强度为 1–30，持续 1–5 次持有者回合结束；同类重施直接替换，不叠层。
默认 Ember（ID 10）费用 1，强度 3，持续 2 次。CSV 的 `duration` 表示触发次数。

灼烧绕过护盾，HP 最低为 0。每次触发扣一次剩余次数，到零清除强度。
施加灼烧不会在对手回合开始时扣血；中毒/再生仍按原回合开始规则执行。
错误动作、成功动作重试、快照、重连、断线与超时结束都不触发灼烧。

## 顺序与终局

1. 校验动作，保存 ACK，扣费/移除卡牌/补牌，执行直接效果，记录玩家事件。
2. 直接伤害致胜则立即结算，不再触发出牌者的灼烧。
3. 执行出牌者的灼烧，记录系统事件；灼烧致死时对手获胜，保持当前 turn_id。
4. 切换玩家、递增 turn_id。超过 40 时按 HP 结算，不执行第 41 回合开始触发。
5. 重置新玩家能量，依次处理中毒和再生，致死即停，再发送快照。

因此第 40 个合法动作的回合结束灼烧先于最大回合判定；它可能在第 40 回合致死。
网络层不直接执行触发，所有上述步骤仍在 Room actor 内串行完成。

## 事件、状态与回放

TextV1 新增 `pN_burn_value/pN_burn_turns`；ProtoV1 的 PlayerState 增加 `burn` 槽位。
灼烧卡事件包含 `duration`，回合结束系统事件沿用 BattleEvent：

```text
type=status_tick;phase=end;status=burn;player=1;turn_id=2;action_id=0;value=3;remaining=1;hp=27
```

`player/turn_id` 属于刚结束的回合，`remaining/hp` 是触发后的值。
`action_id=0` 不占玩家序号，但系统事件独立增加 replay revision。
原中毒/再生事件不强加 phase 字段，旧事件载荷和摘要语义不变。

含灼烧卡表使用 `turn_end_v1`；含新增增益时使用其后继 `bonus_v1`。
重建器严格校验结束触发与下回合开始触发的完整身份、顺序、强度、次数和 HP，
拒绝缺失、重复、错误 phase 或伪造致死结果。旧版本须配原 CSV，保持原规则。
FNV 摘要只诊断一致性；终局离线重建不是实时房间崩溃恢复。

核心入口为 `status_effects.h::on_turn_end`、`BattleEngine::advance_turn` 和回放重建器；Room 只广播有序输出事件。
对应验证入口为 `status_effects_test.cpp`、`turn_end_integration_test.py`、`replay_battle_test.py`。
