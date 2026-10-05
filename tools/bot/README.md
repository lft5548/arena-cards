# Bot 与可重复测量

```powershell
python tools/bot/bot.py --count 20 --host 127.0.0.1 --port 9000
python tools/bot/bot.py --count 2 --rounds 3 --protocol proto_v1
python tools/bot/bot.py --count 2 --rounds 3 --protocol mixed
```

count 必须是正偶数，每两个 Bot 配对。Bot 等待 LoginResp，匹配后只在自己的回合提交动作，
优先选择可支付卡牌，否则 EndTurn。输出单个 JSON，超时、不完整对局或服务错误返回非零。
协议可选择 text_v1、proto_v1 或 mixed；二进制连接先协商，不静默降级。

bot.py 用于正常对局回归，不自动编排故障。断线、原 token 重连、原 ACK 重试和存储故障由
recovery_load_test.py 与 tests/ 中对应隔离工具验证，服务端支持与工具能力分别说明。

## 延迟与吞吐

```powershell
python tools/bot/load_test.py --host 127.0.0.1 --port 9000 `
  --count 20 --rounds 3 --timeout 45 --protocol text_v1 `
  --json-out build-load/report.json
```

load_test.py 使用 asyncio 真实连接，统计 login、matchmaking、action_ack 与完整流程
P50/P95/P99。completed_rounds 是玩家完成轮次，两轮次对应一局；
不能直接把该字段除以持续时间称为实际对局/s。存储验证工具按 MySQL 对局数报告真实吞吐。

## 规模与恢复

- scale_load_test.py：编排 20/100/200 Bot 与 Docker 容器资源采集。
- recovery_load_test.py：临时 schema、完整检查点开销、真实行锁、慢消费、原 ACK 重连与 Redis 故障。
- recovery_compare.py：full/tail 配对、固定 seed/轨迹、20/100/200 Bot、三次重复及独立恢复测量。
- redis_fault_proxy.py：私有连接拒绝、真实 Lua 后丢回复与网络黑洞。
- mysql_commit_proxy.py：测试连接 COMMIT OK 丢失，用于验证提交结果不确定。

私有测试定向清理自身数据，保留共享服务与命名卷。性能运行应与编译/CTest 分开，
明确 recovery、batch、pool、规则、二进制与资源采样方式；sanitizer 不能作为容量样本。

完整命令、公开样本和边界见 [演示指南](../../docs/demo-guide.md)、
[证据索引](../../docs/evidence-index.md)、[恢复负载](../../docs/recovery-load-report.md)
与 [策略对照](../../docs/recovery-strategy-report.md)。
