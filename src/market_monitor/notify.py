from datetime import datetime
import logging
import os
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .detection import Signal

LOG = logging.getLogger(__name__)


def _fmt(value, pattern, missing="—"):
    return missing if value is None else pattern.format(value)


def session_text(signal: Signal) -> str:
    names = {"RTH": "正式盘", "pre": "盘前", "post": "盘后", "night": "夜盘",
             "weekend": "周末", "holiday": "假日休市"}
    return names.get(signal.session, "未知时段")


def displacement_text(signal: Signal) -> str:
    """升级提醒要显示扩大了多少：2.2→4.7×ATR，而不是只给现值。"""
    if signal.kind == "escalation" and signal.escalation_from:
        return f"{signal.escalation_from:.1f}→{signal.atr_units:.1f}×ATR"
    return f"{signal.atr_units:.1f}×ATR"


def breadth_text(signal: Signal) -> str:
    """广度只记录不筛选；没有跨标的统计时明确说明，避免被读成 0。"""
    if not signal.breadth_total:
        return "未统计（单标的运行）"
    return f"{signal.breadth}/{signal.breadth_total} 只同向"


def format_message(signal: Signal, timezone="Asia/Shanghai"):
    direction = "上涨" if signal.direction == "up" else "下跌"
    kind = "快速" if signal.kind == "fast" else "持续单边"
    icon = "🚀" if signal.direction == "up" else "📉"
    timestamp = datetime.fromtimestamp(signal.detected_ms / 1000, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    source = "已完成 K 线（趋势确认）" if signal.source == "closed" else "当前正在形成的 K 线（提前提醒，未收盘）"
    changes = "\n".join(f"最近 {m} 分钟：{signal.changes[m]:+.2f}%" for m in (15, 30, 60))
    return (f"{icon} {signal.symbol} {kind}{direction}异动\n\n"
            f"当前价格：{signal.price:.10g}\n{changes}\n"
            f"单边效率 ER：{signal.er:.2f}（参考值）\nATR 标准化位移：{signal.atr_units:.2f}\n"
            f"量能：{_fmt(signal.vol_ratio, '{:.1f}×')}（{session_text(signal)}同会话基准）"
            f"　波动率扩张：{_fmt(signal.atr_expansion, '{:.2f}×')}"
            f"　主动买占比：{_fmt(None if signal.taker_buy_ratio is None else signal.taker_buy_ratio * 100, '{:.1f}%')}\n"
            f"同向共振：{breadth_text(signal)}　持仓变化：{_fmt(signal.oi_change, '{:+.2f}%')}"
            f"　资金费率：{_fmt(signal.funding_rate, '{:+.4f}%')}\n"
            f"ADX：{signal.adx:.1f}（辅助）\n状态：{kind}{direction}\n"
            f"信号来源：{source}\n涨跌幅基准：1m 历史收盘价，时间误差 < 60 秒\n"
            f"检测时间：{timestamp}\n数据来源：Binance Futures\n"
            f"https://www.binance.com/en/futures/{signal.symbol}")


def format_chat_alert(signal: Signal, timezone="Asia/Shanghai"):
    """One titled block per alert, same shape as the Crypto EMA Watch push."""
    direction = "上涨" if signal.direction == "up" else "下跌"
    kind = "快速" if signal.kind == "fast" else "持续单边"
    escalation = signal.kind == "escalation"
    icon = "🚀" if escalation else "📈" if signal.direction == "up" else "📉"
    title = (f"**{signal.symbol} 异动升级（{direction}）**" if escalation
             else f"**{signal.symbol}**")
    stamp = datetime.fromtimestamp(signal.detected_ms / 1000, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    formed = "已完成K线" if signal.source == "closed" else "未收盘提前"
    if escalation:
        kind = "升级"
    return (f"{icon} {title}\n\n"
            f"💰 当前价：{signal.price:.10g}\n"
            f"📊 涨跌幅：15m {signal.changes[15]:+.2f}%｜30m {signal.changes[30]:+.2f}%｜60m {signal.changes[60]:+.2f}%\n"
            f"📈 单边效率：ER {signal.er:.2f}｜位移 {displacement_text(signal)}\n"
            f"📦 量能：{_fmt(signal.vol_ratio, '{:.1f}×')}（{session_text(signal)}同会话）"
            f"｜扩张 {_fmt(signal.atr_expansion, '{:.2f}×')}"
            f"｜主动买 {_fmt(None if signal.taker_buy_ratio is None else signal.taker_buy_ratio * 100, '{:.1f}%')}\n"
            f"🧭 ADX：{signal.adx:.1f}（辅助）｜资金费率 {_fmt(signal.funding_rate, '{:+.4f}%')}\n"
            f"🧩 共振：{breadth_text(signal)}｜持仓变化 {_fmt(signal.oi_change, '{:+.2f}%')}\n"
            f"⏱️ 类型：{kind}{direction} · {formed}\n"
            f"🕐 提醒时间：{stamp}")


def format_sector_alert(signals, timezone="Asia/Shanghai"):
    """同一轮里同向的多只标的合成一条：一次板块级波动只占一条消息。"""
    direction = signals[0].direction
    side = "上涨" if direction == "up" else "下跌"
    stamp = datetime.fromtimestamp(max(s.detected_ms for s in signals) / 1000,
                                   ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    formed = "已完成K线" if all(s.source == "closed" for s in signals) else "含盘中提前信号"
    avg15 = sum(s.changes[15] for s in signals) / len(signals)
    avg60 = sum(s.changes[60] for s in signals) / len(signals)
    # 广度要取成员里的最大值：逐个扫描时先扫到的标的只看到上一轮快照，不能拿第一条代表全体。
    breadth = max(s.breadth for s in signals)
    total = max(s.breadth_total for s in signals)
    upgrading = all(s.kind == "escalation" for s in signals)
    lines = [f"🧩 **板块级{side}{'升级' if upgrading else '异动'}（{len(signals)} 只同向）**",
             "",
             f"⏱️ {stamp} · {formed} · {session_text(signals[0])}",
             f"📊 板块均值：15m {avg15:+.2f}%｜60m {avg60:+.2f}%｜同向 {breadth}/{total} 只",
             ""]
    for signal in sorted(signals, key=lambda s: -abs(s.changes[15])):
        parts = [f"15m {signal.changes[15]:+.2f}%", f"60m {signal.changes[60]:+.2f}%",
                 displacement_text(signal)]
        if signal.vol_ratio is not None:
            parts.append(f"量 {signal.vol_ratio:.1f}×")
        if signal.taker_buy_ratio is not None:
            parts.append(f"买 {signal.taker_buy_ratio * 100:.0f}%")
        lines.append(f"- **{signal.symbol}** {signal.price:.6g}｜" + "｜".join(parts))
    oi = [s.oi_change for s in signals if s.oi_change is not None]
    funding = [s.funding_rate for s in signals if s.funding_rate is not None]
    if oi or funding:
        tail = []
        if oi:
            tail.append(f"持仓变化均值 {sum(oi) / len(oi):+.2f}%")
        if funding:
            tail.append(f"资金费率均值 {sum(funding) / len(funding):+.4f}%")
        lines += ["", "🧭 " + "｜".join(tail)]
    return "\n".join(lines)


def format_alert_batch(signals, timezone="Asia/Shanghai"):
    """Alerts from one scan, separated the way the EMA Watch batches them."""
    return "\n\n---\n\n".join(format_chat_alert(s, timezone) for s in signals)


class Notifier:
    def __init__(self, config: Config, client=None):
        self.config = config
        self.client = client or httpx.Client(timeout=10)
        self.token = os.getenv(config.telegram.token_env, "")
        self.chat = os.getenv(config.telegram.chat_id_env, "")
        if config.telegram.enabled and not (self.token and self.chat):
            raise ValueError("Telegram enabled but token/chat environment variables are missing")

    def close(self):
        self.client.close()

    def send(self, signal):
        return self.send_text(format_message(signal, self.config.timezone))

    def send_batch(self, signals):
        """一轮多条时的推送：板块级合并文本，或退化成逐条拼装。"""
        LOG.info("合并推送：%s 只同向，阈值 %s", len(signals), self.config.merge_min_symbols)
        text = (format_sector_alert(signals, self.config.timezone)
                if len(signals) >= max(self.config.merge_min_symbols, 2)
                else format_alert_batch(signals, self.config.timezone))
        return self.send_text(text)

    def send_text(self, message):
        print(message, flush=True)
        if not self.config.telegram.enabled:
            return "console"
        # No automatic retries: a timed-out POST may already have delivered the message.
        try:
            response = self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                        json={"chat_id": self.chat, "text": message, "disable_web_page_preview": True})
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise ValueError("Telegram rejected message")
        except (httpx.HTTPError, ValueError):
            # HTTP exceptions contain the secret URL: never log them or chain them.
            raise RuntimeError("Telegram delivery failed or uncertain; no automatic resend") from None
        return "sent"
