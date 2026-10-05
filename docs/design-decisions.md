# 设计取舍

本文记录当前实现的选择、保证及适用范围。数据结论绑定具体版本、配置与负载；可核对样本入口见 [evidence-index.md](evidence-index.md)。

## 权威规则与客户端职责

客户端只提交 `PlayCard`、`EndTurn` 等操作意图。Room 校验连接与对局归属，BattleEngine 校验玩家、回合、动作高水位、手牌和费用，再产生权威状态与有序事件。Unity 负责界面、动画、计时展示与请求，不计算最终伤害、不决定胜负、不访问数据库。

BattleEngine 不依赖 socket、数据库和真实时钟。Room 处理真实截止时间、重连与存储编排，使规则测试能够直接使用纯值命令。卡表是启动时加载的不可变数据，恢复校验规则指纹，避免使用另一套规则继续旧对局。

源码：[BattleEngine](../server/battle/battle_engine.cpp)、[Room](../server/room/room.cpp)、[Unity 客户端](../client_unity/README.md)。

## FIFO actor 与一房间一线程

同一 Room 的操作、断线、重连、超时维护及异步完成都在 actor 上串行处理，连接线程不写战斗状态。队列 mutex 只保护投递/取出；身份、匹配队列和 token 索引另有局部锁，actor 不等于全系统无锁。

当前每房间一个 actor 线程，每连接一个 reader 和一个 writer。状态所有权与执行顺序明确，但线程栈、调度和同步等待带来规模成本。恢复写入通过 batcher 执行，actor 同步等待 future；终局结算与回放写入另起 worker，完成后回投命令。这些行为应与“整个 actor 完全非阻塞”的实现区分。

实现没有共享房间执行池。是否迁移到执行池，应先测量线程数、调度开销与持久化等待占比，再确保同房间单写入和停止语义保持不变。当前容量只依据报告中的规模，不外推到海量连接。

源码：[room_command.h](../server/room/room_command.h)、[Room::actor_loop](../server/room/room.cpp)、[gateway Session](../server/gateway/session.cpp)、[ShutdownTracker](../server/app/shutdown.h)。

## COMMIT 后 ACK 与未确认动作

启用在线恢复时，成功动作先形成候选状态、原 ACK 与事件，再提交 MySQL；确认 COMMIT 后才将 ACK、事件和私有快照交给 writer。该顺序避免客户端已经收到成功确认、重启却回到此前状态。

提交成功与收到回复之间存在窗口：数据库可能已提交，服务端没有收到成功回复；服务端可能已发 ACK，客户端未收到。因此未确认不等于未执行，客户端需要携带原 `action_id` 与原参数重试。最近 128 个成功回执持久化并原样返回，参数冲突拒绝，旧动作高水位阻止再次执行。

存储返回失败时保留候选、冻结新动作，精确重试必须匹配 owner、已提交序号与完全相同 blob。候选待确认期间的 Disconnect 暂存原始断线时间和绝对期限，候选确认后再写下一序号，不能修改原 blob，也不能延长重连窗口。

源码：[apply_action / replay_action / PendingDisconnect](../server/room/room.cpp)、[MysqlStore::save_room_recovery](../server/persistence/mysql_store.h)。

## 跨房间批量 COMMIT

默认 `ARENA_CHECKPOINT_BATCH_SIZE=16`，合法范围 1–32。batcher 使用 2ms 收集窗口，把不同房间候选放入同一个事务，成功后完成各调用方 future。最大排队 128 请求/16MiB，普通批次最多 32 条/1MiB；大于批次字节上限的合法文档单独提交。

批量减少事务提交次数，代价是收集/排队等待与共享失败边界。单批失败时所有成员均不提前 ACK，保留各自候选重试。batch=1 为单次写入路径，可用于同条件对照。

历史 20 Bot 分段样本中，序列化约 0.039ms/请求，共享锁等待约 142ms/请求；在保留完整文档的条件下，批量提交样本 ACK P99 从约 277ms 降到约 151ms，真实吞吐从 1.424 提升到 4.053 局/s。它支持优先减少提交等待的选择，但属于历史配置样本，不能当作当前所有负载的保证。

指标按口径解释：queue/write 针对逻辑请求，锁/连接/SQL/COMMIT针对物理事务，不能直接相加成单动作耗时。比较中的事务分母包括开局事务及更新批次。

源码：[CheckpointBatcher](../server/persistence/checkpoint_batcher.h)；历史证据：[recovery-load-report.md](recovery-load-report.md)。

## 完整检查点与周期快照＋权威尾部

| 方面 | `full` | `tail` |
| --- | --- | --- |
| 常规写入 | 完整恢复文档 | 权威状态差异、新事件、ACK 与元数据 |
| 启动恢复 | 解码完整文档 | 解码基线，再按连续存储序号应用尾部 |
| ACK 保证 | COMMIT 后确认 | 相同 |
| 保存内容 | 状态、RNG、身份/token、原 ACK、期限、pending result | 相同 |
| 额外边界 | 完整文档校验 | 连续性、快照发布/尾部清理、重建状态上限 |

tail 的存储序号独立于战斗 revision；断线、重连等不产生战斗事件的持久化变化也推进序号。它不是只记录伤害事件，也不是重新执行客户端命令。C++ 恢复器顺序应用权威增量，并验证每一步的完整恢复状态。

默认快照间隔 16 次持久化，范围 1–128。新快照发布与已覆盖尾部删除同事务；加载采用一致性读；缺口、未知格式、checksum 或规则/状态校验失败拒绝启动。写入前确保重建后的完整文档不超过 8MiB，不能确认一个随后无法加载的状态。停服切换模式先读取已有格式，再原子转换。

两种实现以相同 Linux C++17 Release 二进制、pool=2、batch=16、间隔16、固定规则和 seed，比较 20/100/200 Bot 两类负载各 3 次，并各做 3 次 100 房间恢复。36 场运行与 6 场恢复，共 2,520 局，轨迹配对及存储/回放核对均完成。本固定种子下两类负载都到 40 回合，普通选牌不能描述为短局。

预定采用门槛：100/200 Bot 的两类负载，每组同一指标的三对样本均改善至少 20%；100 房间全部原 token 可重连不超过 5 秒；故障正确性通过。实际 tail 逻辑 payload 减少约 85–86%，但只有 100 Bot 普通选牌组的 P99 连续满足性能门槛，其他主要组存在波动与退化。

**最终默认为 `full`，`tail` 可显式配置。** 当前比较不支持全局更换默认。保留 tail 的入口、校验和证据，便于复现这项取舍；这不是继续调参的要求。逻辑 payload 不是 redo/binlog/索引或物理磁盘字节，减少 payload 不能直接推断延迟收益。

源码：[RecoveryCodec](../server/room/recovery_checkpoint.h)、[RecoveryTailCodec](../server/room/recovery_tail.h)、[MySQL 写入与加载](../server/persistence/mysql_store.h)；证据：[策略对照报告](recovery-strategy-report.md)、[公开原始指标](recovery-strategy-report.json)。

## MySQL 权威、Outbox 与 Redis 独立补偿

MySQL 在一个事务内完成唯一 `match_results`、积分/胜负、对局状态和 pending Outbox；`match_id` 幂等检查处理重复结算与成功 COMMIT 回复丢失。Redis 的 Lua 用同一 `match_id` 去重更新缓存，Outbox 成功消费后才标记 applied。

Outbox worker 是缓存更新的执行者；结算 worker 只执行 MySQL 并投递完成命令。Redis 连接、DNS 或 I/O 黑洞不会把缓存恢复变成 MatchResult 的前置条件。缓存失败保留 pending、记录错误与重试次数；恢复后继续消费。成功 Lua 回复丢失或 applied 标记失败，会重试同一 `match_id`，积分只更新一次。

Redis 连接和命令 I/O 共用各自的 steady_clock 绝对预算，默认各 1000ms；部分读写进展不延长预算。连接失效清空接收缓冲并下轮重连。系统 DNS 本身可能不可取消，调用方有界返回，最多一个在途解析任务独立持有数据。

当前客户端排行榜通过 C++ 只读查询 MySQL，输出 `source=mysql`；它不读取可能落后的 Redis 榜单。Redis 全量重建以 MySQL 为源；重建应在停服/无消费写入的受控窗口执行，以免重建快照与并发增量错位。

源码：[MysqlStore::settle](../server/persistence/mysql_store.h)、[consume_pending_outbox](../server/main.cpp)、[RedisLeaderboard](../server/ranking/redis_leaderboard.h)、[rebuild_leaderboard.py](../tools/ranking/rebuild_leaderboard.py)。

## 有界连接池与串行存储 API

MySQL 池限制 1–8 会话，事务和 `GET_LOCK` 固定在锁定会话，适用的 Outbox/只读操作轮换其他槽。连接失效丢弃句柄，按 1/2/4/…/30 秒退避重建；重连重新获取实例锁，事务失败由上层整笔幂等重试。

存储 API 仍由 `MysqlStore` 的 mutex 串行保护。池解决有界会话复用、探活与单槽恢复，不能把 pool=2 解释为两笔事务并行，也不能用增加池大小代替瓶颈分析。Redis 网络等待不持该 mutex，避免缓存故障占住权威存储路径。

源码：[mysql_store.h](../server/persistence/mysql_store.h)、[retry_backoff.h](../server/persistence/retry_backoff.h)；证据索引中的 Redis 与真实 MySQL 专项覆盖相关边界。

## 单实例恢复与防旧写

`GET_LOCK` 保证同库正常情况下一个服务实例运行，恢复 owner 防止旧连接失锁后的写入、删除和结算。启动先校验全部恢复数据，再认领 owner、写入启动后的期限/格式并注册 token/启动 actor，最后监听；坏数据不启动部分房间。

崩溃前仍连接的玩家在重启后获得 15 秒重连窗口；已断线玩家保留原期限，回合期限包含停机时长。原 ACK/RNG/身份及 pending result 恢复；终局按唯一结果重试结算并提供结果重取窗口。

实例锁和 owner 约束不提供自动路由、集群协调或无感接管。双实例竞争测试验证拒绝旧写，不证明完整高可用网络分区处理。

## 有预算退出、背压与信任范围

停止信号仅设置标志，退出停止准入、冻结新匹配、排空连接回调、向 Room 投递 Stop、join actor/结算/回放及 Outbox，再释放存储。各阶段共用绝对预算，超限失败退出。恢复开启时保留对局，关闭恢复时只作废本实例未完成对局。

连接数、每连接完整帧速率、writer 队列与心跳有上限。writer 队列超限关闭慢连接，Room 不同步等待网络；read header/body 共用截止时间，慢滴字节不能永久占住连接。TCP Admin 与 FastAPI 面向可信本机操作，项目不将其描述为公网身份体系。

源码：[tcp_server.cpp](../server/gateway/tcp_server.cpp)、[session.cpp](../server/gateway/session.cpp)、[main.cpp](../server/main.cpp)；配置：[network-limits.md](network-limits.md)、[network-lifecycle.md](network-lifecycle.md)。

## 摘要、协议与实测口径

TextV1 保留调试可读性，ProtoV1 使用同一 schema 与同一规则路径，避免客户端或协议复制规则。新连接通过 Hello 选择并锁定协议，未知字段与旧指标省略遵循现有兼容规则。

FNV 摘要检查确定性结果和意外损坏，不是密码学认证。公开样本的 SHA256 用于文件/二进制对应，也不能单独证明某个执行来源。关键证据同时提供负载、配置、指标口径与验证结果。

本机 Bot 成功率与延迟是对应环境的观测值。历史报告使用过玩家轮次吞吐与 Docker 容器资源，新的恢复报告使用真实局/s 与 `/proc` CPU/RSS，两种口径不能直接合并。共享 MySQL 进程采样包含后台活动，强杀测试不完成进程退出泄漏扫描。
