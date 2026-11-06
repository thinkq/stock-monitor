from dataclasses import replace
from io import StringIO
from unittest.mock import Mock

from market_monitor import hermes
from market_monitor.app import Monitor
from market_monitor.config import Config, Telegram
from market_monitor.demo import scenario
from market_monitor.state import State


def test_emits_once_and_persists_across_restart(tmp_path):
    bars, minutes, now = scenario()
    cfg = Config(symbols=['TESTUSDT'], history_bars=len(bars),
                 telegram=Telegram(enabled=True))
    output = StringIO()
    for _ in range(2):
        api = Mock()
        api.now_ms.return_value = now
        api.get.return_value = {'symbol': 'TESTUSDT', 'time': now}
        state = State(tmp_path / 'state.db')
        monitor = Monitor(cfg, api, state, hermes.HermesNotifier(cfg, output))
        monitor.cache.refresh = Mock(side_effect=[bars, minutes])
        assert monitor.scan() == 1
        assert state.db.execute('select status from alerts').fetchone()[0] == 'emitted'
        state.close()
    assert output.getvalue().count('**TESTUSDT**') == 1


def test_overlapping_scan_skipped(tmp_path):
    with hermes.scan_lock(tmp_path / 'state.db') as first:
        with hermes.scan_lock(tmp_path / 'state.db') as second:
            assert first and not second
    with hermes.scan_lock(tmp_path / 'state.db') as released:
        assert released


def test_entry_is_silent_and_database_is_relative_to_config(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'config.toml'
    config.write_text('symbols = ["TESTUSDT"]\n[telegram]\nenabled = true\n')
    monkeypatch.chdir(tmp_path.parent)
    api = Mock()
    api.active_symbols.return_value = {'TESTUSDT'}
    monkeypatch.setattr(hermes, 'Binance', Mock(return_value=api))
    scan = Mock(return_value=1)
    factory = Mock(return_value=Mock(scan=scan))
    monkeypatch.setattr(hermes, 'Monitor', factory)
    assert hermes.main(['--config', str(config)]) == 0
    assert (tmp_path / 'data/monitor.sqlite3').exists()
    assert factory.call_args.args[0].database == str(tmp_path / 'data/monitor.sqlite3')
    assert capsys.readouterr().out == ''
    api.close.assert_called_once()
    scan.return_value = 0
    assert hermes.main(['--config', str(config)]) == 1


def test_output_failure_marks_attempt_failed():
    stream = Mock()
    stream.write.side_effect = BrokenPipeError()
    notifier = hermes.HermesNotifier(Config(), stream)
    bars, minutes, now = scenario()
    from market_monitor.detection import detect
    import pytest
    with pytest.raises(RuntimeError):
        notifier.send(detect('TESTUSDT', bars, minutes, now, Config()).signal)
    assert notifier.failed


def test_alert_layout_matches_ema_watch_style():
    from market_monitor.detection import detect
    from market_monitor.notify import format_alert_batch, format_chat_alert
    bars, minutes, now = scenario()
    sig = detect('NVDAUSDT', bars, minutes, now, Config()).signal
    lines = format_chat_alert(sig).splitlines()
    assert lines[0] == '📈 **NVDAUSDT**'
    assert lines[1] == ''
    assert lines[2].startswith('💰 当前价：')
    assert lines[3].startswith('📊 涨跌幅：')
    assert lines[4].startswith('📈 单边效率：')
    assert lines[5].startswith('🧭 ADX：')
    assert lines[6].startswith('⏱️ 类型：')
    assert lines[7].startswith('🕐 提醒时间：')
    assert len(lines) == 8
    assert format_alert_batch([sig, sig]).count('\n\n---\n\n') == 1


def _run_main(tmp_path, monkeypatch, symbols, good, caplog):
    config = tmp_path / 'config.toml'
    config.write_text(f'symbols = {list(symbols)!r}\n')
    api = Mock()
    api.active_symbols.return_value = set(symbols)
    monkeypatch.setattr(hermes, 'Binance', Mock(return_value=api))
    monkeypatch.setattr(hermes, 'Monitor', Mock(return_value=Mock(scan=Mock(return_value=good))))
    return hermes.main(['--config', str(config)])


def test_partial_scan_exits_zero_so_stdout_is_delivered(tmp_path, monkeypatch, caplog):
    # A single stale symbol must not cost the alerts already on stdout.
    with caplog.at_level('WARNING'):
        assert _run_main(tmp_path, monkeypatch, ['AAAUSDT', 'BBBUSDT'], 1, caplog) == 0
    assert 'partial scan: 1/2' in caplog.text


def test_whole_scan_failure_exits_nonzero(tmp_path, monkeypatch):
    assert _run_main(tmp_path, monkeypatch, ['AAAUSDT', 'BBBUSDT'], 0, None) == 1


def test_symbol_cache_skips_exchange_info(tmp_path):
    state = State(tmp_path / 'state.db')
    api = Mock()
    api.active_symbols.return_value = {'AAAUSDT', 'BBBUSDT'}
    cfg = Config(symbols=['AAAUSDT'])
    hermes.validate_symbols(api, state, cfg)
    hermes.validate_symbols(api, state, cfg)
    assert api.active_symbols.call_count == 1
    # An incomplete scan or a newly configured symbol must re-validate.
    state.clear_symbol_cache()
    hermes.validate_symbols(api, state, cfg)
    assert api.active_symbols.call_count == 2
    hermes.validate_symbols(api, state, Config(symbols=['AAAUSDT', 'NEWUSDT']))
    assert api.active_symbols.call_count == 3
    # Expired cache is not trusted.
    state.set_symbol_cache({'AAAUSDT', 'BBBUSDT'}, 0)
    hermes.validate_symbols(api, state, Config(symbols=['AAAUSDT']))
    assert api.active_symbols.call_count == 4
    state.close()

