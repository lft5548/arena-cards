# Redis 有界等待与结果交付

本报告记录网络等待预算、权威结果交付与缓存补偿。
有限规模样本另见 [recovery-load-report.md](recovery-load-report.md)。

## 行为与边界

MySQL 事务同时提交权威结果、积分和 pending Outbox。结算 worker 成功后立即投递
`SettlementComplete`，Room actor 交付 `MatchResult`；它不再访问 Redis，也不等待缓存连接锁。
唯一 Outbox 消费路径负责 Redis Lua、applied 标记、attempts/last_error 和故障指标。
pending gauge 由数据库查询定期刷新，避免 actor 增减与 SQL 快照竞争。
无评分对局优先消费，即使 Redis 关闭、已有 256 条评分 pending 积压也可标记 applied。

`ARENA_REDIS_CONNECT_TIMEOUT_MS`、`ARENA_REDIS_IO_TIMEOUT_MS` 默认均为 1000ms，
有效等待范围为 1–30000ms；非法值采用默认，数值超界限制到边界。
连接使用非阻塞 socket，DNS 调用方等待和所有候选地址共用同一绝对截止时间。
每个 Lua 命令的全部部分发送和完整响应共用另一绝对截止时间，零碎回复不延长预算。
超时、断线和无效回复关闭连接、清空响应缓存；下一次 Outbox 轮询重连。
一次超时后停止当前评分批次，未尝试行仍为 pending，attempts=0 是正常状态。

DNS 解析任务拥有独立数据及 Windows Winsock 生命周期，每个 Redis 对象最多保留一个在途任务，
重试不会持续创建解析线程。系统 `getaddrinfo` 本身仍可能继续运行。
截止时间约束网络等待，不提供操作系统调度、内存分配或端到端硬实时保证。
DNS 延迟验证在测试编译单元包装系统 `getaddrinfo`，延迟后仍调用真实系统函数，
不修改产品解析器，也不代表外部DNS网络黑洞。最新等待上限、单个在途任务和恢复证据见
[network-boundary-tests.md](network-boundary-tests.md)；以下计数属于对应功能版本。

## 实际验收

隔离验收使用随机 MySQL schema/用户和私有 Redis 代理，不停止共享 Redis或删除命名卷。
恢复保持开启、cleanup=0、pool=2、batch=16、Redis 注入=0；针对验收将两个 Redis 等待预算
设为 100ms，终局动作到双方结果的验收上限为 2000ms。

| 验收 | Linux Release | Linux ASan/UBSan |
| --- | --- | --- |
| TextV1 黑洞，两房间共四个结果均在恢复前交付 | 39.939 / 52.721ms | 50.319 / 47.835ms |
| ProtoV1 黑洞，两房间共四个结果均在恢复前交付 | 45.895 / 43.202ms | 41.619 / 46.744ms |
| TextV1 / ProtoV1 成功 Lua 后回复丢失 | 两协议均通过，真实回复 1→0，积分 1010/990 | 两协议均通过，真实回复 1→0，积分 1010/990 |
| Redis 关闭，draw 位于 256 条评分 pending 之后 | applied，双方保持 1000 分，积压保留 | applied，双方保持 1000 分，积压保留 |
| 合计 | 5/5 场景、7 局回放核对 | 5/5 场景、7 局回放核对，无产品 sanitizer 报告 |

黑洞期间 MySQL 已提交，Outbox 保留 pending；实际尝试行记录 `Redis response read timed out`，
Redis apply/Outbox failures 递增。双协议 Admin 和实际 FastAPI `/metrics`、`/health` 均正常。
代理恢复后自动 applied，逐局 MySQL/Redis一致。接受 TCP但不回复的代理可以使连接成功，
因此 `redis_connection_failures=0` 与这项读超时故障一致。
表中的时间从终局动作开始至双方收齐结果；SQL 轮询可能晚于客户端收到结果，
原始 `observed_commit_to_both_rooms_results_ms=0` 不表示精确 COMMIT 到结果零延迟。

Linux sanitizer 精确 `redis` 与 `settlement` 崩溃窗口双协议 4/4 通过，无产品报告。
Redis 执行前暂停时，权威结果已交付且缓存仍 pending；强杀重启后原 token 可重取结果，
缓存与结算均不重复。COMMIT 后、结果前暂停仍不交付结果，重启后恢复原结果。

Windows C++17 Release 构建、CTest 40/40 与 Python 70/70（无跳过）通过，保留原 Bot、
回放、双协议 Admin/FastAPI 和 C# 兼容。新增传输测试验证部分响应超时与重连清理、
零碎回复的绝对截止时间、8MiB 阻塞发送、错误回复、连接拒绝及配置边界；
Linux 另验证监听队列占满时的实际待连接超时。最终Linux ASan/UBSan全量CTest39/39、
Python70/70和回放58/58无跳过通过，检测器探针有效、无产品报告，边界见 [sanitizers.md](sanitizers.md)。
复用代理另修正Python3.12关闭顺序：先停止活跃连接，再等待listener结束，防止单测清理挂起；
普通Python单测增加60秒CTest门禁，失败记录保留，修复后Linux代理6/6与全量回归通过。

## 复现与证据

仓库根目录 PowerShell 使用 Linux 工具镜像及 Compose 网络：

```powershell
$arenaWorkspace = (Get-Location).Path
docker run --rm --network deploy_default --mount "type=bind,source=$arenaWorkspace,target=/workspace" --entrypoint python arena-cards-sanitizers:local tests/redis_reliability_test.py --server build-linux-release/arena_server --evidence build-redis-reliability-release
docker run --rm --network deploy_default --mount "type=bind,source=$arenaWorkspace,target=/workspace" --entrypoint python arena-cards-sanitizers:local tests/redis_reliability_test.py --server build-sanitizers/arena_server --evidence build-redis-reliability-sanitizers
docker run --rm --network deploy_default --mount "type=bind,source=$arenaWorkspace,target=/workspace" --entrypoint python arena-cards-sanitizers:local tests/room_recovery_windows_test.py --server build-sanitizers/arena_server --scenarios redis settlement --evidence build-redis-recovery-windows
```

脚本生成本地隔离日志与 summary，不删除正常数据和命名卷。对应公开数据与入口见
[证据索引](evidence-index.md)。结果覆盖报告列出的单实例故障，不是生产 HA 网络分区矩阵或容量承诺。
