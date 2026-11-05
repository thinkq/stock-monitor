from dataclasses import replace

import httpx
import pytest

from market_monitor.config import Config, Telegram
from market_monitor.demo import scenario
from market_monitor.detection import detect
from market_monitor.notify import Notifier, format_message


def signal():
    return detect('TESTUSDT', *scenario(), Config()).signal


def test_message_and_console(capsys):
    s = signal()
    message = format_message(s)
    assert '已完成 K 线' in message
    assert '最近 30 分钟' in message
    assert 'https://www.binance.com/en/futures/TESTUSDT' in message
    assert '当前正在形成' in format_message(replace(s, source='forming'))
    notifier = Notifier(Config())
    assert notifier.send(s) == 'console'
    assert message in capsys.readouterr().out
    notifier.close()


@pytest.mark.parametrize('status,payload,success', [(200, {'ok': True}, True), (200, {'ok': False}, False), (429, {}, False), (500, {}, False)])
def test_telegram_mock(monkeypatch, status, payload, success):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'secret-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '123')
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'POST'
        return httpx.Response(status, json=payload)
    notifier = Notifier(Config(telegram=Telegram(enabled=True)), httpx.Client(transport=httpx.MockTransport(handle)))
    if success:
        assert notifier.send(signal()) == 'sent'
    else:
        with pytest.raises(RuntimeError) as exc: notifier.send(signal())
        assert 'secret-token' not in str(exc.value)
    assert len(calls) == 1
    notifier.close()


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    with pytest.raises(ValueError): Notifier(Config(telegram=Telegram(enabled=True)))
