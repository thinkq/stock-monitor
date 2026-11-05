from dataclasses import replace
from unittest.mock import Mock

import httpx
import pytest

from market_monitor.app import Monitor
from market_monitor.config import Config
from market_monitor.demo import scenario
from market_monitor.market import Binance, CandleCache, MarketError, RateLimited, parse_klines
from market_monitor.state import State


def api_with(handler, retries=0):
    return Binance(client=httpx.Client(base_url='https://example.test', transport=httpx.MockTransport(handler)), retries=retries)


def to_rows(frame):
    return [[int(r.time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume),
             int(r.close_time), '0', int(r.trades), '0', '0', '0'] for r in frame.itertuples()]


@pytest.mark.parametrize('status', [429, 418])
def test_global_rate_limit(status):
    handler = Mock(return_value=httpx.Response(status, headers={'Retry-After': '120'}))
    api = api_with(handler)
    with pytest.raises(RateLimited): api.get('/fapi/v1/klines')
    with pytest.raises(RateLimited): api.get('/fapi/v1/exchangeInfo')
    assert handler.call_count == 1
    api.close()


def test_transient_retry(monkeypatch):
    monkeypatch.setattr('market_monitor.market.time.sleep', lambda _: None)
    handler = Mock(side_effect=[httpx.Response(500), httpx.Response(200, json={'serverTime': 123})])
    api = api_with(handler, retries=1)
    assert api.get('/fapi/v1/time') == {'serverTime': 123}
    assert handler.call_count == 2
    api.close()


def test_timeout_and_bad_json():
    def timeout(request): raise httpx.ReadTimeout('timeout')
    api = api_with(timeout)
    with pytest.raises(MarketError): api.get('/fapi/v1/klines')
    api.close()
    api = api_with(lambda _: httpx.Response(200, text='not JSON'))
    with pytest.raises(MarketError): api.get('/fapi/v1/klines')
    api.close()


def test_symbol_validation():
    api = api_with(lambda _: httpx.Response(200, json={'symbols': [
        {'symbol': 'BTCUSDT', 'status': 'TRADING'}, {'symbol': 'OLDUSDT', 'status': 'SETTLING'}]}))
    api.validate_symbols(['BTCUSDT'])
    for symbols in (['NVDAUSDT'], ['OLDUSDT']):
        with pytest.raises(MarketError): api.validate_symbols(symbols)
    api.close()


def test_duplicate_rows_last_wins():
    b, _, _ = scenario()
    rows = to_rows(b)
    rows.append(rows[-1].copy())
    rows[-1][4] = str(float(rows[-1][4]) + .01)
    frame = parse_klines(rows)
    assert len(frame) == len(b)
    assert frame.close.iloc[-1] == b.close.iloc[-1] + .01


def test_incremental_and_gap_invalidates_cache():
    b, _, now = scenario()
    api = Mock()
    api.now_ms.return_value = now
    api.get.return_value = to_rows(b)
    cache = CandleCache()
    cache.refresh(api, 'TESTUSDT', '15m', 900_000, len(b))
    api.get.return_value = to_rows(b.tail(2))
    cache.refresh(api, 'TESTUSDT', '15m', 900_000, len(b))
    assert api.get.call_args.kwargs['limit'] == 3
    assert 'startTime' in api.get.call_args.kwargs
    api.now_ms.return_value = now + 3 * 900_000
    with pytest.raises(MarketError): cache.refresh(api, 'TESTUSDT', '15m', 900_000, len(b))
    assert not cache.frames


@pytest.mark.parametrize('failure', ['timeout', 'stale', 'missing', 'cold'])
def test_pipeline_failure_never_notifies_or_changes_state(failure):
    b, m, now = scenario()
    cfg = Config(symbols=['TESTUSDT'], history_bars=len(b))
    api = Mock()
    api.now_ms.return_value = now
    api.get.return_value = {'symbol': 'TESTUSDT', 'time': now - (999_000 if failure == 'stale' else 0)}
    notifier = Mock()
    state = State(':memory:')
    monitor = Monitor(cfg, api, state, notifier)
    monitor.cache.refresh = Mock(side_effect=MarketError('timeout')) if failure == 'timeout' else Mock(side_effect=[b.drop(index=100) if failure == 'missing' else b.tail(10) if failure == 'cold' else b, m])
    assert monitor.scan() == 0
    assert state.db.execute('select count(*) from alerts').fetchone()[0] == 0
    assert state.db.execute('select count(*) from episodes').fetchone()[0] == 0
    notifier.send.assert_not_called()
    state.close()


def test_pipeline_reserves_before_send_and_deduplicates(tmp_path):
    b, m, now = scenario()
    cfg = Config(symbols=['TESTUSDT'], history_bars=len(b))
    api = Mock()
    api.now_ms.return_value = now
    api.get.return_value = {'symbol': 'TESTUSDT', 'time': now}
    state = State(tmp_path / 'state.db')
    notifier = Mock()
    def fail(signal):
        assert state.db.execute('select status from alerts').fetchone()[0] == 'reserved'
        raise RuntimeError('network uncertain')
    notifier.send.side_effect = fail
    monitor = Monitor(cfg, api, state, notifier)
    monitor.cache.refresh = Mock(side_effect=[b, m, b, m])
    assert monitor.scan() == 1
    assert monitor.scan() == 1
    assert notifier.send.call_count == 1
    assert state.db.execute('select status from alerts').fetchone()[0] == 'failed'
    state.close()


def test_clock_resamples_after_cold_connection(monkeypatch):
    ticks = iter([0., 6., 6., 6.2])
    monkeypatch.setattr('market_monitor.market.time.monotonic', lambda: next(ticks))
    api = Binance(client=Mock())
    api.get = Mock(side_effect=[{'serverTime': 1000}, {'serverTime': 2000}])
    api.sync_clock()
    assert api.clock == (2000, pytest.approx(6.1))
    assert api.get.call_count == 2


def test_clock_rejects_two_slow_samples(monkeypatch):
    ticks = iter([0., 6., 6., 12.])
    monkeypatch.setattr('market_monitor.market.time.monotonic', lambda: next(ticks))
    api = Binance(client=Mock())
    api.get = Mock(return_value={'serverTime': 1000})
    with pytest.raises(MarketError, match='too slow'):
        api.sync_clock()
    assert api.clock is None
