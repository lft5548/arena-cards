# 文档导航

Arena Cards 使用 C++17 执行权威规则，Unity 和 Python 负责网络请求与展示，MySQL 是权威数据源，Redis 是可重建缓存。

## 使用与技术说明

| 文档 | 内容 |
| --- | --- |
| [演示指南](demo-guide.md) | 环境准备、启动与检查、双客户端对局、在线续局和 Redis 补偿 |
| [演示结果](demo-report.md) | 实际彩排结果、运行条件与边界 |
| [Unity 客户端](../client_unity/README.md) | 场景生成、依赖、构建与联机 |
| [Python 客户端](../client_pygame/README.md) | CLI/Pygame 协议调试 |
| [架构](architecture.md) | 模块、线程、状态所有权与持久化路径 |
| [设计取舍](design-decisions.md) | actor、事务、Outbox、恢复与性能取舍 |
| [技术讲解](technical-guide.md) | 一次动作、一次结算和一次恢复的端到端路径 |
| [支持范围](project-status.md) | 当前实现、配置与运行边界 |
| [阶段交付](phase-delivery.md) | 各模块与交付内容的对应关系 |
| [架构路线](architecture-scope.md) | 功能域、支持条件与验证要求 |
| [证据索引](evidence-index.md) | 源码、测试入口、公开样本与测量口径 |

## 协议与规则

| 文档 | 内容 |
| --- | --- |
| [线协议](protocol.md) | 帧、消息、ACK、重连与私有状态 |
| [协议版本](protocol-versioning.md) | 协商、ID 隔离与兼容 |
| [Protobuf](protobuf.md) | 共用 schema、固定依赖与生成 |
| [战斗内核](battle-engine.md) | 权威校验、动作、事件与确定性 |
| [持续状态](battle-status-effects.md) | 中毒、再生和回合开始触发 |
| [弃牌](battle-discard.md) | 顺序弃牌、私有历史与回放 |
| [回合结束](battle-turn-end.md) | 灼烧与触发顺序 |
| [有限增强](battle-bonuses.md) | 攻击/治疗增强和次数消耗 |
| [Room actor](room-actor-migration.md) | 队列、完成回投、同步与生命周期 |

## 可靠性与实测

| 文档 | 内容 |
| --- | --- |
| [网络准入](network-limits.md) | 连接上限、帧令牌桶与指标 |
| [心跳与退出](network-lifecycle.md) | 有效入站期限、退出预算与房间策略 |
| [DNS 与控制台](network-boundary-tests.md) | 延迟解析、共享预算和真实 Windows 信号 |
| [Redis 可靠性](redis-reliability.md) | 有界等待、结果交付与幂等补偿 |
| [在线恢复](room-recovery.md) | full/tail、owner、存储序号与故障窗口 |
| [恢复策略对照](recovery-strategy-report.md) | 配对样本、写入量、运行与恢复时间 |
| [恢复开启压测](recovery-load-report.md) | 分段持久化指标、故障与规模 |
| [历史无恢复基线](load-test-report.md) | 历史口径与有限规模结果 |
| [ASan/UBSan](sanitizers.md) | 工具环境、插桩范围和检测限制 |
| [故障案例](development-incident-log.md) | 根因、修复机制与验证入口 |
| [设计演进](design-archive.md) | 重要设计变化与当前方案 |

## 部署与开发

Docker Compose 配置为 `deploy/docker-compose.yml`，默认映射游戏 `9000`、MySQL `3307`、Redis `6379`。FastAPI 是独立的本地调试工具，使用 `python -m tools.admin_api.app --port 8080` 启动，只绑定本机；`/command` 只允许管理查询和心跳。

MySQL 本地配置包括 `ARENA_MYSQL_ENABLED`、`HOST`、`PORT`、`DATABASE`、`USER`、`PASSWORD`；设置 `ARENA_MYSQL_REQUIRED=1` 后连接失败会拒绝启动。不要把个人数据库凭据提交到仓库。

同库 Arena 进程使用 MySQL `GET_LOCK` 单实例锁。在线恢复要求 `ARENA_ROOM_RECOVERY=1`、`ARENA_MYSQL_REQUIRED=1`、`ARENA_MYSQL_CLEANUP_RUNNING=0`。如果关闭恢复且明确选择作废遗留 running 对局，才使用显式清理配置；有恢复检查点时拒绝清理启动。

卡表采用五列/六列 CSV，启动时校验 ID、费用、效果与数值。重复动作沿用原 action_id；后台 SQL 与网络关闭都可能产生提交不确定性，不能假设没有 ACK 就一定没有提交。性能、正确性和 sanitizer 结果分别按其测试条件解释。
