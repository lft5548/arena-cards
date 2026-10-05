# 连接准入与请求限流

连接上限与消息限流约束网络资源消耗。心跳清理和预算退出见
[network-lifecycle.md](network-lifecycle.md)。规则、动作幂等和存储确认仍由各自边界负责。

## 配置与行为

| 环境变量 | 默认 | 有效范围 | 含义 |
| --- | ---: | ---: | --- |
| `ARENA_MAX_CONNECTIONS` | 512 | 1–4096 | 同一 TcpServer 的活跃 Session 上限 |
| `ARENA_REQUEST_RATE_PER_SECOND` | 200 | 1–10000 | 每连接每秒补充的帧额度 |
| `ARENA_REQUEST_BURST` | 400 | 1–20000 | 新连接初始额度与最多积累额度 |

未设置或空值采用默认；其他值必须为范围内的十进制数字，0 不表示禁用。
非法值在连接数据库或启动 worker 之前以退出码 2 拒绝启动，日志指出变量名。
Compose 显式传入这三个配置，原服务端口及命名卷保持不变。

单个 accept loop 在创建应用 handler、Session 和两个 I/O 线程之前检查上限。
达到上限时直接关闭刚 accept 的 socket，不执行协议解析或同步响应写入。
未登录连接、游戏连接、Admin 和健康探测都占名额；名额在 Session 析构后释放，
包含已经关闭但 reader/writer 尚未退出的对象，避免提前放行后产生更多存活线程。
准入检查依赖当前单 accept loop 的唯一创建者边界，不能直接复用于多个 accept worker。

每个连接 reader 独占一个基于 steady_clock 的令牌桶，初始 burst，按时间补充并保留小数额度，
最多积累 burst。每收到一条长度合法的完整帧，先消耗一个额度，再执行 Hello、协议校验、
Protobuf 解码或业务派发。心跳、Admin、重复动作以及错误协议/错误载荷均不绕过计费。
首次额度不足即走原有 close/单次 Disconnect 回调路径，不派发超限帧、不生成对应 ACK。
此前已经提交的动作仍以原 action_id 幂等重试；重连使用新连接的独立额度。
关闭会清空待发送队列，因此超限时不承诺客户端收到特定 Error 帧或此前排队响应。

这是每连接的帧速率限制，不是每 IP、账户、字节或全进程请求限额。
房间 actor 仍按既有断线/重连生命周期保留；没有增加房间总量上限。
空闲或半帧连接占名额至 Session 释放；当前版本按入站绝对截止时间清理，
关服时的房间处理与退出预算见 [network-lifecycle.md](network-lifecycle.md)。

## 指标与验收

`connections_rejected` 累计因连接上限而拒绝的已 accept socket。
`requests_rate_limited` 累计因首条超限帧而关闭的连接，一次关闭计一次，
不等于该连接发送的超限帧总数。两者随进程重启归零。
TextV1/ProtoV1 AdminRooms 输出 uint64，FastAPI `/metrics` 按整数转发并校验 uint64 范围；
旧字段保留、旧后端缺失新增字段时省略。

`request_limiter` CTest 用显式时间验证突发、分数额度补充、上限、时间回退和连接隔离。
`network_limits` CTest 使用隔离无存储进程，所有 socket 等待有上限：
24 项非法配置、未登录占名额、六次超限拒绝、六次释放/替换连接、双协议限流及补充额度，
还覆盖异常帧计费、正常连接不受另一个刷请求连接影响、新连接独立额度。
原 Bot/回放/重连及双协议回归继续纳入全量 CTest。

Windows C++17 Release 对应验证记录为 CTest 42/42、Python 71/71、回放 58/58；
Linux ASan/UBSan CTest 41/41、33 个编译单元双插桩，探针有效且无产品报告。
同一部署 Release 二进制另通过网络专项、双协议真实 MySQL/Redis/Outbox/Admin 与 FastAPI。
这些是该功能版本的验证，不是吞吐或生产容量测量。当前交付与公开证据见
[支持范围](project-status.md) 和 [证据索引](evidence-index.md)。

## 复现

在仓库根目录，使用 Windows Release 构建：

```powershell
ctest --test-dir build-msvc -C Release -R 'request_limiter|network_limits|application_modules' --output-on-failure
```

Linux 工具镜像与插桩入口见 [sanitizers.md](sanitizers.md)。
