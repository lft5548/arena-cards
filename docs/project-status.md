# 当前支持范围

Arena Cards 提供服务端权威的 1v1 回合制对战、双协议客户端、事务结算、可重建缓存、单实例在线恢复和版本化回放。代码、验证入口与公开数据的对应关系见 [证据索引](evidence-index.md)。

## 服务端与客户端

| 功能域 | 当前支持 |
| --- | --- |
| 模块 | gateway/app/match/room/battle/persistence/ranking/metrics 分层，main 负责组合启动 |
| 房间执行 | 每房间 FIFO actor，启动、动作、断线、重连、计时和异步完成由唯一上下文修改状态 |
| 权威规则 | 九张代表卡牌；直接效果、中毒/再生、顺序弃牌、灼烧和有限增强；最多 40 个玩家回合 |
| 协议 | TextV1 与完整 ProtoV1 runtime、混合房间、生成 C++/Python/C# schema |
| 幂等 | match/turn/action 校验；同签名成功重试返回原 ACK，保留最近 128 条 |
| 网络 | 有界发送队列、连接准入、每连接完整帧限流、有效入站心跳和有预算退出 |
| MySQL | 对局/积分/胜负/Outbox 事务，match_id 幂等，数据库单实例锁、有界连接池、退避重连 |
| Redis | Lua 幂等排行榜、有界连接与命令等待、Outbox 自动补偿和原子重建工具 |
| 在线恢复 | 同库同规则单实例重启，保留状态、RNG、原 token/ACK、绝对期限及 pending result |
| 回放 | 终局原子写入、规则版本、连续 revision、摘要检查和离线完整状态重建 |
| Unity | UGUI 登录/匹配、卡牌、双方状态、倒计时、事件反馈、日志、结算、重连与 MySQL 排行榜 |
| 工具 | Python CLI/Pygame、Bot、压测、Admin 双协议和 FastAPI 本地管理 |

## 恢复策略

`ARENA_ROOM_RECOVERY_MODE=full|tail` 默认为 `full`。两种模式均 COMMIT 成功后 ACK、拒绝旧 owner 写入，并使用精确 blob 比较处理提交回复丢失。

`tail` 使用周期完整快照和连续的权威状态增量，保存新增事件、ACK、身份、期限和终局元数据；不重新执行客户端意图，也不依赖 Python 离线回放。存储序号独立于战斗 revision，快照发布和覆盖尾部删除在同一事务内完成。

42 场策略比较（36 场运行、6 场恢复）核对 2,520 局。尾部逻辑 payload 减少约 85–86%，但主要规模下 P99/吞吐未稳定达到预定门槛，因此默认保留完整检查点。详细条件与结果见 [策略报告](recovery-strategy-report.md)。

## 验证覆盖

| 验证层 | 已记录结果与条件 |
| --- | --- |
| Windows C++17 Release | CTest 48/48 |
| Python | 75/75；回放专项 58/58，入口可能有包含关系 |
| Linux C++17 Release | CTest 46/46 |
| Linux ASan/UBSan | CTest 46/46，37 个编译单元双重插桩，检测器探针有效，无产品报告 |
| 新恢复故障窗口 | TextV1/ProtoV1，Release 与 sanitizer 各 8/8；owner/序号/精确重试/模式转换专项通过 |
| 恢复策略比较 | 42/42 场、2,520 局，SQL/Redis/Outbox/逐局回放核对及临时数据清理 |
| 有限规模与故障 | 恢复开启 P6 35/35 场、2,170 局；双协议 100/200 Bot 六类故障 24/24 |
| Redis 专项 | Release/SAN 各 5/5 场景；持续黑洞期间结果交付、Lua 成功后回复丢失、无评分 Outbox |
| 网络边界 | DNS Windows 2/2、Linux Release/SAN 各 3/3；Windows 实际 Ctrl+C/Ctrl+Break 双协议 4/4 |
| Unity | Unity 2022.3.62f3 编译、720p/1080p 渲染、Windows Mono 构建与双协议真实联机 |

上述计数属于各验证版本和入口，不能直接相加。Linux 工具环境不含 .NET/Unity，故其 CTest 数量与 Windows 不同；强杀或 `_Exit(4)` 不完成正常退出泄漏扫描。演示入口的实际彩排结果以 [演示报告](demo-report.md) 为准。

## 运行边界

- 一房间一 actor 线程，一连接 reader/writer；队列同步、应用对象和存储仍有必要互斥，不是全进程无锁架构。
- MySQL 连接池有界为 1–8 个会话；存储 API 串行保护，连接池主要改善资源复用和连接生命周期，不承诺并行 SQL 吞吐。
- 恢复使用同库、同卡表与数据库锁名称；这是单实例重启续局，不提供多实例路由、租约或自动接管。
- 所有规模样本是报告所列机器与配置中的实测。逻辑 payload 不是 redo/binlog/磁盘写入量，进程 RSS 不等于容器内存。
- 规则使用固定状态槽位和有限次数，不是通用技能脚本系统。FNV 摘要用于一致性诊断，不是安全签名。
- Unity 是权威状态的展示层，不连接 MySQL/Redis；FastAPI 管理接口适用于本机调试。

## 演示与技术资料

[演示指南](demo-guide.md) 提供启动与检查入口、双客户端操作和故障展示；[设计取舍](design-decisions.md) 与 [技术讲解](technical-guide.md) 说明主要运行路径；[证据索引](evidence-index.md) 区分公开数据与本地生成日志。
