# 阶段交付索引

阶段编号用于对应模块与验证，不表示版本生产容量。当前能力见 [支持范围](project-status.md)，使用入口见 [演示指南](demo-guide.md)。

| 阶段 | 交付内容 | 技术说明与验证 |
| --- | --- | --- |
| P0 基线 | 权威 1v1 对战、匹配、配置、客户端、MySQL/Redis 与 Compose | [架构](architecture.md)、[线协议](protocol.md) |
| M 模块边界 | gateway/app/match/room/battle/persistence/ranking/metrics 分层 | [架构](architecture.md)、[设计演进](design-archive.md) |
| P1 房间单写入 | FIFO Room actor、Tick 与异步完成回投、移除房间状态锁 | [Room actor](room-actor-migration.md) |
| P2 网络工程 | reader/writer、发送背压、连接上限、完整帧限流、心跳、预算退出 | [准入与限流](network-limits.md)、[生命周期](network-lifecycle.md)、[边界测试](network-boundary-tests.md) |
| D5 代表规则 | 九张卡、固定状态、顺序弃牌、回合结束灼烧、有限增强、版本化回放 | [战斗内核](battle-engine.md) 及规则文档 |
| P3 在线恢复 | 完整状态/RNG/token/128 ACK/绝对期限/pending result、owner 防旧写 | [在线恢复](room-recovery.md) |
| P3 策略对照 | full 与周期快照＋在线尾部、配对比较、默认方案选择 | [策略报告](recovery-strategy-report.md)，实验已收口，默认 full |
| P4 双协议 | 完整 TextV1/ProtoV1、共用生成 schema、混合房间 | [Protobuf](protobuf.md)、[协议版本](protocol-versioning.md) |
| P5 数据可靠性 | 事务、幂等、有界连接池、退避重连、Outbox、Lua 缓存和故障分类指标 | [架构](architecture.md)、[Redis](redis-reliability.md) |
| P6 有限规模证据 | 20/100/200 Bot、恢复开销和六类高规模故障 | [恢复压测](recovery-load-report.md)、[历史基线](load-test-report.md) |
| U Unity | UGUI 状态展示、联机、重连、结算、排行榜与构建 | [Unity](../client_unity/README.md) |
| P7 演示与证据 | 可复现启动/检查、客户端说明、架构取舍、证据导航和技术讲解 | [演示](demo-guide.md)、[报告](demo-report.md)、[取舍](design-decisions.md)、[证据](evidence-index.md)、[技术讲解](technical-guide.md) |

P0–P6、M、D5 与 U 已完成对应交付范围，Redis 等待/结果解耦及 DNS/Windows 控制台边界验证也已收口。P7 文档与演示工具的实际验证按演示报告记录，不用历史结果替代。

## 支持条件

- 严格 C++17，TextV1 保留兼容；ProtoV1 默认启用，显式 TextV1-only 构建报告 runtime=0。
- MySQL 是权威源；动作与终局结果在对应事务提交后确认，Redis 失败由 Outbox 补偿。
- 在线恢复要求 recovery=1、required=1、cleanup=0，并使用同库、同规则和单实例锁。
- 快照＋尾部对照保持相同 COMMIT 后 ACK 语义，包含元数据写入；离线回放不是在线恢复器。
- 有限压测以实际配置、二进制、分母和采样口径解释，不推断未测量的生产规模。

## 交付验证

源码变更按受影响的单元、协议和集成边界验证；C++ 变更使用 Linux ASan/UBSan 检查，真实存储通过 Docker/Linux 验证。文档和展示工具不需要重新执行无关的容量矩阵。

公开证据保留结果、配置、版本/二进制标识和测量方法。包含随机测试身份、凭据、本机路径的日志由本地验证工具生成，不作为直接公开材料；公开副本采用稳定样本标识与必要测量字段。
