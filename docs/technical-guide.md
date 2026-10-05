# 技术阅读指南

从三条端到端路径阅读 Arena Cards，再核对关键边界。架构图见 [architecture.md](architecture.md)，取舍依据见 [design-decisions.md](design-decisions.md)，报告与公开原始指标见 [evidence-index.md](evidence-index.md)。

## 路径一：一次动作

1. [gateway/frame_codec.cpp](../server/gateway/frame_codec.cpp) 与 [socket.cpp](../server/gateway/socket.cpp) 读取完整帧，处理 TCP 部分读写和长度边界。
2. [gateway/session.cpp](../server/gateway/session.cpp) 计费完整帧、协商/解码协议，合法入站更新心跳，再调用应用 `SessionHandler`。
3. [app/session.cpp](../server/app/session.cpp) 验证房间绑定，解析操作意图，投递 `PlayCard` 或 `EndTurn`。
4. [room/room.cpp](../server/room/room.cpp) 的 actor 从 FIFO 取出命令，验证当前连接、match_id 和原成功回执。
5. [battle_engine.cpp](../server/battle/battle_engine.cpp) 验证规则并更新权威状态，返回按规则顺序产生的事件与终局信息。
6. Room 保存原 ACK、新事件、RNG/期限及必要终局元数据，形成 full/tail 候选。
7. [checkpoint_batcher.h](../server/persistence/checkpoint_batcher.h) 合并其他房间写入；[mysql_store.h](../server/persistence/mysql_store.h) 校验 owner/前序号并 COMMIT。actor 同步等待完成。
8. 成功后 Room 发布延迟消息，gateway writer FIFO 发送 ACK、事件与私有快照。存储失败时冻结候选并精确重试。

第7步的保证要求在线恢复开启。writer 入队与客户端收到消息之间仍有网络窗口，因此客户端重试需要沿用原 action_id。

## 路径二：终局与缓存

1. Room 形成 `pending_result`，先持久化终局恢复状态。
2. `start_settlement` 复制 match_id、玩家、胜者、回合和原因到 worker。
3. `MysqlStore::settle` 在同一事务中写唯一结果、玩家积分/胜负、对局终态及 pending Outbox；已有 match_id 返回幂等结果。
4. worker 向 Room FIFO 投递 `SettlementComplete`；actor 成功后发送 MatchResult。Redis 不在此路径中。
5. [main.cpp](../server/main.cpp) 的 Outbox worker 读取 committed pending，执行 [Redis Lua](../server/ranking/redis_leaderboard.h)。成功才标记 applied；失败保留任务并记录错误。

缓存网络等待不持 MysqlStore mutex。当前排行榜请求在服务端只读查询 MySQL，榜单声明 `source=mysql`。核对脚本需同时检查 MySQL、Redis 和 Outbox，不能仅从客户端界面判断缓存已补齐。

## 路径三：启动与续局

1. [main.cpp](../server/main.cpp) 校验配置，连接 MySQL，获取 `GET_LOCK`，要求 recovery=1、MySQL required=1、cleanup_running=0。
2. 一致性读加载完整 checkpoint 与连续 tail；[RecoveryCodec](../server/room/recovery_checkpoint.h) 解码基线，[RecoveryTailCodec](../server/room/recovery_tail.h) 应用权威增量。
3. 校验格式、序号、checksum、规则指纹、完整状态、玩家和 token；任何坏记录拒绝启动。
4. 构建 Room，恢复 RNG、原 ACK、身份、期限、pending result，认领新 owner，写入启动后候选与必要模式转换。
5. 全部文档有效后注册 token 并启动 actor，最后开放 TCP。客户端新连接协商协议，以原 token 重连。
6. 重连元数据提交后发成功回复与私有快照。已断线玩家期限不重置，回合期限包含停机时长；崩溃前仍连接玩家获得 15 秒重连窗口。

Python 离线回放不参与第2步。保存到回放目录的诊断快照也不是 MySQL 在线恢复检查点。

## 常见技术问题

### 为什么使用 actor，还保留了 mutex？

actor 限制房间状态只有一个写入线程。队列、共享身份、匹配队列、token 索引和数据库会话仍是不同线程访问的共享对象，需要各自的同步。没有把所有共享数据变成无锁结构。

### actor 能一直处理其他命令吗？

当前恢复写入会同步等待 batcher future，同房间后续命令留在队列；不同房间可并发等待。结算与回放采用 worker 回投完成命令。当前不是共享执行池或全异步存储模型。

### 为什么先应用内存规则再持久化？失败是否回滚？

规则先生成一致的候选，Room 暂存成功 ACK 与事件。失败后保持候选、冻结新动作、重试同一 blob，不向客户端确认新状态。进程重启从最后已提交状态恢复；没有回执的动作可能已提交，客户端以原 ID 重试确定结果。

### COMMIT 回复丢失如何区分成功和失败？

重新连接后检查 owner、持久化序号与完全相同的 blob，确认同一候选是否已提交。不能只根据超时当作未执行，也不能在不确定时修改候选后覆盖。结算则检查 match_id 唯一结果，避免再加积分。

### 原 ACK 缓存只有 128 条，是否会重复执行旧动作？

最近 128 个成功回执支持原样返回；规则还保留成功动作高水位。被淘汰的较旧 ID 不能重新执行，但不会无限保存每一个历史 ACK。

### 断线期间为什么不能延长恢复期限？

期限是持久化绝对时间。候选未确认时的 Disconnect 暂存原始时间，确认后单独推进存储序号，等待数据库不额外赠送重连时间。已断线玩家重启后仍受原期限约束。

### 为什么尾部不是直接保存玩家命令？

在线续局还要恢复 ACK、身份、断线/重连、真实截止时间与待结算结果。tail 保存权威状态增量和这些元数据，按存储序号应用，不重新运行客户端命令。战斗事件 revision 与存储 sequence 是两个维度。

### 为什么缩小写入量后仍选择 full？

tail 将逻辑 payload 减少约 85–86%，但在预定100/200 Bot两类负载下，没有全部满足同一指标连续三对改善至少20%的门槛。当前保持full默认，tail可配置。减小payload不必然缩短队列、锁或COMMIT等待，详情见 [策略报告](recovery-strategy-report.md)。

### 快照发布与尾部删除中途崩溃会怎样？

更新快照、调整序号与删除已覆盖尾部在同一事务。提交前崩溃看到旧快照与原尾部，提交后看到新快照与未覆盖尾部；加载还验证连续性，拒绝部分或损坏状态。

### 为什么需要批量 COMMIT？

历史分段证据显示持久化等待明显大于序列化。跨房间合并减少提交次数，同时保持每个动作在共同COMMIT成功后确认。代价是短收集等待及批次失败共同重试，参数与分段统计见 [设计取舍](design-decisions.md)。

### MySQL 和 Redis 为什么没有分布式事务？

MySQL 是权威源，积分与Outbox在同一事务内；缓存采用重试与match_id Lua幂等达到最终一致性。结果交付只等待权威事务。两存储短期可能不同，但pending任务可观察、可补偿。

### Lua 已成功但回复丢失，会不会重复加分？

Lua 在同次执行中检查并记录match_id，再更新分数。回复丢失或SQL applied标记失败会造成重复消费，同一match_id不再次更新。MySQL已有唯一结果也阻止权威积分重复结算。

### DNS 超时能取消系统解析吗？

调用方按连接预算返回，解析任务独立持有数据，最多一个尚未完成的任务；阻塞的系统解析本身不可取消。保证是调用方等待有界，不是声称底层系统调用可强制中止。

### pool=2 是否代表两笔事务并行？

不是。存储API由共享mutex串行保护；池解决有界会话复用、失效恢复和允许的槽轮换，事务与实例锁在锁定会话。吞吐判断需要实际SQL/COMMIT与队列统计。

### 服务重启是否作废 running 对局？

在线恢复开启时加载并恢复Room，保留状态、身份、RNG、ACK、期限和pending result；不会用作废替代续局。关闭恢复的可选清理路径才标记aborted；存在durable checkpoint时拒绝用关闭恢复的启动配置丢弃对局。

### GET_LOCK 和 owner 是多实例自动接管吗？

它们实现同库单实例互斥与旧写防护。实例竞争验证拒绝旧写，不包括自动路由、租约协调或集群网络分区接管。

### 慢客户端和空闲连接如何释放资源？

writer队列有帧数/字节上限，超限关闭连接；完整帧令牌桶限流，语法完整请求才刷新心跳，慢滴字节不延长header/body预算。关闭走统一Disconnect路径，I/O退出后释放socket与连接名额。

### 停止服务时如何决定房间结果？

停止准入与匹配，排空连接回调，再FIFO停止房间与任务。恢复开启时保留对局，不制造断线败者；关闭恢复时本实例未完成对局作废且不计分。所有阶段共享绝对退出预算，超限失败退出。

### FNV 与公开 SHA256 分别证明什么？

FNV检测确定性差异和意外损坏，不具备抗恶意篡改能力。SHA256绑定公开文件或二进制内容；负载、配置、测试过程与口径仍需一起核对，哈希本身不能替代执行证据。

### 如何理解测试数量与容量结论？

Windows CTest、Linux CTest和Python回归存在包含关系，不简单相加。历史P6与策略对照是不同样本集合，吞吐和资源口径也可能不同。200Bot是对应本机环境的100房间实验，不能直接承诺生产连接量或通用SLA。

### 如何复现可视演示？

按 [演示指南](demo-guide.md) 启动Compose并检查配置，使用两名Unity客户端完成正常对局、原token续局及Redis补偿；操作与自动核对分开记录。历史Unity Editor验证与当前复用构建彩排的范围见 [demo-report.md](demo-report.md)。
