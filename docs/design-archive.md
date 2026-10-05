# 设计演进

本文记录重要方案变化及其当前落点。详细取舍见 [design-decisions.md](design-decisions.md)，模块和线程关系见 [architecture.md](architecture.md)。

## 从集中实现到职责边界

早期实现将连接、匹配、房间和规则集中组合。现在 main 只负责构建依赖与启动，gateway 处理网络，app 路由身份和业务，match 配对，room 管理状态生命周期，battle 执行规则，persistence/ranking/metrics 处理存储、缓存和观测。

拆分以依赖与状态所有权为目的，协议和规则保持一套实现。Battle 不依赖 socket、数据库或 UI；Matchmaker 不结算。

## 从房间 mutex 到 actor

早期状态由 Room::mu_ 保护，网络发送和同步 SQL 可能延长持锁时间。当前房间命令按 FIFO 进入唯一 actor；计时、断线、重连和异步完成一起串行处理，房间战斗状态不再需要 mutex。

命令队列、Session、应用共享对象和存储仍有各自同步。每房间一个线程使状态边界明确，也有线程数和调度开销，不能宣称全进程无锁或任意规模伸缩。

## 从同步 I/O 到完成回投

网络发送改为有界队列与独立 writer，SQL 结算和终局回放使用后台任务。worker 使用复制数据，成功或失败以 RoomCommand 回投，不跨线程直接修改房间。

ShutdownTracker 持有 joinable 任务句柄，关服统一排空；预算到期失败退出，避免无限 join 或销毁仍被线程使用的依赖。关闭 socket 不撤销 SQL，提交不确定性仍由幂等和恢复保证处理。

## 从单文本到双协议

TextV1 保留调试兼容，ProtoV1 使用独立 ID、显式协商和 C++/Python/C# 共用 schema。协议适配归一到已有业务路径，允许混合房间。固定依赖和生成代码维持 schema 一致，不能把客户端二进制适配写成第二套规则引擎。

## 从直接缓存写入到 Outbox

MySQL 事务同时写结果、积分和 pending Outbox，MatchResult 以 MySQL 成功为条件。Redis 缓存由唯一 Outbox worker 消费，Lua 幂等处理重复和成功回复丢失；失败留下 attempts/last_error，恢复后重试。

Redis connect/command I/O 使用明确预算。DNS 任务独立持有数据及 Winsock 生命周期，调用方等待超时不产生无界解析线程；系统解析本身可能迟到完成。

## 从离线回放到在线恢复

终局回放用于诊断与离线状态对照；在线恢复另保存状态、RNG、身份、原 ACK、绝对期限和待交付结果，保证已确认动作不回退。

完整检查点从单次提交改为跨房间批量 COMMIT。依据分段指标减少提交/等待开销，确认边界仍是同 COMMIT 成功后 ACK，未通过缩短确认路径换取数据。

## 周期快照与权威尾部对照

tail 保存状态差异、新事件、ACK 和元数据，存储序号独立于战斗 revision。快照发布和覆盖尾部删除在同一事务中完成，启动一致性读取并严格校验连续性。

固定版本、配置、操作轨迹和采用门槛后比较运行、写入量与恢复时间。tail 减少约 85–86% 逻辑 payload，但主要规模收益不稳定；full 保持默认，tail 可显式配置。两种模式可在停服后读取原格式并原子转换，不删除对局。

## 展示与证据

Unity 专注 UI、网络和反馈，Python Bot 保留自动化验证。规则、故障、性能和展示分别提供真实结果；公开说明以源码、固定条件与可核对数据为依据。
