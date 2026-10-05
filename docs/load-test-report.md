# 无在线恢复的规模基线

目标：127.0.0.1:9000，每个规模每个 Bot 1 局。

本报告保留关闭在线恢复的历史实测。完整恢复开启样本和公开数据入口见
[recovery-load-report.md](recovery-load-report.md) 与 [evidence-index.md](evidence-index.md)。

口径更正：下表历史吞吐是 `completed_rounds/duration`，即玩家完成轮次/s，两个玩家对应一局；26.991/33.559/37.416并非实际对局/s。Docker内存统计也不是Arena进程RSS。原始数值保留；当前同版本恢复开关对照与进程资源见 [recovery-load-report.md](recovery-load-report.md)。

| Bot | 房间 | 成功率 | 吞吐（玩家轮次/s） | ActionAck P50/P95/P99 ms | 完整流程 P50/P95/P99 ms | Docker CPU 峰值 | Docker容器内存峰值 | 结果 |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 20 | 10 | 1.0 | 26.991 | 1.642/2.625/3.687 | 514.713/716.608/716.707 | 18.27% | 414502092 bytes | 通过 |
| 100 | 50 | 1.0 | 33.559 | 3.9/7.685/9.599 | 2275.236/2919.918/2954.544 | 52.14% | 414816665 bytes | 通过 |
| 200 | 100 | 1.0 | 37.416 | 11.022/29.224/32.812 | 4432.796/5236.354/5296.438 | 56.34% | 415026380 bytes | 通过 |

资源采集容器：arena-cards-server, arena-cards-mysql, arena-cards-redis
瓶颈判断应结合服务端日志、MySQL/Redis 指标和这张表；该脚本没有把压测数据解释成生产容量承诺。
