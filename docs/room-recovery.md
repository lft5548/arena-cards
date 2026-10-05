# 在线房间重启续局

## 范围

`ARENA_ROOM_RECOVERY=1` 启用同一 MySQL、相同卡表/规则版本的单实例进程重启续局。要求 `ARENA_MYSQL_REQUIRED=1` 和 `ARENA_MYSQL_CLEANUP_RUNNING=0`。默认关闭，不影响历史 TextV1、ProtoV1、原 Bot 与离线回放格式。

这与 `ARENA_SNAPSHOT_V1` 离线快照是独立机制。当前不提供多实例自动故障转移、跨机房容灾、停机期间继续运行或热升级不同战斗规则。

## 权威与确认

MySQL `room_checkpoints` 使用独立 `ARENA_ROOM_RECOVERY_V1` 二进制格式，保存完整 BattleState、RNG、回放、双方身份/重连凭证、最近 128 个成功动作的原 ACK、绝对截止时间和待结算结果。开局 `running` 记录与序号 0 的初始检查点在同一事务创建。两种持久化模式都在 COMMIT 成功后才发送 ACK、事件和快照。

`ARENA_ROOM_RECOVERY_MODE=full` 每次成功命令保存完整检查点；`tail` 保存周期快照与在线增量尾部。未收到确认的命令可能已写入；客户端必须使用原 action_id 重试。SQL 失败或提交结果不明时，房间保留候选状态、停止后续规则操作，后台幂等重试，成功后才公布；不能回滚一个可能已提交的动作。

提交不确定期间到达的 Disconnect 保留断线发生时的绝对期限，等待原候选精确重试成功，再以新存储序号写入；它不能修改正在重试的原 blob。等待数据库不会延长断线宽限。

尾部表 `room_recovery_tail` 使用 `(match_id, sequence)` 主键和 `ARENA_ROOM_TAIL_V1` 格式。每条记录保存变化的权威规则状态、牌组、RNG、身份、期限和终局字段，并追加新战斗事件和 ACK；ACK 淘汰以保留旧后缀、追加新回执表示。它不重新执行客户端意图，也不依赖 Python 离线重建。存储序号包含仅修改元数据的断线、重连和启动宽限写入，与战斗 revision 分离；每次严格推进一位。

`ARENA_ROOM_RECOVERY_SNAPSHOT_INTERVAL` 默认 16、范围 1–128，按成功持久化次数计数。到间隔时，在同一事务内发布完整快照、推进 snapshot/head 序号并删除已覆盖尾部；外部只能读到旧快照与旧尾部，或新快照与剩余尾部。每次写入比较 owner 与上一序号，提交回复丢失后的重试必须匹配已提交序号和原始 blob，序号相同但内容不同会被拒绝。快照与尾部都校验格式、校验和、规则指纹、身份、连续序号和状态边界，缺失或损坏会阻止启动。

启动用数据库中的格式重建全部房间，再按配置持久化启动元数据；因此可在停服后切换模式，无须删除对局。完整模式转尾部时发布完整基线，尾部转完整模式时原子覆盖基线并清尾部。旧完整文档和现有离线回放格式保持兼容；旧的完整写入 API 拒绝覆盖尾部模式。

启动先获取现有数据库 `GET_LOCK`，验证全部检查点和卡表指纹，再设置新的进程 owner。检查点写入、删除、开局重试与结算均验证 owner，防止旧进程在恢复数据库连接后覆写已接管房间。所有使用同一数据库的实例必须配置相同的实例锁名称；此机制不是多实例路由服务。

## 生命周期

启动读取可恢复检查点并重建 Room actor、回放、ACK 去重和 token 路由。原本在线的玩家有 15 秒重连宽限；崩溃前已断线玩家的原截止时间保持不变。回合截止时间包含停机时长，恢复时不会重新发放 30 秒；过期房间按规则结算，排队请求不能绕过超时检查。

终局检查点在结果发出后保留 15 秒供原 token 重新获取结果。若 MySQL 已结算但客户端尚未收到结果，恢复沿用原 pending result 和 match_id 幂等结算，不重复改变积分或 Outbox。终局再次恢复会获得新的 15 秒结果交付窗口。

计划关服先冻结新请求/匹配并关闭、排空传输，再向 Room 投递 FIFO Stop，排空在途检查点、结算和回放任务。
恢复开启时关服关闭连接不写玩家断线计时、不制造赢家，保留原检查点、token/128 ACK/RNG/pending result
与绝对截止时间；已断线玩家的原截止时间不变。默认10秒共享退出预算内完成则返回0，
超预算以退出码4失败退出，MySQL已发出的写入仍可能完成；重启后使用原action_id解决未确认动作的不确定性。
关闭恢复时仅作废本实例未完成对局为 `aborted/server_shutdown`，不计分、不写结果或Outbox。
信号、线程排空顺序、配置和双协议真实存储验收见 [network-lifecycle.md](network-lifecycle.md)。

损坏检查点、卡表/规则不匹配、无检查点的遗留 running 对局会阻止恢复启动，避免静默丢弃。关闭恢复时若数据库仍有检查点也会拒绝启动。首次启用前应结束旧模式的对局；只有确认遗留记录应作废时，才按原流程显式启动清理，不可自动删除用户对局。

## 取舍

完整检查点提供单实例在线续局能力：已确认动作不回退，原身份、ACK、RNG、计时器和终局结算可恢复。full 模式每次动作同步持久化完整文档；`ARENA_CHECKPOINT_BATCH_SIZE` 默认 16、范围 1–32，跨房间更新共用事务，只有同一 COMMIT 成功后才确认批次内的动作，失败则各房间保留候选状态并重试。设置为 1 使用原单请求路径。存储 API 仍串行保护，批量提交减少事务及等待开销，没有降低持久化确认要求。

20 Bot TextV1 单请求分段样本的平均文档为 10,290 bytes，序列化约 0.039 ms/请求，共享锁等待约 142.353 ms/请求，占累计存储调用时间约 89.8%；首要瓶颈是排队与持久化路径。保留相同完整文档的批量 16 样本已将 ACK P99 从 277.361 ms 降至 151.288 ms、实际对局吞吐从 1.424 提升至 4.053 局/s。批量共享锁、SQL 与 COMMIT 时间按物理批次累计，不能直接当作逐请求平均与原路径比较。上述为单次本机样本，完整规模与故障证据以 [recovery-load-report.md](recovery-load-report.md) 为准。

两种模式保留相同的同步提交保证。尾部减少重复写入整局日志，但追加需要更新 head 和插入增量，压缩时需要原子清理，恢复时还要应用连续尾部，不能只凭 blob 更小决定采用。公平比较使用同一 Release 二进制、规则、固定 seed/操作序列、MySQL 配置和 batch=16，20/100/200 Bot 的正常选牌与最大回合负载各重复三次，另测 100 房间重启到全部原 token 可重连的耗时。性能采用门槛为主要规模的同一指标三次都改善至少 20%，且尾部恢复不超过 5 秒、正确性专项通过；门槛不满足则保留默认完整模式。当前版本结果见 [recovery-strategy-report.md](recovery-strategy-report.md)。

完整检查点及单条尾部上限均为 8 MiB；尾部写入前还按完整格式计算候选重建后的总大小，超限停止公布，避免确认一个重启时无法加载的状态。数据库是权威源；Redis 故障仍只影响缓存/Outbox，不回滚结算。关闭恢复的历史结果不能视为开启恢复后的容量证据。

## 启用与验证

```powershell
$env:ARENA_ROOM_RECOVERY = "1"
$env:ARENA_MYSQL_CLEANUP_RUNNING = "0"
docker compose -f deploy/docker-compose.yml up -d --no-deps --wait arena-server
```

保持现有数据库和回放卷。不要执行 `down -v`。

`room_recovery_checkpoint` 与 `room_recovery_config` 纳入普通 CTest。真实数据库故障脚本单独运行：

```powershell
$env:ARENA_MYSQL_USER = "arena"
$env:ARENA_MYSQL_PASSWORD = "arena_dev_password"
$env:ARENA_MYSQL_DATABASE = "arena_cards"
python tests/room_recovery_integration_test.py --docker-server arena-cards-server --port 9000 --mysql-container arena-cards-mysql --protocol text_v1
.\.venv\Scripts\python.exe tests/room_recovery_integration_test.py --docker-server arena-cards-server --port 9000 --mysql-container arena-cards-mysql --protocol proto_v1
.\.venv\Scripts\python.exe tests/room_recovery_fault_test.py --server build-msvc/Release/arena_server.exe --owner-test build-msvc/Release/mysql_room_recovery_owner_test.exe
```

集成脚本强制重启 Arena，验证原 ACK、完整规则/私有状态、RNG、回合/断线截止时间及终局积分幂等，仅清理本次随机记录。权限失败/owner 测试使用独立临时 schema/用户，验证失败期间无 ACK/广播、后续命令被冻结，恢复后只应用一次。

精确窗口脚本 `tests/room_recovery_windows_test.py` 已在 Linux ASan+UBSan 二进制上完成 TextV1/ProtoV1 各三场景：

- 成功保存动作检查点但尚未发送 ACK/事件/快照时暂停，再 SIGKILL；恢复后的状态、RNG、原回执和回合截止时间不变，原 action_id 重试只返回持久化原 ACK，不重新执行动作。
- MySQL 结算 COMMIT 后、结果交付前暂停，再 SIGKILL；原 token 获取原 pending result，match_results/Outbox 各一条，积分和 Redis 不重复增加。
- 两个真实恢复进程竞争相同 GET_LOCK，后者以退出码 3 拒绝启动；暂停旧进程、KILL 其锁会话后，新进程读取并接管原检查点。旧进程恢复并尝试重连时先被实例锁拒绝；新进程退出后，旧进程重新取得数据库锁仍被新的 checkpoint owner 拒绝写入。检查点无 stale 修改，最终恢复能继续结算。

脚本使用独立临时 schema/用户，定向清理自身 Redis 记录，不停止正常服务或删除卷。测试屏障要求显式 `ARENA_ENABLE_TEST_FAULTS=ON`，普通镜像关闭；命令与插桩边界见 [sanitizers.md](sanitizers.md)。上述是单实例重启路径的竞争/防旧写验收，不是生产多实例自动接管、路由或旧进程自动退出机制。

检查点进行中、终局提交前与缓存暂停窗口也分别在 TextV1/ProtoV1 验证：

- `checkpoint-inflight`：真实 InnoDB 行锁阻塞检查点 UPDATE，确认查询已进入执行且没有 ACK/广播，然后 SIGKILL Arena。MySQL 已发出的自提交查询可能继续完成；本次两协议均实际提交。验收接受旧完整检查点或新完整检查点，拒绝部分文档；恢复原状态/ACK/RNG/截止时间，原 action_id 重试最多生效一次。
- `settlement-precommit`：阻塞真实事务中的 match_results INSERT，确认终局检查点已保存，但事务尚未提交、外部仍为初始积分。强杀进程并解除锁后事务回滚；重启从 pending result 幂等结算，双方只增加一次胜负/积分。
- `redis`：在执行 Lua 前暂停缓存消费；SQL 已结算、Outbox pending，Redis 玩家分数/本局幂等键不存在，权威结果已经独立交付。强杀后启动消费 Outbox、原 token 重取同一结果，最终 MySQL/Redis 一致且 applied。

`settlement_before_result` 保留 COMMIT 后但结果未发出的窗口，与仅暂停缓存消费的 `redis` 屏障分别验证。

KILL 数据库连接/同进程重试与 Arena SIGKILL/启动恢复是不同边界。运行命令可添加 `--scenarios checkpoint-inflight settlement-precommit redis`；对应验证无 sanitizer 报告且定向清理临时数据，公开入口见 [证据索引](evidence-index.md)。

恢复开启后的规模对照、故障场景及当前版本批量配置见 [recovery-load-report.md](recovery-load-report.md)，实际验收规模以报告记录为准。同步检查点的序列化与存储调用分别计时，存储时间包含共享锁等待及批量队列等待；当前不能把性能证据解释为生产容量或事件尾部方案必然更快。
