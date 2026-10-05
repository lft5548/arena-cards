# 心跳失效与优雅退出

有效入站心跳清理失效连接，计划关服按共享绝对预算排空网络与存储。
连接准入和请求限制见 [network-limits.md](network-limits.md)。

## 心跳失效

`ARENA_HEARTBEAT_TIMEOUT_MS` 默认30000，范围100–300000；未设置/空值使用默认，
非法值在存储初始化前以退出码2拒绝启动。每个连接创建时建立 steady_clock 入站截止时间。
成功协商或当前协议下语法完整的请求刷新截止时间，包括心跳、Admin和游戏请求。
游戏请求刷新只表示协议语法正确；费用、回合、房间和动作合法性仍由权威规则校验。

长度头/正文未读完、逐字节发送、协议不匹配、未知消息、错误Protobuf、缺失动作字段或错误数字
都不能刷新截止时间，服务端主动推送、错误响应和Pong发送也不刷新。
一次收帧的header/body共用同一个截止时间，不能通过慢速补字节延长寿命。
accept loop每250ms巡检连接，在reader等待业务回调时也能关闭过期socket；
没有为每连接新增心跳线程。正常reader自身也在入站截止时间到达时关闭。
失效连接走既有单次Disconnect路径，名额待reader/writer及回调完成、Session析构后释放。
如果业务回调卡在SQL，关闭socket不能撤销SQL，也不保证立刻回收其线程；资源上限仍约束存活Session。

新增 `heartbeat_timeouts` uint64累计指标，一次超时关闭计一次，进程重启归零；
TextV1/ProtoV1 Admin和FastAPI保留旧字段，新字段缺失时省略。
Unity和原Bot已有周期心跳，默认30秒预算与其行为兼容。

## 退出顺序

Linux处理SIGTERM/SIGINT；Windows注册CRT SIGTERM/SIGINT/SIGBREAK。
signal handler仅设置sig_atomic_t标志，不执行锁、日志、socket或SQL操作。
主accept loop观察标志后按以下顺序退出：

1. 关闭listener，停止新的连接准入。
2. 设置应用stopping状态，禁止新业务/匹配并清空匹配队列，停止Outbox下一轮轮询。
3. 关闭现有socket，等待reader/writer/Disconnect回调结束及Session释放。
4. 向仍存活Room投递FIFO Stop，停止自动回合/断线判负，排空在途检查点、结算和回放任务。
5. join管理的actor/存储任务和Outbox，关闭Redis/MySQL及实例锁，正常返回0。

原actor/结算/回放的线程模型保留，但应用线程由ShutdownTracker持有joinable句柄，
新任务创建时回收已完成句柄，退出时统一join。弱房间列表不延长房间生命周期。
已经确认的动作依旧先COMMIT再ACK；关闭传输时未确认动作可能已提交或未执行，
客户端重启后必须重试原action_id，不承诺排队但尚未确认的动作一定生效或一定回滚。
关闭会清空发送队列，当前不承诺向每个客户端送达关服通知或全部排队响应。

`ARENA_SHUTDOWN_TIMEOUT_MS` 默认10000，范围100–60000，非法配置拒绝启动。
accept停止后所有排空阶段共用一个steady_clock绝对截止时间；watchdog到期以`_Exit(4)`失败退出，
避免无限join，或销毁仍被阻塞线程使用的依赖。超预算不是优雅退出成功，没有正常退出泄漏扫描。
实际SQL提交可能在进程退出后完成，恢复端仍按完整检查点/事务幂等解决提交不确定性。

## 房间与存储策略

恢复开启时保留完整检查点、原token/最近128 ACK/RNG/pending result和绝对截止时间。
关服引起的连接关闭不写玩家断线截止时间、不制造赢家；已断线玩家原截止时间保留。
在途SQL可在退出预算内完成；未完成候选检查点继续既有重试，超过预算则失败退出。
已提交终局及Outbox可在重启后重取结果/自动补偿，不要求缓存pending全部清空才关服。
新实例仍须相同库、卡表和锁名称，沿用owner校验，不提供自动多实例接管。

恢复关闭时，仅将本实例尚未完成的match标记`aborted/server_shutdown`，
不写match_results/Outbox、不加减积分；已经finished的事务保持不变。
没有MySQL时未完成内存房间随进程终止，不生成对局结果。

Compose使用SIGTERM，默认15秒停止宽限，大于默认10秒应用预算；
若人为增大应用预算，应同步设置Compose停止宽限大于它，否则容器可能先被SIGKILL。
原9000/3307/6379端口、recovery=1、cleanup=0和命名卷保留。

## 验收入口与边界

`heartbeat` CTest验证收帧截止时间、有效入站刷新、仅出站无刷新、语法错误、
阻塞回调时巡检关闭、单次计数及线程/引用释放；`gateway_shutdown`验证停接、
应用冻结先于关闭、空listener退出及同预算内的阻塞回调返回失败。
`lifecycle`用真实socket验证双协议空闲/半帧/慢滴、有效心跳、错误帧和主动推送、
连接名额回收，以及Linux实际SIGTERM/SIGINT；Windows不把TerminateProcess冒充信号验收。

`tests/graceful_recovery_test.py`使用临时MySQL schema/用户，覆盖双协议运行中/终局关服续局、
200ms预算下真实检查点行锁阻塞的退出码4/无错误ACK、恢复关闭时无计分中止，
以及预算内释放行锁后在途SQL完成、重启原ACK幂等重取。脚本定向清理本次数据，保留共享服务和卷。
入口支持`--scenarios live locked nonrecovery inflight`只复验受影响场景。

Windows 实际 Ctrl+C/Ctrl+Break × TextV1/ProtoV1 共 4/4 通过，详细注入方法见
[network-boundary-tests.md](network-boundary-tests.md)。Linux 实际 SIGTERM/SIGINT 与
Windows CRT/控制台事件分别验证，不以 TerminateProcess 代替信号。

## 验证结果

对应功能版本的 Windows C++17 Release CTest 45/45、Python 71/71、回放 58/58通过；
Linux ASan/UBSan CTest 44/44，35 个双插桩编译单元，探针有效且无产品报告。

heartbeat C++ 专项 9 场景、gateway_shutdown 3 场景通过。Linux Release 真实 socket 专项中，
600ms 配置下空闲/半帧/慢滴约 601–602ms 失效，有效入站刷新、无效/出站不刷新、
名额回收及 SIGTERM/SIGINT 退出 0。

Release 与 sanitizer 各 6/6 真实 MySQL 场景通过，均定向清理临时数据：
双协议运行中/终局关服续局，双协议真实行锁 200ms 预算退出 4 且无错误 ACK，
TextV1 恢复关闭无计分中止，TextV1 停止后 250ms 解锁 SQL 并正常排空。
Release 预算耗尽约 215ms；解锁场景约 365ms 返回 0，重启保持原 ACK/RNG/绝对期限。

实际 Compose stop 的 SIGTERM 退出 0，日志顺序为 accept stopped、transports closed、
transports drained、complete；重启后服务 healthy。存储专项主预算 2000ms，耗尽场景 200ms，
均不是部署默认值。强杀/预算耗尽不完成正常退出泄漏扫描。

对应源码、入口和公开数据见 [证据索引](evidence-index.md)；插桩边界见
[sanitizers.md](sanitizers.md)。
