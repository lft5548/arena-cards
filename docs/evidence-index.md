# 源码与验证证据索引

本索引把可观察行为、实现入口、公开结果和复现工具对应起来。公开 JSON 是保留技术指标的发布副本，去除本机路径、身份凭据及执行日历信息；源码版本、二进制哈希、相对耗时和测量条件保留。文件清单与 SHA256 见 [manifest.json](evidence/manifest.json)。

## 核心路径

| 能力 | 实现入口 | 结果与验证入口 |
| --- | --- | --- |
| TCP framing、取消等待、背压 | [gateway/](../server/gateway/) | [network-limits.md](network-limits.md)、[network-lifecycle.md](network-lifecycle.md)、[network-boundaries.json](evidence/network-boundaries.json)；[gateway_test.cpp](../tests/gateway_test.cpp) |
| 连接上限、帧限流、心跳与退出 | [TcpServer](../server/gateway/tcp_server.cpp)、[Session](../server/gateway/session.cpp)、[main](../server/main.cpp) | [network-boundary-tests.md](network-boundary-tests.md)；[windows_console_test.py](../tests/windows_console_test.py)、[graceful_recovery_test.py](../tests/graceful_recovery_test.py) |
| FIFO 单写入与旧连接保护 | [Room](../server/room/room.cpp)、[RoomRegistry](../server/room/room_registry.cpp) | [room-actor-migration.md](room-actor-migration.md)；[concurrent_action_test.py](../tests/concurrent_action_test.py)、[reconnect_test.py](../tests/reconnect_test.py) |
| 纯规则与确定性事件 | [BattleEngine](../server/battle/battle_engine.cpp)、[card_effect.cpp](../server/battle/card_effect.cpp) | [battle-engine.md](battle-engine.md)、[battle-status-effects.md](battle-status-effects.md)；[battle_engine_test.cpp](../tests/battle_engine_test.cpp) |
| TextV1 / ProtoV1 共用规则 | [schema](../proto/arena_cards.proto)、[codec](../server/gateway/protobuf_codec.cpp) | [protobuf.md](protobuf.md)、[protocol-versioning.md](protocol-versioning.md)；[protobuf_runtime_test.py](../tests/protobuf_runtime_test.py)、[csharp_protocol_test.py](../tests/csharp_protocol_test.py) |
| MySQL 结算、幂等与池恢复 | [MysqlStore](../server/persistence/mysql_store.h)、[schema.sql](../deploy/schema.sql) | [mysql_settlement_test.py](../tests/mysql_settlement_test.py)、[storage_fault_matrix_test.py](../tests/storage_fault_matrix_test.py)；[release-validation.json](evidence/release-validation.json) |
| Redis 黑洞下结果交付与补偿 | [Room 结算](../server/room/room.cpp)、[Outbox](../server/main.cpp)、[RedisLeaderboard](../server/ranking/redis_leaderboard.h) | [redis-reliability.md](redis-reliability.md)、[redis-reliability.json](evidence/redis-reliability.json)；[redis_reliability_test.py](../tests/redis_reliability_test.py)、[redis_dns_test.cpp](../tests/redis_dns_test.cpp) |
| 单实例在线恢复与 COMMIT 后 ACK | [RecoveryCodec](../server/room/recovery_checkpoint.h)、[启动恢复](../server/main.cpp) | [room-recovery.md](room-recovery.md)；[room_recovery_fault_test.py](../tests/room_recovery_fault_test.py) |
| 快照＋连续权威尾部 | [RecoveryTailCodec](../server/room/recovery_tail.h)、[MySQL 存储序号与压缩](../server/persistence/mysql_store.h) | [tail-fault-release.json](evidence/tail-fault-release.json)、[tail-fault-sanitizer.json](evidence/tail-fault-sanitizer.json)、[tail-owner.json](evidence/tail-owner.json)；[recovery_tail_fault_test.py](../tests/recovery_tail_fault_test.py) |
| 离线回放与诊断快照 | [battle_replay.h](../server/battle/battle_replay.h)、[snapshot_store.h](../server/battle/snapshot_store.h) | [replay_battle.py](../tools/replay/replay_battle.py)、[replay_battle_test.py](../tests/replay_battle_test.py)；离线校验不替代在线恢复 |
| Unity 权威展示 | [Unity 说明](../client_unity/README.md)、[Scripts](../client_unity/Assets/Scripts/) | 历史 Editor/Windows 双协议联机记录在客户端说明；当前演示记录见 [demo-report.md](demo-report.md) |
| ASan / UBSan | [sanitizer 执行器](../tools/build_support/run_sanitizers.py)、[CMakeLists.txt](../CMakeLists.txt) | [sanitizers.md](sanitizers.md)、[sanitizer-validation.json](evidence/sanitizer-validation.json) |

## 性能报告：分开理解各代样本

| 样本集合 | 公开数据 | 条件与含义 |
| --- | --- | --- |
| 早期 Compose 小规模 | [load-test-compose-4.json](load-test-compose-4.json)、[load-test-report.md](load-test-report.md) | 4 Bot 历史样本；吞吐为玩家完成轮次/s，不能与真实局/s直接比较 |
| 历史规模与故障矩阵 | [load-tests/matrix.json](load-tests/matrix.json)、[20 Bot](load-tests/20-bot.json)、[100 Bot](load-tests/100-bot.json)、[200 Bot](load-tests/200-bot.json) | 使用对应版本与旧资源口径；不是当前默认策略的新测量 |
| 完整恢复与批量 COMMIT | [recovery-load-report.md](recovery-load-report.md)、[recovery-load-report.jsonl](recovery-load-report.jsonl) | 历史与新增样本共 51 条；该交付轮 35/35 场、2,170 局核对，双协议高规模六类故障24/24 |
| full/tail 同版本对照 | [recovery-strategy-report.md](recovery-strategy-report.md)、[recovery-strategy-report.json](recovery-strategy-report.json) | 同二进制，pool=2、batch=16、间隔16；36 场运行＋6 场恢复、2,520 局；默认 full |

不同集合可能包含复测或重叠场景，计数不相加成一个累计总数。历史 Redis blackhole 样本证明受控恢复后的最终一致性，部分结果曾等待恢复；黑洞期间及时结果交付由独立 Redis 可靠性证据验证。

full/tail 比较覆盖 20/100/200 Bot 两类负载各三次，两种负载在固定种子下均到 40 回合。三次中位数只是展示摘要，默认选择还依据每对改善与预定门槛；不能从中位数隐藏退化样本。逻辑 payload 不是物理磁盘写入；CPU/RSS 为 `/proc` 进程采样，共享 mysqld 包含后台活动。

## 验证口径与复现

服务端功能交付记录包括 Windows C++17 Release CTest 48/48、Python 75/75、Linux Release 与 ASan/UBSan CTest 各46/46；这是对应构建的原始结果。不同平台启用目标不同，各套测试有包含关系，不能相加成互不重复的测试总数。P7 演示彩排与此前源码验证分别记录，未运行的项目不推定通过。

| 任务 | 工具 | 前提 |
| --- | --- | --- |
| 日常 C++ / Python 回归 | [run_tests.ps1](../tests/run_tests.ps1)、[run_tests.sh](../tests/run_tests.sh) | 当前平台配置与依赖；Windows CTest 使用 Release 配置 |
| 策略公平比较 | [recovery_compare.py](../tools/bot/recovery_compare.py) | 真实 MySQL/Redis、同一测试二进制、固定 seed；命令见策略报告 |
| 新恢复故障边界 | [recovery_tail_fault_test.py](../tests/recovery_tail_fault_test.py)、[mysql_commit_proxy.py](../tools/bot/mysql_commit_proxy.py) | 测试构建显式开启故障屏障；隔离 schema；代理测试不宣称生产 TLS 黑洞 |
| Redis 故障 | [redis_reliability_test.py](../tests/redis_reliability_test.py)、[redis_fault_proxy.py](../tools/bot/redis_fault_proxy.py) | 临时 Arena 与私有代理，真实缓存验证、定向清理 |
| 内存与 UB 检测 | [run_sanitizers.py](../tools/build_support/run_sanitizers.py) | Linux ASan/UBSan 构建，探针确认检测器有效；精确强杀不完成退出泄漏扫描 |
| 双客户端可视演示 | [demo-guide.md](demo-guide.md) | 当前 Compose 服务、Unity 构建或 Editor；实际执行范围见演示报告 |

## 阅读路径

先读 [architecture.md](architecture.md) 看状态和线程边界，再读 [design-decisions.md](design-decisions.md) 理解一致性与性能取舍。沿 [technical-guide.md](technical-guide.md) 的三条调用路径查源码，最后核对各报告、公开 JSON 与 manifest。

公开资料保留技术限制：单实例恢复、一房间一线程、串行存储 API、本机压测、FNV 非安全签名。它们决定证据支持什么结论，不能以文字整理扩大保证。

演示交付公开结果：[关服续局](evidence/demo-graceful.json)、[Redis补偿](evidence/demo-redis.json)、[Unity真实存储](evidence/demo-unity.json)。源码与附件交付说明见[publication-guide.md](publication-guide.md)。
