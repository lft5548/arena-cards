# 演示交付验证

本次交付增加部署检查、源码打包、公开证据和技术说明，复用已验证的服务端与 Unity 构建。服务端 C++ 与 Unity C# 源码没有修改；本记录与此前功能、sanitizer、容量实验分开引用。

## 部署与探针

`tools/demo/demo.py start --no-build` 和 `check` 均实际执行成功。MySQL、Redis、Arena 三服务 healthy；端口保持 3307、6379、9000；恢复开启、cleanup=0、full、快照间隔16、batch16、pool2、Redis注入0。SQL running、pending Outbox 与半结算计数均为0；TextV1/ProtoV1 Admin 可达。

复用镜像 `sha256:9d34ed56cab253abb56c68f29efd1951126bdf8e4fe97bb1172224e66e71f483`，Arena 二进制 `baf0c437cbb54f021cdb90d97928daf1139867ce85841d56f7690e8e8b6db6e9`。本次没有重建服务端镜像；启动入口的默认构建路径提供正常源码构建，实际彩排选择已部署镜像。

探针兼容早期已结算但没有 Outbox 的历史数据，仍拒绝终局缺结果或非终局存在结果/Outbox 的异常。新演示对局单独核对 Outbox，未通过删除历史数据使检查通过。

## Unity 实际联机

复用 Unity 2022.3.62f3 Windows Mono player。导出项目的全部 C# 文本与仓库源码核对一致；构建可执行文件 SHA256 为 `f5f73d8616a2500e0fb0223d83774e2d7f6a74c1bbdad772f4fefc9bf5812036`。

TextV1 与 ProtoV1 各完成一次 opt-in 联机彩排，两次退出码均为0并生成成功标记与真实渲染截图。每次在同一 Unity 进程运行两个独立网络客户端，覆盖登录/匹配、伤害/治疗/护盾、私有快照、原 token 重连、双方结果和来源为 MySQL 的排行榜。它不是两个人工操作窗口的验收。

两局均核对 finished、唯一结果、唯一 applied Outbox，MySQL 与 Redis 积分为1010/990、胜负计数正确、缓存幂等标记存在；随后仅清理本次测试身份、对局、缓存和回放。当前没有重新运行 Unity Editor 编译或构建；原 Editor/构建覆盖见 [客户端说明](../client_unity/README.md)。

彩排使用 `-batchmode`，保留图形渲染、不使用 `-nographics`，核对成功标记和截图。README 展示图来自真实联机截图，测试身份已遮盖、日志区域已裁剪。

## 续局与缓存补偿

隔离彩排复制上述生产 Arena 二进制，使用共享 MySQL/Redis 上的随机临时数据库与私有代理，保持共享服务持续运行。

| 彩排 | 结果 | 观察 |
| --- | --- | --- |
| 双协议关服续局 | 2/2 | 实际 SIGTERM/SIGINT，检查点及原 ACK 一致、绝对期限保留、终局重取不重复计分；退出0 |
| Redis 既有隔离场景 | 5/5 | 双协议黑洞与成功 Lua 回复丢失，另验证评分积压不阻止无评分结果；临时数据清理完成 |

黑洞期间四个房间的终局动作至双方结果为45.897–54.594ms，门禁2000ms，结果在恢复前到达。失败任务保留 pending 与错误指标，恢复后 applied；Lua 成功回复丢失后收到执行结果1、幂等重试0，最终分数仅更新一次。

这些是有限功能彩排，不是容量样本或生产 SLA。没有追加大规模矩阵。公开汇总见 [demo-graceful.json](evidence/demo-graceful.json)、[demo-redis.json](evidence/demo-redis.json)、[demo-unity.json](evidence/demo-unity.json)。

## 工具验证与交付包

项目虚拟环境 Python 单测90/90通过，包含新增部署保护、历史数据兼容、包隔离与证据脱敏测试；Windows 现有 Release 构建对应的 `python_unit_tests` CTest 入口1/1通过。原协议、Bot、FastAPI及报告单测保留。当前没有 C++ 变更，没有重新编译或运行全量 CTest/ASan/UBSan；此前功能构建记录见 [证据索引](evidence-index.md)。

源码和完整 Unity player 的 ZIP 交付入口均执行检查，逐文件 SHA256 一致，路径限制、固定 ZIP 条目时间、私有目录与旧 Git 历史隔离正常。正式包在提交完成后生成，包内 manifest 记录实际源码提交；文件清单与公开证据见 [发布说明](publication-guide.md) 和 [manifest](evidence/manifest.json)。

P7.1 演示入口、P7.2 架构与证据、P7.3 技术说明已经收口。公开代码与材料可按发布说明交付。
