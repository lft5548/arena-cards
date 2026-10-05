# ASan / UBSan 验证

Linux GCC 13.3 C++17 的恢复策略版本已通过 ASan/UBSan CTest 46/46，
37 个编译单元双插桩，检测器/子进程日志捕获探针有效，无产品报告。
双协议恢复故障窗口 8/8 及真实 owner/序号/精确重试/压缩/模式转换专项通过。

Windows Release CTest 48/48、Python 75/75、回放 58/58，以及 Linux Release CTest 46/46
分别提供普通构建回归。容量和恢复时间使用普通 Release 测量，见
[恢复策略报告](recovery-strategy-report.md)。公开数据与对应入口见 [证据索引](evidence-index.md)。

## 范围与限制

Linux GCC/Clang 独立 Debug 构建启用 `ARENA_ENABLE_SANITIZERS=ON`，保持 C++17。
编译选项覆盖全部项目 C++ 目标，包括 Protobuf codec/生成代码、测试与 header-only 内核；
系统提供的 MySQL/Protobuf 共享库不重新插桩。普通 Release 镜像关闭 sanitizer。
不支持的编译器显式拒绝此开关，MSVC ASan 结果不能替代 Linux ASan＋UBSan。

`tools/build_support/run_sanitizers.py` 配置、增量构建并运行可用 CTest。
UBSan 禁止报错后继续执行；工具为每次运行保存独立报告。
入口检查 CTest 退出码与服务端子进程报告，即使 Python 丢弃 stderr 也不会隐藏报告。
故意释放后访问和有符号溢出的独立样例验证检测器与捕获链路；
detector-probes 中的预期错误不计为产品缺陷。

正常退出的 C++ 测试开启 LeakSanitizer。SIGTERM/SIGINT 排空成功的服务完成退出扫描；
`_Exit(4)` 预算耗尽与精确 SIGKILL 不完成扫描，不声明这些窗口通过泄漏检查。
ASan/UBSan 不检查数据竞争，不能证明事务幂等、owner 归属或恢复正确性，
这些由隔离集成与故障注入单独验证。

## Linux 工具环境

Docker Desktop 使用 Linux engine。在仓库根目录执行：

```powershell
docker build --build-arg ARENA_BASE_IMAGE=docker.m.daocloud.io/library/ubuntu:24.04 -f deploy/Dockerfile.sanitizers -t arena-cards-sanitizers:local deploy
$arenaWorkspace = (Get-Location).Path
docker run --rm --mount "type=bind,source=$arenaWorkspace,target=/workspace" arena-cards-sanitizers:local --parallel 4
```

默认基础镜像是官方 Ubuntu；参数可切换可访问镜像仓库。工具镜像只安装依赖，
运行时绑定源码，产物保留在 build-sanitizers，可增量使用；不替换正常服务或删除数据卷。
GitHub Actions 使用相同 Dockerfile/执行入口并上传运行证据。
该环境不含 .NET/Unity，因此其 CTest 不包含 C# 条件测试和 Unity Editor。

## 精确恢复故障窗口

先启用测试构建屏障，连接实际 Compose 网络：

```powershell
docker run --rm --mount "type=bind,source=$arenaWorkspace,target=/workspace" arena-cards-sanitizers:local --parallel 4 --enable-test-faults
docker run --rm --network deploy_default --mount "type=bind,source=$arenaWorkspace,target=/workspace" --entrypoint python arena-cards-sanitizers:local tests/room_recovery_windows_test.py --server build-sanitizers/arena_server
docker run --rm --network deploy_default --mount "type=bind,source=$arenaWorkspace,target=/workspace" --entrypoint python arena-cards-sanitizers:local tests/recovery_tail_fault_test.py --server build-sanitizers/arena_server --evidence build-recovery-tail-faults
```

脚本使用已有 MySQL/Redis，随机创建专用 schema/身份，定向清理自身记录。
开发 root 密码取 `ARENA_MYSQL_ROOT_PASSWORD` 或 Compose 本地默认值，不适用于外部部署。
网络名按实际 Compose 环境调整。

`ARENA_ENABLE_TEST_FAULTS` 默认 OFF。测试二进制识别 fault point 与 directory，
写 ready 标记并等待；脚本结合 SQL/客户端消息定位窗口再强杀，避免以随机终止替代精确注入。
屏障 60 秒超时失败，普通 Release 不提供暂停能力。

检查点提交/ACK、终局提交/结果、实例归属与尾部快照/清理的定义见
[在线恢复](room-recovery.md) 和 [策略报告](recovery-strategy-report.md)。
Redis 屏障仅暂停缓存，当前权威结果已独立交付。

## 其他专项

- Redis：持续黑洞、真实 EVAL 成功后回复丢失、无评分 Outbox，SAN 5/5 场景和 7 局回放。
- 退出：运行中/终局续局、行锁 200ms 预算耗尽、恢复关闭中止、预算内 SQL 排空，SAN 6/6。
- DNS：单在途任务、销毁后迟到释放、DNS＋TCP 共享预算，Linux Release/SAN 各 3/3。

计数属于各功能版本与测试入口，存在包含关系，不能相加为总测试量。详见
[Redis](redis-reliability.md)、[网络生命周期](network-lifecycle.md) 与
[DNS/控制台验证](network-boundary-tests.md)。
