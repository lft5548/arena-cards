# 源码与演示包发布

公开交付包含服务端、协议、Unity 源码、Python 客户端、工具、测试、部署文件和技术说明。公开入口以 [README](../README.md)、[演示指南](demo-guide.md)、[设计取舍](design-decisions.md) 和 [证据索引](evidence-index.md) 为主。

## 生成源码包

在已完成提交的仓库根目录执行：

```sh
python tools/demo/demo.py package --output build-delivery/ArenaCards-source.zip
```

输出目录受 Git 忽略。源码包包含 Git index 中仍存在的公开文件，不包含 `.git`、`.local`、虚拟环境、构建缓存、日志或原始回放。ZIP 条目采用固定时间；包内 manifest 列出源码版本、工作区状态及各文件内容哈希。正式发布使用 `source_state=clean` 的产物。

修改当前文档不会从已有 Git 提交中移除历史文案、路径或提交元数据。需要发布独立源码快照时，将上述 ZIP 解压到新的目录，在解压后的 `arena_cards` 根目录建立新仓库：

```sh
git init -b main
git add .
git commit -m "Initial source release"
```

随后按托管平台的普通流程连接目标仓库并推送。此方式保留全部当前公开源文件，原开发仓库及其历史仍留在本地；不执行历史重写或强制推送。新仓库正常记录本次发布提交的作者与时间，ZIP 的固定时间不改变 Git 提交规则。

## 客户端附件

Unity 源码采用 `Assets` 导入方式，完整操作见 [客户端说明](../client_unity/README.md)。如果要附带已经构建的 Windows 客户端，明确选择完整 player 目录：

```sh
python tools/demo/demo.py package --output build-delivery/ArenaCards-demo.zip --unity-build build-unity-qa/Builds/ArenaCardsDemo
```

附件包含可执行文件、数据目录和运行依赖，过滤日志、调试符号、备份及验收目录。源码仓库保留客户端构建方式，二进制可作为 Release 附件；不要单独只复制 `.exe`。

## 技术证据

公开 JSON 是脱敏发布副本：保留成功/失败标志、配置、测量值和二进制哈希，去除日历信息、本机路径、测试身份及原始日志。相对耗时、回合期限和性能分位数属于技术数据，保留其真实数值。

已发布样本的原始摘要哈希与公开副本哈希分别记录在 [manifest](evidence/manifest.json) 中；哈希可验证内容绑定，不能单独证明测试执行过程。没有原始文件的第三方可以查看公开指标，并运行索引中的复现工具自行验证。

新增实验先写入被忽略的本地证据目录，再通过 `tools/build_support/publish_evidence.py` 导出公开副本。开发环境依赖、版本与样本局限仍按技术事实说明，不把文档整理扩大为生产容量保证。
