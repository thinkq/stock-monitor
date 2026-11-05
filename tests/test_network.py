import httpx
import pytest

from market_monitor.network import DoHTransport


def make_transport(answers=None, handler=None):
    calls = []
    def dns(request):
        calls.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': answers if answers is not None else [
            {'type': 1, 'TTL': 60, 'data': '8.8.8.8'}, {'type': 1, 'TTL': 60, 'data': '1.1.1.1'}]})
    resolver = httpx.Client(transport=httpx.MockTransport(dns))
    transport = DoHTransport('fapi.binance.com', resolver, httpx.MockTransport(handler or (lambda _: httpx.Response(200))))
    return transport, calls


def test_preserves_host_tls_identity_query_and_caches_dns():
    routed = []
    def handle(request):
        routed.append(request)
        return httpx.Response(200, json={'serverTime': 123})
    transport, calls = make_transport(handler=handle)
    with httpx.Client(transport=transport) as client:
        for _ in range(2):
            response = client.get('https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT')
            assert response.status_code == 200
        assert len(calls) == 1
        assert routed[0].url.host == '8.8.8.8'
        assert routed[0].headers['host'] == 'fapi.binance.com'
        assert routed[0].extensions['sni_hostname'] == 'fapi.binance.com'
        assert routed[0].url.params['symbol'] == 'BTCUSDT'
        assert response.request.url.host == 'fapi.binance.com'
        transport.expires = 0
        client.get('https://fapi.binance.com/fapi/v1/time')
        assert len(calls) == 2


def test_connect_failure_tries_next_address():
    hosts = []
    def handle(request):
        hosts.append(request.url.host)
        if len(hosts) == 1:
            raise httpx.ConnectTimeout('first unavailable')
        return httpx.Response(200)
    transport, _ = make_transport(handler=handle)
    with httpx.Client(transport=transport) as client:
        assert client.get('https://fapi.binance.com/fapi/v1/time').status_code == 200
    assert hosts == ['8.8.8.8', '1.1.1.1']


@pytest.mark.parametrize('answers', [[], [{'type': 1, 'TTL': 60, 'data': '127.0.0.1'}],
                                    [{'type': 1, 'TTL': 60, 'data': 'invalid'}]])
def test_invalid_dns_fails_closed(answers):
    transport, _ = make_transport(answers)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            client.get('https://fapi.binance.com/fapi/v1/time')


def test_dns_failure_never_reuses_expired_addresses():
    transport, _ = make_transport()
    transport.addresses = ['8.8.8.8']
    transport.expires = 0
    transport.resolver.close()
    transport.resolver = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(httpx.ConnectError):
            client.get('https://fapi.binance.com/fapi/v1/time')
        assert transport.addresses == []
