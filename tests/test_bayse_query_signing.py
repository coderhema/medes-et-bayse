from __future__ import annotations

import pytest

from medes_et_bayse.client import BayseClient


def test_private_request_signs_query_string_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BayseClient(api_key='pk_test', api_secret='sk_test')
    seen: dict[str, object] = {}

    def fake_sign(*, method: str, path: str, body=None, timestamp=None):
        seen['method'] = method
        seen['path'] = path
        seen['body'] = body
        return {
            'X-Public-Key': 'pk_test',
            'X-Timestamp': '1710000000',
            'X-Signature': 'sig',
        }

    client._auth.sign = fake_sign  # type: ignore[assignment]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b'{}'

    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        return FakeResponse()

    monkeypatch.setattr('medes_et_bayse.client.request.urlopen', fake_urlopen)

    client._request(
        'GET',
        '/pm/orders',
        params={'status': 'open', 'page': 1, 'size': 20},
        auth='private',
    )

    assert seen['method'] == 'GET'
    assert seen['path'] == '/v1/pm/orders?status=open&page=1&size=20'
    assert captured['url'] == 'https://relay.bayse.markets/v1/pm/orders?status=open&page=1&size=20'
    assert seen['body'] is None
