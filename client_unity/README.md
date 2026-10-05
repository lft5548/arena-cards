# Arena Cards Unity 客户端

Unity 2022.3 LTS / C# 的 UGUI 展示客户端，支持登录、匹配、手牌、双方生命/能量/护盾/状态、倒计时、事件反馈、结算、原 token 重连和真实排行榜。规则、费用、伤害、随机数和胜负由 C++17 服务端权威计算。

## 从源码创建可运行项目

仓库提交 `Assets` 源码，Unity 的 `ProjectSettings`、`Packages`、`Library` 和导出程序由本地项目生成；本目录不是可直接通过 Hub 打开的完整 Unity 项目。

1. 在仓库根目录执行 `python tools/protocol/install_unity_protobuf.py`，安装固定版本的官方 NuGet runtime：`Google.Protobuf 3.21.12` 和 `System.Runtime.CompilerServices.Unsafe 4.5.2`。生成的 DLL 在 `client_unity/Assets/Plugins/Protobuf`，不提交 Git。
2. 使用 Unity Hub 创建 **Unity 2022.3 LTS / 3D (Built-In Render Pipeline)** 项目，放在仓库内忽略的 `build-unity-qa` 或自己选择的本地目录。
3. 将本目录 `Assets` 下的 `Scripts`、`Editor`、`Plugins` 全部复制到新项目的 `Assets`。ProtoV1 的 C# 生成代码已经包含在 `Scripts/Generated`，无需重新生成。
4. Package Manager 确认安装 `Unity UI`（`com.unity.ugui`）。Player Settings 的 API Compatibility Level 使用 `.NET Standard 2.1`，Active Input Handling 使用 `Input Manager (Old)` 或 `Both`，以配合 UGUI 的输入模块。
5. 等待编译完成，在菜单选择 `Arena Cards/Create Demo Scene`，生成 `Assets/ArenaDemo.unity`。生成器创建 Camera、Canvas 和 EventSystem；已有旧场景先保存，再重新生成。
6. Build Settings 添加 `Assets/ArenaDemo.unity`，选择 Windows / x86_64。Player Settings 使用 Mono 后端，然后 Build 到独立 player 目录，例如 `Builds/ArenaCardsDemo`。

运行需要复制整个 player 目录，包含 `.exe`、对应 `_Data`、`UnityPlayer.dll` 和其他 Unity 导出文件；只复制 `.exe` 不能运行。另一台机器只需启动已有 player，服务端地址在连接页输入。

## 双客户端联机

按 [演示指南](../docs/demo-guide.md) 启动服务端并通过 `demo.py check`。两个 Unity 程序，或 Editor 加一份程序，连接 `127.0.0.1:9000`，输入不同用户名并点击 `Connect`、`Find opponent`。

连接页选择 TextV1 或 ProtoV1；不同协议客户端可以进入同一房间。轮到自己时点击卡牌或 `End Turn`，成功出牌也会推进回合。终局、等待确认、非己方回合及倒计时结束时禁用操作。战斗日志与伤害/治疗/护盾反馈来自服务端事件，客户端不自行计算权威数值。

结算后自动更新排行榜；`Leaderboard` 支持真实加载、空榜、错误和刷新。排行榜由游戏服务端查询 MySQL，按积分降序、player_id 升序返回，Unity 不连接 MySQL、Redis 或 Admin HTTP 服务。

断线后使用原 token 重连；不要重新登录来替代身份恢复。token 不进入 UI 战斗日志。完整停服续局和 Redis 补偿步骤见 [演示指南](../docs/demo-guide.md)。

## 协议与线程边界

两种协议共用长度/类型外层：

```text
uint32_be body_length
uint16_be message_type
payload
```

TextV1 payload 为 UTF-8 字段或原始用户名/token；ProtoV1 使用生成的 Google.Protobuf 类型与独立消息 ID，先通过 TextV1 Hello 选择协议，二进制正文不会交给 TextV1 解析。出牌/结束回合包含 match_id、turn_id、action_id，重复动作由服务端返回原 ActionAck。

- `ArenaClient` 的后台网络线程读取拆包，通过队列在 Unity `Update` 派发消息，避免从后台线程调用 Unity API。
- `ArenaDemoState` 解析服务端状态；`ArenaPresentationView` 绘制 UGUI，卡面元数据只用于显示和提示。
- 生命条、护盾条、回合数字和进度条显示服务端值；倒计时以收到的 remaining_ms 为依据。
- `ArenaDemoController` 提供连接、匹配、出牌、结束回合、重连和排行榜调用，可供替换 UI 复用。
- `TcpClient.Connected` 只作连接提示，真实连接状态来自读写结果与消息处理。

## 构建与有限联机验证

`ArenaPresentationVerification.Verify` 在独立 Unity 项目生成登录/战斗/三类反馈/排行榜布局/结算截图、`.unitypackage` 和 Windows Mono player。布局预览是固定 UI 输入，不能作为真实联机证据。

普通 player 启动不运行自动验证。可显式使用：

```text
ArenaCardsDemo.exe --arena-acceptance --arena-protocol text_v1 --arena-port 9000 --arena-output build-unity-live-text
ArenaCardsDemo.exe --arena-acceptance --arena-protocol proto_v1 --arena-port 9000 --arena-output build-unity-live-proto
```

在仓库根目录启动 player 并为每次执行选择新证据目录。该入口在实际 Unity 生命周期中创建两个客户端连接，验证登录、匹配、伤害/治疗/护盾、状态更新、原 token 重连、终局和 MySQL 排行榜。退出码零且生成本次 `passed.txt` 才判成功；失败会生成 `failed.txt`。使用屏幕渲染的正常 player，不以无图形模式替代 UI 验证。

验收会创建随机玩家和真实结算记录。`passed.txt` 与 player 日志保留在本地证据目录；公开交付使用经过检查的结果摘要和截图，不打包原始日志。源码包与可选 player 包入口见 [演示指南](../docs/demo-guide.md)。
