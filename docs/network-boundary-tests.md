# DNS 延迟与 Windows 控制台退出验证

本文记录 Redis DNS 等待边界和 Windows 实际控制台信号，分别说明注入方法与测量限制。

## DNS 延迟

`redis_dns` 在独立测试编译单元包装 `getaddrinfo`：先受控阻塞/延迟，再执行真实系统解析，
只替换该测试的调用，不给产品增加故障开关。观察实际调用次数、在途数和地址释放，验证：

- 120ms 连接预算到期时返回 DNS 超时且没有有效 socket；连续四次重试，包括改目标，最多一个在途解析任务。
- 解除阻塞后复用解析结果，完成私有 TCP 对端的 Redis 命令发送和成功回复读取。
- Redis 对象先销毁、解析随后完成，调用方析构不等待阻塞解析，独立任务安全释放地址。
- Linux 额外在 DNS 延迟 200ms 后连接已满 backlog，整个调用共享 300ms 预算，不重新分配连接预算。

私有对端只监听 IPv4，恢复用例显式解析 `127.0.0.1`，避免 localhost 的 IPv6 优先顺序
把 DNS 恢复验证变成另一地址的连接等待。此注入验证系统调用延迟下的应用行为，
不声称构造了外部 DNS 网络故障或硬实时调度保证。

Windows Release DNS 2/2通过，四次120ms预算实测128/132/132/134ms；Linux Release及
ASan/UBSan各3/3通过，四次均约120ms，DNS+TCP共享300ms预算实测300/301ms。
所有重试观察到解析调用数1、峰值在途数1，释放后私有命令回复和迟到地址释放均成功。

## Windows 控制台退出

`windows_console` 使用独立隐藏 `CREATE_NEW_CONSOLE`，在生成事件前检查控制台进程列表，
确认测试协调进程未附着。worker 清除继承的忽略 Ctrl+C 属性，服务端继承该独立控制台，
没有以新进程组屏蔽 Ctrl+C。测试向该控制台发送真实 `GenerateConsoleCtrlEvent`。

Ctrl+C、Ctrl+Break × TextV1、ProtoV1 共4项均正常退出0，最终测量0.265–0.516秒，
专项退出预算1000ms，另允许750ms调度余量。每项先取得心跳与 Admin 响应，
再保留空闲和半帧连接；退出后检查两连接关闭、listener关闭，以及 accept stopped →
transports closed → transports drained → complete 的日志顺序。强制终止仅用于失败清理。
该Windows主构建没有MySQL Connector，专项关闭MySQL/Redis，不作为Windows存储验收。

## 验证结果

| 平台 | 实际结果 |
| --- | --- |
| Windows C++17 Release | DNS 2/2、控制台 4/4；相邻 Redis 传输与 Python 71/71 |
| Linux Release | 定向 CTest 2/2（DNS 3 场景＋Redis 传输 12 场景） |
| Linux ASan/UBSan | 同一定向 CTest 2/2，退出泄漏检测开启，无产品报告 |

控制台各场景均正常退出，无强制失败清理。早期测试 launcher 的 Ctrl+C 忽略属性，
以及 DNS 恢复对端的 IPv4/IPv6 地址不匹配分别修正后复验；最终结果仅对应通过样本。
测试和 CTest 入口变化没有修改产品实现；真实存储结果由相关存储报告提供。

## 复现

仓库根目录 Windows Release：

```powershell
cmake --build build-msvc --config Release --target redis_dns_test arena_server --parallel 4
ctest --test-dir build-msvc -C Release -R '^(redis_dns|windows_console)$' --output-on-failure
```

Linux 在工具镜像中构建 redis_dns_test/redis_transport_test，随后执行：

```bash
cmake --build build-linux-release --target redis_dns_test redis_transport_test --parallel 4
ctest --test-dir build-linux-release -R '^(redis_dns|redis_transport)$' --output-on-failure
```

sanitizer 构建使用对应 build-sanitizers 目录，并开启错误即失败和报告检查；
环境与边界见 [sanitizers.md](sanitizers.md)，证据导航见 [evidence-index.md](evidence-index.md)。
