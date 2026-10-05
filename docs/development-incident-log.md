# 故障与排障案例

这些案例说明故障如何影响状态、确认和资源边界。结果与测试入口见 [证据索引](evidence-index.md)；本文保留产品机制，运行工具产生的本机日志单独保存。

## 旧二进制造成协议误判

测试曾读到缺少 token 或 match_id 的响应，原因是误用了根目录残留二进制。集成测试现在显式接收构建产物，CTest 使用目标文件路径，避免源码已修改但实际进程仍运行旧版本。判断协议异常前应核对启动命令与二进制 SHA256。

## Redis 初次积分缺少基线

首次在线结算曾得到 Redis 10/-10、MySQL 1010/990。ZINCRBY 对不存在成员从零开始，不能直接代表权威积分。Lua 先用 1000 初始化缺失成员，再执行增量，并以 match_id 保存幂等标记；真实结算和重复消费分别验证初值与幂等。

## 排行榜重建留下半榜

先删正式 ZSET 再逐行写入，在中途断开时会留下空榜或部分数据。重建器现在显式选择 MySQL 数据库，先填充临时 key，全部成功后 RENAME 原子替换；空结果按约定删除正式榜。工具可重复执行，Redis 是缓存，权威数据仍来自 MySQL。

## 启动清理与数据库归属

对同库全部 running 记录执行清理会误伤另一个服务进程。MySQL GET_LOCK 约束同数据库单实例；遗留清理要求显式 cleanup=1，在线恢复要求 cleanup=0。有恢复检查点时拒绝清理启动，避免丢弃可恢复对局。

## 重连与旧 Disconnect 乱序

旧连接的关闭状态、Disconnect 与新连接的 Reconnect 曾存在观察顺序问题。Session 先关闭 socket 并发布 closed，再提交 Disconnect；actor 在合法 token 重连时检查旧 Session 状态。旧 Disconnect 只匹配原 Session 对象，不能清除已接管的新连接。

## 网络写阻塞房间执行

直接在 actor 调用 send_all 会让不读数据的客户端拖住房间。Session 使用独立 writer 和 FIFO 有界发送队列，actor 只入队；超限关闭连接并释放队列。真实不读响应连接与正常 Bot 并行测试验证了背压和隔离，不能据此推断无限队列容量。

## MySQL 结算阻塞房间执行

同步结算把 SQL 等待叠加在 actor 上。现在复制结算参数交给 worker，成功后回投 SettlementComplete，失败保持 pending 并退避重试。actor 只在权威事务成功后交付结果；原 action_id 的成功回执仍可重放。

## Redis 黑洞拖住结果交付

MySQL 已提交，Redis 网络等待却曾拖住 MatchResult。结算 worker 现在不访问 Redis，Outbox worker 是唯一缓存消费者。连接预算与整条命令 I/O 预算使用绝对截止时间，超时失效 socket；持续黑洞下结果及时交付，Outbox 保留错误与 pending，恢复后自动 applied。

成功 Lua 执行后回复丢失时，重试返回“已处理”而不再次加分；测试代理转发真实 EVAL 后丢回复，分别核对 MySQL、Redis 和 Outbox。细节见 [Redis 可靠性](redis-reliability.md)。

## 协议 namespace 兼容

把 Protobuf bytes 放入历史文本消息 ID 会让旧客户端错误解析。ProtoV1 使用独立 0x4001 起的 ID，在登录前协商；错误 namespace 明确拒绝，不静默降级。TextV1 和 ProtoV1 在同一 listener 运行，共用权威规则，不维护第二套战斗逻辑。

## 卡表变化与旧回放

新增卡牌会改变确定性循环牌库，直接用新 CSV 重建旧回放会改变抽牌序列。match_start 保存规则版本与牌库，重建器校验对应 CSV；legacy、status_v1、discard_v1、turn_end_v1 和 bonus_v1 分别保留语义。回放文件不内嵌整份卡表，旧版本需使用原配置。

## 终局文件与实时恢复边界

内存事件随进程退出而消失。终局使用不可变 BattleReplay 副本，后台临时写入后原子替换，失败只记录回放指标。离线工具检查连续 revision、摘要并重建结果；实时房间恢复另用 MySQL 检查点/在线尾部，不能以离线重放替代。

## COMMIT 回复丢失与候选变化

事务已经成功、成功回复丢失时，恢复写入必须精确重试原序号和 blob。断线曾修改正在重试的候选，导致完全相同 blob 检查始终失败。PendingDisconnect 现在保存原断线时间与绝对期限，原候选确认后再按新序号持久化断线。

私有 MySQL 代理实际丢弃成功 COMMIT OK，并用独立 SQL 确认提交；双协议故障测试验证精确重试、原 ACK 和完整恢复。该代理只对测试连接关闭 TLS 协商，不代表生产 TLS 黑洞验证，见 [恢复策略报告](recovery-strategy-report.md)。

## 工具与测试边界

测试 launcher 继承忽略 Ctrl+C 属性会让控制台信号注入失败；独立 worker 清除属性后验证真实 Ctrl+C/Ctrl+Break。DNS 恢复测试的 IPv4 私有对端使用明确地址，避免 localhost 的 IPv6 优先顺序改变测试目的。

这些问题属于验证工具行为；最终通过结果不会把早期失败样本改写为成功。操作系统信号、强制终止、系统调用延迟和真实网络故障各有不同证据含义。
