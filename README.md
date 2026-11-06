# Binance 多标的实时行情异动监控

Python 3.11+ 的独立行情报警程序。按配置扫描多个 USDⓈ-M Futures 标的，每 60 秒检查 15m K 线，发现已发生的明显上涨或下跌。包含控制台与 Telegram 通知；没有下单、预测、收益统计、交易回测或 AI 分析。

**网络修复：已定位本机系统 DNS 异常，项目提供应用内 DNS over HTTPS 解析，保留 Binance HTTPS 域名和证书校验。实际验证结果见 [VALIDATION.md](VALIDATION.md)。** 每次启动仍通过 exchangeInfo 检查全部标的。Telegram 未提供凭证，未进行真实消息投递。

## Hermes 运行（推荐）

项目支持交由 Hermes 定时扫描和投递，无信号时静默，不调用大模型。
使用 `market-monitor-hermes --config /绝对路径/config.toml`；部署步骤见
[integrations/hermes/README.md](integrations/hermes/README.md)。策略、行情校验及 SQLite 去重继续保留。

聊天里或终端里管理监控，不必动基础配置文件（`mm` 已装进 ~/.local/bin，任何目录可用）：

```bash
mm                  # 状态总览（同 mm status）
mm help             # 全部命令
mm list             # 生效配置，标出哪些来自本地覆盖
mm add NVDA         # 校验后加入，写入 config.local.toml 并记审计
mm del APP          # 移除，历史告警保留
mm check CRWV       # 只查：成交额、ATR、按现阈值大约多久触发一次
mm reset [key]      # 撤销本地覆盖
mm audit            # 改动记录
mm prune [天数]     # 清理旧告警，默认保留 180 天；删完会 VACUUM 收缩文件
```

改动写进 `config.local.toml`（已 gitignore），基础 `config.toml` 保持不动；每轮扫描重新读取，
不需要重启。检测阈值不开放给命令行改写，只能回测后再改。

## 安装与运行

```bash
cd /Users/zhaoqing/personal/stock-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

# 不依赖网络，不发送 Telegram：合成上涨、下跌、横盘、盘中位移、反转
mm demo

# 自动化测试
python -m pytest -q

# 第一步：验证真实接口、所有配置标的及指标，保存 CSV
mm verify

# 完成一次真实扫描；默认只在控制台报警
mm once

# 持续运行；Ctrl-C 退出
mm run
```

`mm` 与 `market-monitor` 是同一个命令（`mm` 已装到 `~/.local/bin`，任何目录都能用，配置文件位置固定）。

支持 `python -m market_monitor` 的同等命令。TA-Lib 的现代 Python wheel 通常自带 C 库；如果当前 Python／操作系统没有匹配 wheel，需要先按 [TA-Lib Python 安装说明](https://github.com/TA-Lib/ta-lib-python#installation) 安装底层 TA-Lib，再安装项目。无需 Binance API Key。本机系统 DNS 曾把 Binance 域名解析到异常地址，示例配置已开启 `dns_over_https = true`：通过 Google 的 HTTPS DNS 动态查询 A 记录，按 TTL 缓存并在连接失败时尝试其他返回地址；不写死 IP、不修改系统 DNS 或 hosts、不关闭 TLS 证书校验。首次 DNS／TLS 建连较慢时会重新采样服务器时间，仍要求采样耗时不超过 5 秒。

DoH 模式对 Binance 直接连接，不使用环境代理；如果需要 `HTTPS_PROXY`，将 `dns_over_https = false` 后设置标准代理环境变量。DoH 服务不可用时明确失败，不使用过期 DNS 记录。Telegram 保持原有 httpx 网络配置。

`verify` 成功后在 `data/verification/` 保存真实主周期和 1m K 线 CSV。任何标的无效、不可交易、行情陈旧或指标不可用都会明确失败，不会自动替换成测试网、现货或合成行情。`once` 只要有标的未成功检查就返回非零退出码。启动失败需处理原因后重新启动；运行中单个标的失败会跳过并在下次扫描重试。

## 检测规则

**所有默认阈值均为初始经验参数，未经最优性验证。** 它们描述已经发生的价格运动，不预测后续方向。

| 类型 | 默认条件 |
| --- | --- |
| 快速异动 | 当前主周期 K 线或最近 15 分钟位移 ≥ 1.5%，或最近 30 分钟 ≥ 2%；同时位移 ≥ 2 ATR、分钟路径 ER ≥ 0.65，并通过持续确认 |
| 持续单边 | 最近 2～4 根已完成 K 线至少两次同向收盘推进，最后一步仍同向；净位移 ≥ 2% 且 ≥ 2 ATR，ER ≥ 0.7 |
| 小回撤容忍 | 持续单边窗口中的单次反向变化不超过 0.35 ATR，当前价格也未明显反向回撤 |
| 结束／重置 | 最近两根完成 K 线的净变化 < 0.5%，且 ER < 0.35 或收盘价区间 < 0.5%；连续两根不同的完成 K 线满足才重置 |

上涨、下跌使用对称规则。当前 K 线发生显著快速反转时优先使用当前方向。快速信号的 ER 是相关分钟价格路径的效率，默认要求 ≥ 0.65，以过滤来回震荡；持续单边的 ER 使用 2～4 根主周期收盘价路径。ER = 净位移绝对值 / 逐步位移绝对值之和；TA-Lib 没有直接提供 ER，故仅此简单公式自行计算，不能用 KAMA 输出冒充 ER。

ATR、ADX、+DI、−DI 均直接使用 TA-Lib。ATR 基准排除最近四根已完成 K 线以及当前未收盘 K 线，防止异常运动抬高自己的比较基准。ADX／DI 只作辅助信息，不阻挡早期提醒。持续单边取已完成 K 线，快速信号允许使用当前快照；检测函数没有网络、系统时钟或持久化副作用，不读取未来 K 线。

额外缓存 65 根 1m K 线，用于计算最近 15／30／45／60 分钟涨跌幅，基准取目标时刻之前最近的分钟收盘价，时间误差小于一分钟。45／60 分钟变化用于通知上下文，主周期最近 2～4 根窗口用于连续性判断。修改主周期后，持续单边窗口随主周期改变，固定分钟涨跌幅窗口保持不变。

## 行情和异常处理

- 启动检查 `/fapi/v1/exchangeInfo` 中所有标的的 `TRADING` 状态，不假定任何股票挂钩合约存在。
- `/fapi/v1/klines` 首次取 200 根主周期历史 K 线及 65 根分钟线；以后从倒数第二根的开盘时间增量更新，按开盘时间去重，保留最新版本。
- 连续性、OHLC 合法性、有限数、周期对齐、历史长度都必须通过检查。缺口不插值、不前向填充；丢弃该缓存，下一扫描重新拉取历史。新上市历史不足的标的不报警。
- 每轮用 `/fapi/v1/time` 校准服务器时间并通过本地单调时钟推进，区分完成／形成中 K 线。必须包含服务器当前时间桶；边界尚未返回新 K 线时跳过该轮。
- 额外使用 `/fapi/v2/ticker/price` 的最近成交时间检查新鲜度，默认最大 180 秒；低流动性或休市标的可能被跳过。行情失败、数据滞后、指标未就绪均不更新报警状态，更不会用上次缓存补发。
- 超时、网络错误及 5xx 有两次指数退避重试。429／418 根据 `Retry-After` 设置整个客户端的暂停时间，停止该轮扫描，避免对剩余标的继续轰炸。正常情况下约 60 次行情请求／分钟，另加服务器时间请求，顺序执行。
- 扫描周期从每轮开始计时。网络耗时超过扫描周期时，实际间隔会拉长；60 秒是轮询目标，不是严格延迟承诺。瞬间冲高又回落可能在两次轮询之间被错过。

Binance 官方接口说明：[USDⓈ-M Futures Market Data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)。

## 去重与通知语义

快速信号默认需要在相邻有效扫描中持续满足条件至少 60 秒（`fast_confirmation_seconds = 60`）。默认每分钟扫描时，首次候选不通知，后续仍满足条件且累计达到 60 秒才预约通知；已完成 K 线的持续单边信号直接确认。候选消失、方向改变、行情请求失败或两次观察间隔超过扫描周期的 1.5 倍，会重置快速确认。确认前不占用每日限额，也不把该轮标记为已通知。等待状态存入 SQLite，短时间重启仍可延续确认，长时间离线后重新确认。设置为 0 可关闭这一步。

这会过滤瞬时触线，但快速提醒通常比旧版多等待一轮扫描；将扫描间隔改为 180／300 秒时，实际确认延迟也相应增加。ER 过滤也可能漏掉路径较曲折的真实异动。阈值仍是初始经验参数，需要结合后续报警记录观察，不能视为已验证的最优参数。

SQLite 以“标的 + 方向”保存一轮异动状态。同一轮只预约一次主通知，快速信号升级为持续单边仅更新状态。短暂低于触发阈值不会结束这一轮；必须等待上述震荡重置条件。两次重置观察必须来自连续的不同完成 K 线，重复扫描同一根 K 线不会累计次数，行情断档也不会当成震荡。

默认同方向冷却 3600 秒、全局每日最多 30 条，按 `Asia/Shanghai` 的自然日统计。被冷却或每日上限抑制的一轮会记录为 `suppressed` 并标记活跃，之后不把旧信号延迟补发。反向异动拥有独立状态，可以单独提醒。保留 SQLite 数据库才能在重启后继续去重；不要把多个进程指向不同数据库来监控同一组标的。

预约和状态更新使用 `BEGIN IMMEDIATE` 原子事务；**先持久化预约，再发通知**。Telegram 没有这里可使用的幂等发送键，因此采用至多一次发送尝试：网络超时或程序在预约后崩溃时不自动重发，以防重复。代价是可能漏达一次通知，不能保证“恰好送达一次”。控制台会先打印；SQLite 的 `reserved` 表示预约后未完成确认，`failed` 表示失败或结果不确定，`sent` 表示 Telegram 接口确认成功，`console` 表示仅控制台输出。失败尝试也占每日限额。数据库不会保存 Bot Token。

查看通知记录：

```bash
sqlite3 data/monitor.sqlite3 \
  'SELECT id,symbol,direction,kind,day,status,detail FROM alerts ORDER BY id DESC LIMIT 30;'
```

## Telegram

先创建自己的 Bot 并确保目标聊天允许 Bot 发送消息。在 `config.toml` 中设置 `[telegram] enabled = true`，凭证用环境变量提供：

```bash
export TELEGRAM_BOT_TOKEN='你的 Bot Token'
export TELEGRAM_CHAT_ID='你的 Chat ID'
market-monitor run
```

通知包含中文方向与类型、当前价格、15／30／45／60 分钟涨跌幅、ER、ATR 标准化位移、辅助 ADX、检测时间及 Binance 页面链接，并明确是已完成 K 线确认还是当前形成中的提前信号。HTTP 错误不会把含 Token 的 Telegram URL 写到日志。`demo` 永远不发送 Telegram。

## 配置与目录

`config.toml` 集中管理 symbols、interval、scan_seconds（如 60／180／300）、directions、cooldown_seconds、daily_limit、reset_bars、时区、新鲜度与指标阈值。`sensitivity` 越小越灵敏，同时缩放百分比与 ATR 触发阈值。周期支持 `1m/3m/5m/15m/30m/1h`。数据库路径相对于运行命令所在目录。

```text
src/market_monitor/
  config.py       TOML 配置和校验
  market.py       REST、限流、服务器时间、行情缓存与校验
  network.py      可选 DoH 动态解析、TTL 缓存和 TLS 域名保留
  detection.py    纯检测逻辑与 TA-Lib 指标
  state.py        SQLite 状态、限额、冷却和原子预约
  notify.py       中文消息与 Telegram
  app.py          verify / once / run 命令和扫描编排
  demo.py         可重复的合成行情演示
 tests/           算法、采集异常、状态、通知与管线测试
```

测试覆盖连续上涨／下跌、小回撤、普通震荡、小幅单边、极端波动、盘中快速异动、反转、缺口、NaN、过期／未来时间、指标预热、增量拉取、重复 K 线、429／418、网络异常、去重、升级、冷却、每日上限、重启、跨连接预约和 Telegram 模拟响应。这些是功能验证，不能代替真实行情上对覆盖率和通知密度的长期观察。
