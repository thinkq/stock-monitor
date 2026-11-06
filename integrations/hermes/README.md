# Hermes 部署

推荐用 Hermes 的 no-agent 定时脚本运行；每次扫描不调用模型，也不直接发送 Telegram。
要求 Linux/macOS、Python 3.11+。Hermes 负责调度、渠道凭证、投递与任务错误通知。

## 安装

在目标主机项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/market-monitor verify --config config.toml
.venv/bin/market-monitor-hermes --config config.toml
```

修改 config.toml 中标的、阈值和数据库路径。新环境网络正常时设置
`dns_over_https = false`；原有 DoH 是可选的网络兼容处理。
Hermes 入口忽略 `[telegram]`，不需要项目的 Telegram 环境变量。
相对数据库路径按配置文件所在目录解析；独立 CLI 仍按原工作目录解析。
不要同时运行旧常驻监控与 Hermes 任务。

## 注册任务

将 market-monitor.sh 中 MONITOR_ROOT 改为实际项目绝对路径。
复制脚本本身到 Hermes 的 scripts 目录（不要使用指向项目外部的符号链接）：

```bash
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/scripts"
cp integrations/hermes/market-monitor.sh "${HERMES_HOME:-$HOME/.hermes}/scripts/market-monitor.sh"
hermes cron create "every 1m" --no-agent --script market-monitor.sh --deliver feishu --name market-monitor
```

多渠道路由用逗号列表（如 `--deliver telegram,feishu`，需先为每个渠道配置 home chat），
每个渠道各自投递；不要为同一个数据库再建第二个任务——文件锁会让后到的那次永久静默跳过。
先在 Hermes 中配置所需投递渠道与目标聊天。根据本机版本使用 `hermes cron create --help`
确认投递目标参数。无需将依赖安装进 Hermes 自身的 Python 环境。
默认 scan_seconds=60；更改任务周期时同步更改此值，否则快速信号的相邻确认可能过期。
目标主机需要持续运行 Hermes 调度服务；运行超时应覆盖整轮串行网络请求的耗时。

## 输出和状态

- 新信号：stdout 输出中文通知（每条一个带标题的区块，与 Crypto EMA Watch 推送同款排版），
  由 Hermes 投递；同轮多标的用 `---` 分隔。
- 无信号、确认等待、冷却、额度抑制：stdout 为空，静默运行。
- 日志：stderr。整轮扫描失败或输出失败返回 1，由 Hermes 报任务错误；
  部分标的失败仍返回 0（stderr 记录 `partial scan`），因为 Hermes 在非零退出时会丢弃 stdout，
  已产出的真告警不能跟着丢。整轮没有任何标的成功才返回 1。
- 标的校验结果缓存在 SQLite 中 24 小时，避免每分钟重复拉取约 1.2 MB 的 exchangeInfo；
  任何一次扫描不完整都会清掉缓存，下一轮重新校验，所以下架标的不会被静默掩盖。
- 重叠扫描：同一数据库使用非阻塞文件锁，后到任务静默跳过（stderr 有 `tick skipped`）。
- `emitted`：已写入脚本 stdout，不代表 Hermes 已投递成功。
- `failed`：输出失败或结果不确定；`reserved`：预约后未确认输出。

继续采用至多一次输出尝试，避免重复报警。输出后崩溃、Hermes 投递失败、
部分扫描失败或脚本超时时，可能漏达；不自动补发。部分失败前已输出的信号也可能
被 Hermes 的错误结果覆盖，数据库不会因此重新报警。渠道送达状态在 Hermes 查看。
每次任务重新获取行情，不持久化行情缓存；这换取简单部署，代价是更多历史数据传输。
策略状态保存在 SQLite，不依赖 LLM 记忆。

仍保留独立 CLI，便于本地验证与脱离 Hermes 排障；Hermes 运行路径不启用常驻循环和 Telegram 客户端。

## 聊天里管理

统一入口是 `mm`（`market-monitor` 同等可用）。以下命令都不发网络告警，只读本地状态或改写
`config.local.toml`：

```bash
mm                  # 状态总览
mm status           # 最近一轮扫描、今日告警、最近告警、配置改动
mm list             # 生效配置 + 覆盖来源
mm add NVDA         # 校验存在/TRADING/历史足够后写入
mm del APP          # 移除标的并清理其 episode/pending
mm check CRWV       # 只查不改，看成交额与粗估触发密度
mm reset [key]      # 撤销本地覆盖
mm audit            # 改动审计
mm prune [天数]     # 清理旧告警，默认保留 180 天
mm help             # 命令清单
```

Hermes 侧的快捷命令：`/mm`（状态）、`/mm_list`（生效配置）、`/mm_help`（命令清单）。
带参数的改动由对话交给 agent 调用 `mm add ...`，因为 Hermes 的 `quick_commands` exec 类型不接收参数。

参考：https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
（no-agent：空输出静默，非零退出或超时触发错误通知，脚本须位于 HERMES_HOME/scripts）。
