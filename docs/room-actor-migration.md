# Room actor 执行模型

同一房间的战斗状态只由一个串行上下文修改。Session 提交操作意图，Matchmaker 配对，Room actor 编排回合、断线、重连、恢复和结算，Battle 执行权威规则。

## 当前模型

```text
Session reader
    -> application routing
    -> Room::post(RoomCommand)
    -> FIFO command queue
    -> one room actor
    -> Battle state transition
    -> send queue / persistence task
    -> completion command back to actor
```

`server/room/room_command.h` 定义命令，`room.cpp` 处理 FIFO 和状态机。每个 Room 有专属线程，命令队列使用独立 mutex/condition_variable。Tick 的绝对期限等待也在 actor 内，房间状态不由网络线程或独立计时线程修改。

Room 没有战斗状态 mutex；队列和共享服务仍保留必要同步。actor 的串行边界是单房间，多个房间可并行执行，但存储 API 仍串行保护，不保证跨房间 SQL 并行。

## 状态与线程所有权

| 执行上下文 | 允许操作 |
| --- | --- |
| Session reader | 收帧、协议适配、应用路由、提交命令 |
| Session writer | 消费有界帧队列、执行 socket 写入 |
| Room actor | Battle/身份槽位/期限/ACK/终局状态修改 |
| 结算 worker | 用复制参数调用 MySQL，回投 SettlementComplete |
| 恢复提交路径 | 保存不可变候选、批量 COMMIT，发布完成状态 |
| 回放 writer | 保存终局副本，回投 ReplayPersistComplete |
| Outbox worker | Redis Lua、applied/attempts 与缓存故障指标 |

对已成功动作的重复请求，actor 返回缓存原 ACK；错误动作不修改规则状态。网络发送只入队，超限关闭 Session。外部 worker 不通过引用直接修改 BattleState。

## 生命周期与重连

1. Matchmaker 创建 Room、Session 槽位和 resume token。
2. Start 命令在 actor 内初始化/公布对局；开启恢复时先成功持久化。
3. 动作、计时和连接变化进入同一队列；恢复失败冻结新命令并重试原候选。
4. 结算成功命令交付 MatchResult，回放和缓存完成按各自语义处理。
5. 计划关服冻结新业务，关闭并排空传输，再提交 FIFO Stop，排空任务并 join。

重连调用通过 promise/future 等待 actor 验证结果，再排入私有 Snapshot，保证身份更新先于快照。合法 token 可接管已关闭旧 Session；旧 Disconnect 只匹配原对象，不能清空新连接。

提交回复丢失期间的 Disconnect 暂存原时间与绝对期限，原候选确认后再保存，避免精确 blob 重试被异步状态变化破坏。

任务由 ShutdownTracker 持有 joinable 句柄，完成句柄在后续任务创建时回收，退出统一 join。预算耗尽返回失败，不能视为正常排空或泄漏扫描完成。

## 验证与限制

并发同回合请求只应用合法动作，重复成功 action_id 不产生第二次事件。断线/重连测试核对新连接接管与旧断线隔离；慢客户端测试核对有界发送队列与正常房间完成；恢复和提交不确定专项核对 ACK、owner 与期限。

内存与未定义行为使用 [ASan/UBSan](sanitizers.md) 检查，事务/身份/故障正确性由隔离集成测试检查。sanitizer 不检测数据竞争。源码和对应入口见 [证据索引](evidence-index.md)。
