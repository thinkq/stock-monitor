from datetime import datetime
import os
from zoneinfo import ZoneInfo

import httpx

from .config import Config
from .detection import Signal


def format_message(signal: Signal, timezone="Asia/Shanghai"):
    direction = "上涨" if signal.direction == "up" else "下跌"
    kind = "快速" if signal.kind == "fast" else "持续单边"
    icon = "🚀" if signal.direction == "up" else "📉"
    timestamp = datetime.fromtimestamp(signal.detected_ms / 1000, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    source = "已完成 K 线（趋势确认）" if signal.source == "closed" else "当前正在形成的 K 线（提前提醒，未收盘）"
    changes = "\n".join(f"最近 {m} 分钟：{signal.changes[m]:+.2f}%" for m in (15, 30, 45, 60))
    return (f"{icon} {signal.symbol} {kind}{direction}异动\n\n"
            f"当前价格：{signal.price:.10g}\n{changes}\n"
            f"单边效率 ER：{signal.er:.2f}\nATR 标准化位移：{signal.atr_units:.2f}\n"
            f"ADX：{signal.adx:.1f}（辅助）\n状态：{kind}{direction}\n"
            f"信号来源：{source}\n涨跌幅基准：1m 历史收盘价，时间误差 < 60 秒\n"
            f"检测时间：{timestamp}\n数据来源：Binance Futures\n"
            f"https://www.binance.com/en/futures/{signal.symbol}")


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
