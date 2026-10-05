# 部署、联机演示与源码交付

本地演示由 C++17 服务端、MySQL、Redis 和两个 Unity 客户端组成。服务端权威执行规则，Unity 只展示收到的状态；Python Bot 用于自动化验证。

## 运行条件

- Docker Engine 与 Compose v2，能够运行 Linux 容器。
- Python 3.10 或以上；ProtoV1 检查需要 `tools/protocol/requirements.txt` 中的依赖。
- Arena TCP `9000`、MySQL `3307`、Redis `6379` 端口可用。
- Unity 2022.3 LTS，或已导出的 Windows 客户端；源码导入和构建见 [Unity 客户端](../client_unity/README.md)。

以下命令在仓库根目录执行。Windows 可将 `python` 替换为 `.venv\Scripts\python.exe`，Linux 可替换为 `.venv/bin/python`。

```sh
python -m venv .venv
```

激活虚拟环境：Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`，Linux 使用 `source .venv/bin/activate`。随后执行：

```sh
python -m pip install -r tools/protocol/requirements.txt
python tools/demo/demo.py start
python tools/demo/demo.py check
```

`start` 默认构建当前服务端源码并启动 Compose 的三个服务。已有匹配镜像时使用 `start --no-build`。镜像下载受限的环境可在运行前配置 `ARENA_IMAGE_REGISTRY` 和 `ARENA_BASE_IMAGE`；入口保留这两个覆盖项。

入口固定以下演示配置，覆盖同名继承环境变量：

| 项目 | 值 |
| --- | --- |
| 在线恢复 / 启动清理 running | `1` / `0` |
| 恢复模式 / 快照间隔 | `full` / `16` |
| MySQL 会话池 / 检查点 batch | `2` / `16` |
| Redis 人工失败注入 | `0` |

入口不删除容器、命名卷或数据库数据。启动前读取真实 MySQL 的 running 和半结算状态，已有运行对局时拒绝重建；构建后再次检查，数据库检查失败也拒绝继续。请在本地演示窗口停止客户端匹配操作后运行 `start`，检查与服务重建之间并不是跨进程原子维护锁。

`check` 检查三服务健康、端口映射、上述运行配置、SQL running/pending/半结算计数以及 TextV1/ProtoV1 Admin 请求。running 或 pending 大于零会如实显示，它们不等同于部署失败；终局缺少结果或非终局出现结果/Outbox 记录属于半结算异常，会返回失败。历史已结算记录可能早于 Outbox 表引入，不以缺少 Outbox 行判定历史事务错误；本次新对局的结果、Outbox 和缓存由定向联机核验确认。命令成功表示部署和探针就绪，完整联机行为通过下面的演示及验证工具核对。输出只包含允许公开的状态字段，不打印数据库凭据、token 或本机绝对路径。

## 两个客户端完成一局

1. 启动两份 Unity Windows 程序，或一份 Editor 与一份程序，输入不同用户名。
2. 连接 `127.0.0.1:9000`，两个客户端可分别选择 TextV1、ProtoV1。
3. 两端点击 `Find opponent`，确认 MatchId 相同、双方手牌和当前回合显示正常。
4. 出牌和结束回合，观察双方生命、能量、护盾、状态、回合计时和战斗日志。成功出牌会推进回合。
5. 完成对局，确认两端收到结算；点击 `Leaderboard`，确认来源为 MySQL 的真实积分、胜负记录。
6. 再运行 `python tools/demo/demo.py check`，确认没有遗留 running 对局、pending Outbox 或半结算记录。

普通客户端启动不运行自动测试。已有的 opt-in 验证入口 `--arena-acceptance --arena-protocol text_v1` 或 `proto_v1` 在真实 Unity 生命周期中创建两个网络客户端，完成登录、匹配、伤害/治疗/护盾、原 token 重连、结算和排行榜检查。`--arena-output` 可指定仓库内被忽略的证据目录；程序退出码为零且生成 `passed.txt` 才表示成功。这个入口验证两名真实客户端，但不等同于人工操作的两个独立窗口。

## 关服续局与 Redis 补偿

续局演示在两名客户端已收到动作 ACK 后停止并重启同一个服务实例；保留原 token，重连后确认状态和已确认动作不回退。恢复保留绝对期限，停机时间仍计入回合/重连期限，需在期限内完成。服务停止时应先停止继续提交操作，正常使用 `docker compose -f deploy/docker-compose.yml stop arena-server`，再 `start arena-server`。此步骤按原配置启动已有容器，不能使用会拒绝运行对局的演示 `start` 入口。

Redis 补偿说明展示的是：MySQL 提交后结果先交付，Redis 暂时不可达时保留 pending Outbox，链路恢复后自动 applied，Lua 回复丢失后的重试不重复加分。为了避免影响已有缓存和其他演示，不停止共享 Redis；使用 [Redis 可靠性验证](redis-reliability.md) 的隔离服务、随机测试身份与 `tools/bot/redis_fault_proxy.py` 代理。

自动彩排复用现有隔离入口：

| 演示边界 | 入口 | 操作范围 |
| --- | --- | --- |
| 原 token 在线续局 | `tests/room_recovery_integration_test.py` | 私有服务进程/随机身份；可指定专用数据库与端口 |
| Redis 黑洞、恢复补偿、Lua 回复丢失 | `tests/redis_reliability_test.py` | 私有代理/服务、随机身份；共享 Redis 持续运行 |
| 双协议 SQL/Redis/Outbox | `tests/mysql_settlement_test.py --existing-server` | 对现有实例创建本次随机身份并清理 |

真实存储验证使用带 MySQL Connector 的 Linux 构建；Windows 无 Connector 的构建不能代替这一部分。[证据索引](evidence-index.md) 提供验证结果与可公开的样本入口。

## 创建无 Git 历史的源码包

```sh
python tools/demo/demo.py package --output build-delivery/ArenaCards-source.zip
```

打包只读取 Git index 中的源文件；新增文件需先 `git add`，已经删除的文件跳过。`.git`、`.local`、虚拟环境、构建目录、日志、回放和 Unity 生成缓存不进入源码包。ZIP 条目使用固定时间，manifest 记录源码 revision、工作区 `clean/dirty` 状态、相对文件名、字节数和 SHA-256；原始 Git 历史不包含在内。正式发布应在提交后打包并确认 `source_state=clean`；dirty 包的 revision 只表示基线提交，包内每个文件以其 SHA-256 为准。

可选附带一个已导出的 Unity Windows player 目录：

```sh
python tools/demo/demo.py package --output build-delivery/ArenaCards-demo.zip --unity-build build-unity-qa/Builds/ArenaCardsDemo
```

该目录必须位于仓库内、受 `.gitignore` 隔离，并包含 player 可执行文件。只添加明确选择的目录，日志、调试符号、Unity backup 和验收目录仍排除。产物保留在忽略目录；上传 Release 附件前核对 manifest。入口不执行 push、发布或改写 Git 历史。
