from dataclasses import replace
from io import StringIO
from unittest.mock import Mock

from market_monitor import hermes
from market_monitor.app import Monitor
from market_monitor.config import Config, Telegram
from market_monitor.demo import scenario
from market_monitor.market import MarketError
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


def test_breadth_counts_same_direction_and_excludes_self():
    from market_monitor.app import breadth_of
    seen = {'AUSDT': ('down', 'sustained'), 'BUSDT': ('down', 'fast'), 'CUSDT': ('up', 'sustained')}
    assert breadth_of('AUSDT', 'down', seen, 13) == 2
    assert breadth_of('CUSDT', 'up', seen, 13) == 1
    assert breadth_of('DUSDT', 'up', seen, 13) == 2      # C + 自身
    assert breadth_of('DUSDT', 'down', seen, 13) == 3
    assert breadth_of('AUSDT', 'down', seen, 1) == 1     # 不超过配置标的数


def test_breadth_is_recorded_in_payload_and_shown(tmp_path):
    import json
    bars, minutes, now = scenario()
    cfg = Config(symbols=['AAAUSDT', 'BBBUSDT'], history_bars=len(bars))
    output = StringIO()
    api = Mock()
    api.now_ms.return_value = now
    api.get.side_effect = lambda path, **params: {'symbol': params.get('symbol'), 'time': now}
    state = State(tmp_path / 'state.db')
    monitor = Monitor(cfg, api, state, hermes.HermesNotifier(cfg, output))
    monitor.cache.refresh = Mock(side_effect=[bars, minutes, bars, minutes])
    assert monitor.scan() == 2
    payloads = [json.loads(row[0]) for row in state.db.execute('select payload from alerts order by id')]
    assert [p['breadth'] for p in payloads] == [1, 2]   # 第二个标的能看到第一个
    assert all(p['breadth_total'] == 2 for p in payloads)
    assert all(p['window'] in (2, 3, 4) for p in payloads)
    assert all(p['vol_ratio'] is None or p['vol_ratio'] > 0 for p in payloads)
    assert '1/2 只同向' in output.getvalue() and '2/2 只同向' in output.getvalue()
    state.close()


def test_derivatives_failure_never_blocks_alert(tmp_path):
    """OI / 资金费率只是观测数据：拉取失败必须照常报警。"""
    import json
    bars, minutes, now = scenario()
    cfg = Config(symbols=['TESTUSDT'], history_bars=len(bars))
    api = Mock()
    api.now_ms.return_value = now
    api.get.return_value = {'symbol': 'TESTUSDT', 'time': now}
    api.derivatives.side_effect = MarketError('openInterestHist unavailable')
    state = State(tmp_path / 'state.db')
    monitor = Monitor(cfg, api, state, hermes.HermesNotifier(cfg, StringIO()))
    monitor.cache.refresh = Mock(side_effect=[bars, minutes])
    assert monitor.scan() == 1
    payload = json.loads(state.db.execute('select payload from alerts').fetchone()[0])
    assert payload['oi_change'] is None and payload['funding_rate'] is None
    assert payload['session'] in ('RTH', 'pre', 'post', 'night', 'weekend')
    state.close()


def test_derivatives_values_recorded(tmp_path):
    import json
    bars, minutes, now = scenario()
    cfg = Config(symbols=['TESTUSDT'], history_bars=len(bars))
    api = Mock()
    api.now_ms.return_value = now
    api.get.return_value = {'symbol': 'TESTUSDT', 'time': now}
    api.derivatives.return_value = {'oi_change': -2.5, 'funding_rate': 0.0269}
    state = State(tmp_path / 'state.db')
    monitor = Monitor(cfg, api, state, hermes.HermesNotifier(cfg, StringIO()))
    monitor.cache.refresh = Mock(side_effect=[bars, minutes])
    assert monitor.scan() == 1
    payload = json.loads(state.db.execute('select payload from alerts').fetchone()[0])
    assert payload['oi_change'] == -2.5 and payload['funding_rate'] == 0.0269
    state.close()


def _monitor(tmp_path, symbols, frames, now, config=None):
    cfg = config or Config(symbols=symbols, history_bars=len(frames[0]))
    api = Mock()
    api.now_ms.return_value = now
    api.get.side_effect = lambda path, **params: {'symbol': params.get('symbol'), 'time': now}
    state = State(tmp_path / 'state.db')
    output = StringIO()
    monitor = Monitor(cfg, api, state, hermes.HermesNotifier(cfg, output))
    monitor.cache.refresh = Mock(side_effect=frames)
    return monitor, state, output


def test_three_same_direction_merge_into_one_sector_message(tmp_path):
    bars, minutes, now = scenario((101, 102.1, 103.2))
    monitor, state, output = _monitor(tmp_path, ['AUSDT', 'BUSDT', 'CUSDT'], [bars, minutes] * 3, now)
    assert monitor.scan() == 3
    text = output.getvalue()
    assert text.count('板块级') == 1
    assert '3 只同向' in text
    for symbol in ('AUSDT', 'BUSDT', 'CUSDT'):
        assert f'**{symbol}**' in text
    # 不再逐条推送
    assert '📈 **' not in text
    rows = state.db.execute('select symbol,status,detail from alerts order by id').fetchall()
    assert [row['detail'] for row in rows] == ['batched:3'] * 3
    assert all(row['status'] == 'emitted' for row in rows)
    state.close()


def test_sector_message_uses_max_breadth_not_first_symbol(tmp_path):
    """广度取成员最大值：先扫到的标的只看到上轮快照，不能代表全体。"""
    from market_monitor.notify import format_sector_alert
    from market_monitor.detection import detect
    up, upm, now = scenario((101, 102.1, 103.2))
    base = detect('AUSDT', up, upm, now, Config())
    first = replace(base.signal, breadth=1, breadth_total=4)
    second = replace(base.signal, symbol='BUSDT', breadth=4, breadth_total=4)
    text = format_sector_alert([first, second])
    assert '同向 4/4 只' in text


def test_escalation_render_single_and_sector():
    from market_monitor.detection import detect
    from market_monitor.notify import format_chat_alert, format_sector_alert
    bars, minutes, now = scenario((101, 102.1, 103.2))
    base = detect('AUSDT', bars, minutes, now, Config()).signal
    up = replace(base, kind='escalation', escalation_from=2.0, atr_units=4.2)
    single = format_chat_alert(up)
    assert '异动升级（上涨）' in single
    assert '位移 2.0→4.2×ATR' in single
    sector = format_sector_alert([up, replace(up, symbol='BUSDT')])
    assert '板块级上涨升级（2 只同向）' in sector
    assert '2.0→4.2×ATR' in sector


def test_two_symbols_do_not_merge(tmp_path):
    bars, minutes, now = scenario((101, 102.1, 103.2))
    monitor, state, output = _monitor(tmp_path, ['AUSDT', 'BUSDT'], [bars, minutes] * 2, now)
    assert monitor.scan() == 2
    text = output.getvalue()
    assert '板块级' not in text
    assert text.count('📈 **') == 2
    assert all(row['detail'] is None
               for row in state.db.execute('select detail from alerts'))
    state.close()


def test_opposite_directions_are_counted_separately(tmp_path):
    up, upm, now = scenario((101, 102.1, 103.2))
    down, dm, _ = scenario((99, 97.9, 96.8))
    monitor, state, output = _monitor(
        tmp_path, ['AUSDT', 'BUSDT', 'CUSDT', 'DUSDT'],
        [up, upm, up, upm, down, dm, down, dm], now)
    assert monitor.scan() == 4
    text = output.getvalue()
    assert '板块级' not in text          # 2 涨 + 2 跌，各自都没到 3
    assert '📈 **' in text and '📉 **' in text
    state.close()


def test_merge_can_be_disabled(tmp_path):
    bars, minutes, now = scenario((101, 102.1, 103.2))
    cfg = Config(symbols=['AUSDT', 'BUSDT', 'CUSDT'], history_bars=len(bars), merge_min_symbols=0)
    monitor, state, output = _monitor(tmp_path, None, [bars, minutes] * 3, now, config=cfg)
    assert monitor.scan() == 3
    assert '板块级' not in output.getvalue()
    assert output.getvalue().count('📈 **') == 3
    state.close()


def test_reserved_alerts_are_flushed_when_scan_aborts(tmp_path):
    """中途限流中止也必须把已预留的告警发出去，不能烂在 reserved。"""
    import pytest
    from market_monitor.market import RateLimited
    bars, minutes, now = scenario((101, 102.1, 103.2))
    monitor, state, output = _monitor(
        tmp_path, ['AUSDT', 'BUSDT'], [bars, minutes, RateLimited('backoff')], now)
    with pytest.raises(RateLimited):
        monitor.scan()
    assert '**AUSDT**' in output.getvalue()
    assert state.db.execute('select status from alerts').fetchone()[0] == 'emitted'
    state.close()


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
    assert lines[5].startswith('📦 量能：')
    assert lines[6].startswith('🧭 ADX：')
    assert lines[7].startswith('🧩 共振：')
    assert lines[8].startswith('⏱️ 类型：')
    assert lines[9].startswith('🕐 提醒时间：')
    assert len(lines) == 10
    # 单独 detect 时没有跨标的统计，必须写明而不是显示 0
    assert '未统计' in lines[7]
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

