# 有限状态效果与回合开始触发

中毒和再生采用固定状态槽位，触发由服务端权威规则执行。

## 范围与规则

| 状态 | 卡牌作用目标 | 触发 | 行为 |
| --- | --- | --- | --- |
| `poison` | 对手 | 持有者回合开始 | 直接扣 HP，绕过护盾，HP 最低为 0 |
| `regen` | 自己 | 持有者回合开始 | 恢复 HP，最高为 30；满血也消耗一次触发 |

- 每个玩家只有这两个固定槽位，无叠层、随机目标或递归触发。
- 强度 `value` 为 1–30，持续 `duration` 为 1–5 次持有者回合开始。
- 同类重施直接替换强度和剩余次数，最后一次合法施加生效；不会把两次强度相加，也不延长到原次数之外。
- 固定顺序是中毒、再生。中毒致死后立即结束，不触发再生、不消耗未触发的再生次数。
- 每次触发后剩余次数减一，到零时强度和次数都清零。只有真正进入持有者的新回合才触发；快照、重连和成功动作重试不触发。
- 继续保留现有“一次成功出牌或 EndTurn 推进一个回合”的规则，不改成多动作回合。

回合推进顺序：记录成功动作 → 切换玩家、递增 `turn_id` → 检查最大回合 → 重置当前玩家能量为 3 → 中毒/再生 → 致死判定或广播快照。直接伤害致死仍在推进回合前结束。

最大回合仍是 40；推进到 41 时先按 HP 判定 `max_turns`，不重置能量、不触发任何回合 41 状态。状态致死的结果使用 `reason=poison`，其 `turn_id` 是被中毒玩家刚进入的回合。

## 配置与确定性牌库

旧五列头 `id,name,cost,effect,value` 继续接受，普通效果的 duration 默认为 0。新六列头为：

```csv
id,name,cost,effect,value,duration
1,Strike,2,damage,8,0
2,Mend,1,heal,4,0
3,Barrier,1,shield,6,0
7,Venom,1,poison,3,2
8,Renew,1,regen,3,2
```

普通 `damage/heal/shield/draw` 必须为 `duration=0`；状态效果必须显式填写合法 duration。无效配置在监听 TCP 前拒绝启动。卡牌 ID 1、2、3 仍是必需的初始手牌，旧配置和旧 Bot 操作不变。

初始牌库按所有已配置 ID 升序排列，重复三轮；status_v1 的示例为 `[1,2,3,7,8] * 3`。当前默认卡表有九张牌，规则版本为 bonus_v1。普通出牌补牌遇到空牌库时重建并使用 XorShift64 洗牌，RNG 状态由快照/恢复保存；draw 效果只抽剩余牌，不自行循环牌库。历史回放按各自规则版本重建，不能用新卡表替换旧配置。

## TextV1 与事件顺序

不新增消息 ID、不改变长度帧或动作请求。快照新增公开字段，两个玩家各一组：

- `p0_poison_value/p0_poison_turns`、`p0_regen_value/p0_regen_turns`。
- `p1_poison_value/p1_poison_turns`、`p1_regen_value/p1_regen_turns`。

私人手牌规则不变。Python/Pygame 状态模型和 Bot 识别新卡，缺少这些字段的旧快照按全零状态处理；客户端仍只依据权威快照更新战斗状态。Unity 和 Python 均按服务端快照展示状态，不自行计算触发结果。

成功状态卡的 `BattleEvent` 在原动作字段之外带 `duration`：

```text
match_id=match-1;turn_id=1;player=0;action_id=1;card=7;type=poison;value=3;duration=2
```

触发使用同一 `BattleEvent`，保留 `action_id=0` 给系统事件；`player` 表示持有者，不是施加者，`turn_id` 是新回合：

```text
match_id=match-1;turn_id=2;player=1;action_id=0;type=status_tick;status=poison;value=3;remaining=1;hp=27
```

`value` 是本次触发强度，`remaining` 是触发后的次数，`hp` 是该次触发后的 HP。每个触发都单独增加回放 revision；同回合的中毒和再生可产生两个系统事件，但不占用玩家 action 序号。合法动作的 ActionAck、128 条成功回执缓存以及 settlement pending 时的回执重放保持原行为。

## 新旧离线回放

文件格式继续是 `ARENA_REPLAY_V1`，revision 和 FNV-1a 摘要算法不变。新 `match_start` 载荷包含：

```text
match_id=match-1;rules=status_v1;deck=1,2,3,7,8,1,2,3,7,8,1,2,3,7,8
```

- 没有 rules/deck 的旧开局按 legacy 规则重建：初始手牌 `[1,2,3]`、牌库 `[1,2,3]*3`，没有状态触发，即使当前 CSV 额外定义了新卡也不改旧牌库。
- 新开局验证规则版本和完整确定性牌库；未知版本或与 CSV 不匹配的牌库会拒绝。
- 重建工具逐项验证状态卡强度/次数、系统 action_id、持有者、触发顺序、剩余次数和 HP。缺失、重复、乱序或伪造触发都会失败。
- 最大回合按 40 个玩家动作检查，不再假设总事件只能是 42 个；系统触发也必须纳入连续 revision 和摘要。
- JSON 输出包含双方 `statuses`，同时保留 HP、能量、护盾、手牌、剩余牌库和结果。

```powershell
python tools\replay\replay_battle.py .\match.replay --cards .\server\config\cards.csv
```

必须使用对局对应的卡牌配置；回放没有内嵌整份卡牌定义。FNV-1a 只是确定性一致性诊断，不是安全签名。终局文件及离线重建仍不能恢复进程崩溃时正在进行的实时房间。

## 验证入口

status_effects_test.cpp 验证槽位、替换、到期、顺序和致死；
status_effects_integration_test.py 验证 TCP/私有状态/幂等/重连/终局，
replay_battle_test.py 拒绝缺失、重复、乱序或伪造系统触发。
配置启动测试覆盖五列兼容与 duration 值域。

规则也通过双协议、Bot、Unity 和真实存储交付路径验证。当前版本回归及 sanitizer 边界见
[支持范围](project-status.md)、[sanitizers.md](sanitizers.md) 与 [证据索引](evidence-index.md)。
