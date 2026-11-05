"""Optional application-local DoH; preserve the origin's Host and TLS identity."""
import ipaddress
import time

import httpx


class DoHTransport(httpx.BaseTransport):
    def __init__(self, host, resolver=None, transport=None):
        self.host = host
        self.resolver = resolver or httpx.Client(timeout=5, trust_env=False)
        self.transport = transport or httpx.HTTPTransport()
        self.addresses = []
        self.expires = 0.0

    def resolve(self):
        if self.addresses and time.monotonic() < self.expires:
            return self.addresses
        try:
            response = self.resolver.get('https://dns.google/resolve', params={'name': self.host, 'type': 'A'})
            response.raise_for_status()
            data = response.json()
            if data.get('Status') != 0:
                raise ValueError('DNS query failed')
            records = [r for r in data.get('Answer', []) if r.get('type') == 1]
            addresses = []
            for record in records:
                address = ipaddress.IPv4Address(record['data'])
                if not address.is_global:
                    raise ValueError('non-public DNS address')
                addresses.append(str(address))
            if not addresses:
                raise ValueError('no A records')
            ttl = min(int(r['TTL']) for r in records)
            self.addresses = list(dict.fromkeys(addresses))
            self.expires = time.monotonic() + max(0, min(ttl, 300))
            return self.addresses
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.addresses = []
            raise httpx.ConnectError('DNS-over-HTTPS resolution failed') from None

    def handle_request(self, request):
        if request.url.host != self.host or request.url.scheme != 'https':
            raise httpx.ConnectError('unexpected origin for DoH transport')
        # Public GET requests only. No global DNS monkey patch or hosts-file modification.
        if request.method != 'GET':
            raise httpx.ConnectError('DoH transport only accepts public GET requests')
        for address in self.resolve()[:3]:
            routed = httpx.Request(
                request.method, request.url.copy_with(host=address),
                headers=request.headers,
                extensions={**request.extensions, 'sni_hostname': self.host},
            )
            try:
                return self.transport.handle_request(routed)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
        self.addresses = []
        raise httpx.ConnectError('all resolved Binance addresses failed')

    def close(self):
        self.resolver.close()
        self.transport.close()
