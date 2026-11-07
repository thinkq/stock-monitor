from datetime import datetime
import os
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .detection import Signal


def _fmt(value, pattern, missing="—"):
    return missing if value is None else pattern.format(value)


def session_text(signal: Signal) -> str:
    names = {"RTH": "正式盘", "pre": "盘前", "post": "盘后", "night": "夜盘", "weekend": "周末"}
    return names.get(signal.session, "未知时段")


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
    changes = "\n".join(f"最近 {m} 分钟：{signal.changes[m]:+.2f}%" for m in (15, 30, 45, 60))
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
    icon = "📈" if signal.direction == "up" else "📉"
    stamp = datetime.fromtimestamp(signal.detected_ms / 1000, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")
    formed = "已完成K线" if signal.source == "closed" else "未收盘提前"
    return (f"{icon} **{signal.symbol}**\n\n"
            f"💰 当前价：{signal.price:.10g}\n"
            f"📊 涨跌幅：15m {signal.changes[15]:+.2f}%｜30m {signal.changes[30]:+.2f}%｜60m {signal.changes[60]:+.2f}%\n"
            f"📈 单边效率：ER {signal.er:.2f}｜位移 {signal.atr_units:.2f}×ATR\n"
            f"📦 量能：{_fmt(signal.vol_ratio, '{:.1f}×')}（{session_text(signal)}同会话）"
            f"｜扩张 {_fmt(signal.atr_expansion, '{:.2f}×')}"
            f"｜主动买 {_fmt(None if signal.taker_buy_ratio is None else signal.taker_buy_ratio * 100, '{:.1f}%')}\n"
            f"🧭 ADX：{signal.adx:.1f}（辅助）｜资金费率 {_fmt(signal.funding_rate, '{:+.4f}%')}\n"
            f"🧩 共振：{breadth_text(signal)}｜持仓变化 {_fmt(signal.oi_change, '{:+.2f}%')}\n"
            f"⏱️ 类型：{kind}{direction} · {formed}\n"
            f"🕐 提醒时间：{stamp}")


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
        message = format_message(signal, self.config.timezone)
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
