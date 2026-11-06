from unittest.mock import Mock

from market_monitor import admin
from market_monitor.config import load, load_overrides, local_path, save_overrides
from market_monitor.state import State

BASE = '''symbols = ["BTCUSDT"]
interval = "15m"
database = "data/monitor.sqlite3"
[detection]
sensitivity = 1.0
'''


def write_config(tmp_path, text=BASE):
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return path


def bars(count=200, close=100.0):
    return [[index, close, close, close, close, 1.0, index, 1000.0, 10] for index in range(count)]


def test_overlay_merges_and_partial_detection_section_keeps_base_values(tmp_path):
    path = write_config(tmp_path)
    save_overrides(path, {'symbols': ['BTCUSDT', 'NVDAUSDT'],
                          'detection': {'sensitivity': 0.8}})
    config = load(path)
    assert config.symbols == ['BTCUSDT', 'NVDAUSDT']
    assert config.detection.sensitivity == 0.8
    assert config.detection.fast_15_pct == 1.5  # untouched base value survives
    assert load_overrides(path)['detection'] == {'sensitivity': 0.8}


def test_overlay_file_is_parseable_and_gitignored_name(tmp_path):
    path = write_config(tmp_path)
    save_overrides(path, {'symbols': ['AUSDT'], 'cooldown_seconds': 1800, 'dns_over_https': False})
    assert local_path(path).name == 'config.local.toml'
    assert load(path).cooldown_seconds == 1800
    assert 'false' in local_path(path).read_text()


def test_config_flag_is_accepted_after_subcommand():
    rest, config = admin._split_config(['config', 'add', 'NVDA', '--config', '/tmp/x.toml'])
    assert rest == ['config', 'add', 'NVDA']
    assert config == '/tmp/x.toml'


def test_add_validates_symbol_then_writes_overlay_and_audit(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NVDAUSDT', 'status': 'TRADING'}
    api.get.return_value = bars()
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))

    assert admin.cmd_config_add(path, ['nvda']) == 0
    assert load(path).symbols == ['BTCUSDT', 'NVDAUSDT']
    out = capsys.readouterr().out
    assert 'NVDAUSDT' in out and '自动生效' in out

    state = State(tmp_path / 'data/monitor.sqlite3')
    try:
        change = state.recent_config_changes(1)[0]
    finally:
        state.close()
    assert change['key'] == 'symbols'
    assert 'NVDAUSDT' in change['new_value']


def test_add_rejects_symbol_without_enough_history(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NEWUSDT', 'status': 'TRADING'}
    api.get.return_value = bars(count=20)
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))

    assert admin.cmd_config_add(path, ['NEWUSDT']) == 1
    assert load(path).symbols == ['BTCUSDT']
    assert '指标未就绪' in capsys.readouterr().out


def test_add_rejects_non_trading_symbol(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'OLDUSDT', 'status': 'SETTLING'}
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))

    assert admin.cmd_config_add(path, ['OLDUSDT']) == 1
    assert not local_path(path).exists()


def test_del_removes_episode_rows_but_refuses_empty(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, BASE.replace('["BTCUSDT"]', '["BTCUSDT", "APPUSDT"]'))
    state = State(tmp_path / 'data/monitor.sqlite3')
    state.db.execute("INSERT INTO episodes(symbol,direction,active) VALUES ('APPUSDT','up',1)")
    state.close()

    assert admin.cmd_config_del(path, ['app']) == 0
    assert load(path).symbols == ['BTCUSDT']
    state = State(tmp_path / 'data/monitor.sqlite3')
    try:
        assert state.db.execute("SELECT count(*) FROM episodes WHERE symbol='APPUSDT'").fetchone()[0] == 0
    finally:
        state.close()

    assert admin.cmd_config_del(path, ['BTCUSDT']) == 1
    assert '不能删空' in capsys.readouterr().out


def test_check_reports_facts_without_changing_config(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NVDAUSDT', 'status': 'TRADING'}
    api.get.return_value = bars(close=100.0)
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))

    assert admin.cmd_config_check(path, ['NVDA']) == 0
    out = capsys.readouterr().out
    assert '成交额中位' in out and '持续单边启动' in out and '粗估' in out
    assert not local_path(path).exists()


def test_status_without_database_explains_itself(tmp_path, capsys):
    path = write_config(tmp_path)
    assert admin.cmd_status(path) == 0
    out = capsys.readouterr().out
    assert '尚未扫描过' in out
    assert '标的：1 只' in out


def test_status_reads_last_scan_and_alerts(tmp_path, capsys):
    path = write_config(tmp_path)
    state = State(tmp_path / 'data/monitor.sqlite3')
    state.set_meta('last_scan', {'ts': 1_700_000_000, 'good': 1, 'total': 2,
                                 'duration_ms': 6500, 'errors': {'APPUSDT': 'stale'}})
    state.db.execute("INSERT INTO alerts(symbol,direction,kind,detected_ms,day,payload,status)"
                     " VALUES ('BTCUSDT','up','sustained',1700000000000,'2023-11-15','{}','emitted')")
    state.close()

    assert admin.cmd_status(path) == 0
    out = capsys.readouterr().out
    assert '1/2 成功' in out and '耗时 6.5 秒' in out
    assert 'APPUSDT（stale）' in out
    assert 'BTCUSDT 上涨' in out


def test_reset_restores_base_values(tmp_path, capsys):
    path = write_config(tmp_path)
    save_overrides(path, {'cooldown_seconds': 600, 'detection': {'sensitivity': 0.5}})
    assert load(path).cooldown_seconds == 600

    assert admin.cmd_config_reset(path, 'cooldown_seconds') == 0
    assert load(path).cooldown_seconds == 3600
    assert load(path).detection.sensitivity == 0.5

    assert admin.cmd_config_reset(path, 'detection.sensitivity') == 0
    assert load(path).detection.sensitivity == 1.0
    assert load_overrides(path) == {}


def test_help_lists_every_command(tmp_path, capsys):
    assert admin.main(['help']) == 0
    out = capsys.readouterr().out
    for needle in ('mm — 状态总览', 'mm list — 生效配置', 'mm add NVDA — 加入标的',
                   'mm del APP — 移除标的', 'mm check CRWV — 只查不加',
                   'mm reset — 撤销全部覆盖', 'mm audit — 最近的配置改动',
                   'mm run — 前台常驻'):
        assert needle in out
    # 一行一个，不留列对齐的空白
    assert 'mm status ' not in out


def test_mm_scan_commands_delegate_to_app(tmp_path, monkeypatch):
    from market_monitor import app
    path = write_config(tmp_path)
    seen = {}
    monkeypatch.setattr(app, 'main', lambda argv: seen.update(argv=argv) or 0)
    for action in ('once', 'run', 'verify', 'demo'):
        assert admin.main([action, '--config', str(path)]) == 0
        assert seen['argv'][0] == action
        assert seen['argv'][1:3] == ['--config', str(path)]


def test_relative_database_follows_config_file_not_cwd(tmp_path, monkeypatch):
    from market_monitor.app import resolve_paths as app_resolve_paths
    path = write_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    config = load(path)
    resolved = app_resolve_paths(config, path)
    assert resolved.database == str(tmp_path / 'data/monitor.sqlite3')


def test_audit_describes_symbol_changes_in_words(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NVDAUSDT', 'status': 'TRADING'}
    api.get.return_value = bars()
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))
    admin.cmd_config_add(path, ['NVDA'])
    capsys.readouterr()

    assert admin.cmd_config_audit(path) == 0
    out = capsys.readouterr().out
    assert '加入 NVDAUSDT' in out
    assert 'BTCUSDT", "NVDAUSDT' not in out  # raw JSON dump must not leak


def test_bare_mm_shows_status(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert admin.main([]) == 0
    assert '行情异动监控' in capsys.readouterr().out


def test_flat_add_syntax(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NVDAUSDT', 'status': 'TRADING'}
    api.get.return_value = bars()
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))
    monkeypatch.chdir(tmp_path)

    assert admin.main(['add', 'NVDA']) == 0
    assert load(path).symbols == ['BTCUSDT', 'NVDAUSDT']
    assert '已加入' in capsys.readouterr().out


def test_legacy_config_prefix_still_works(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    api = Mock()
    api.symbol_info.return_value = {'symbol': 'NVDAUSDT', 'status': 'TRADING'}
    api.get.return_value = bars()
    monkeypatch.setattr(admin, 'Binance', Mock(return_value=api))
    monkeypatch.chdir(tmp_path)

    assert admin.main(['config', 'add', 'NVDA']) == 0
    assert load(path).symbols == ['BTCUSDT', 'NVDAUSDT']


def test_market_monitor_entry_delegates_admin_actions(tmp_path, monkeypatch):
    from market_monitor import app
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert app.main(['list']) == 0
    assert app.main(['help']) == 0


def test_prune_deletes_only_alerts_past_retention(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from market_monitor.state import State

    path = write_config(tmp_path)
    database = tmp_path / 'data/monitor.sqlite3'
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    old, mid = (today - timedelta(days=400)).isoformat(), (today - timedelta(days=60)).isoformat()
    state = State(database)
    for day in (old, mid, today.isoformat()):
        state.db.execute(
            "INSERT INTO alerts(symbol,direction,kind,detected_ms,day,payload,status) "
            "VALUES ('BTCUSDT','up','fast',0,?,'{}','emitted')", (day,))
    state.db.execute("INSERT INTO episodes(symbol,direction,active) VALUES ('BTCUSDT','up',1)")
    state.close()

    monkeypatch.chdir(tmp_path)
    assert admin.main(['prune', '90']) == 0
    assert '删除 1 条' in capsys.readouterr().out

    state = State(database)
    try:
        days = [row[0] for row in state.db.execute("SELECT day FROM alerts ORDER BY day")]
        assert days == [mid, today.isoformat()]
        assert state.db.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
    finally:
        state.close()


def test_prune_rejects_zero_days(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert admin.main(['prune', '0']) == 1
    assert '至少 1 天' in capsys.readouterr().out


def test_prune_without_database_is_harmless(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert admin.main(['prune']) == 0
    assert '没有需要清理' in capsys.readouterr().out
