from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from medes_et_bayse.auth import BayseAuth
from medes_et_bayse.client import BayseClient


def test_sign_uses_raw_json_bytes_for_body_hash() -> None:
    auth = BayseAuth(api_key="pk_test", api_secret="sk_test")
    body = json.dumps(
        {
            "amount": 100,
            "currency": "USD",
            "outcomeId": "yes",
            "side": "BUY",
            "type": "LIMIT",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    timestamp = "1710000000"
    path = "/v1/pm/events/evt_123/markets/mkt_456/orders"
    headers = auth.sign(method="POST", path=path, body=body, timestamp=timestamp)

    payload = f"{timestamp}.POST.{path}.{hashlib.sha256(body).hexdigest()}"
    expected = base64.b64encode(
        hmac.new(b"sk_test", payload.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    assert headers["X-Timestamp"] == timestamp
    assert headers["X-Signature"] == expected


def test_client_passes_serialized_bytes_to_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BayseClient(api_key="pk_test", api_secret="sk_test")
    seen: dict[str, object] = {}

    def fake_sign(*, method: str, path: str, body=None, timestamp=None):
        seen["method"] = method
        seen["path"] = path
        seen["body"] = body
        return {
            "X-Public-Key": "pk_test",
            "X-Timestamp": "1",
            "X-Signature": "sig",
        }

    object.__setattr__(client._auth, "sign", fake_sign)  # BayseAuth is frozen; bypass via object.__setattr__

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b"{}"

    monkeypatch.setattr("medes_et_bayse.client.request.urlopen", lambda *args, **kwargs: FakeResponse())

    client._request("POST", "/pm/orders", json_body={"b": 2, "a": 1}, auth="private")

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/pm/orders"
    assert seen["body"] == b'{"a":1,"b":2}'


def test_sign_rejects_empty_secret() -> None:
    auth = BayseAuth(api_key="pk_test", api_secret="")

    with pytest.raises(ValueError, match="secret key"):
        auth.sign(method="GET", path="/v1/pm/orders", timestamp="1")
