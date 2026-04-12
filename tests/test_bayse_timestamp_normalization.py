from __future__ import annotations

from medes_et_bayse.auth import BayseAuth


def test_sign_normalizes_decimal_timestamp_to_integer() -> None:
    auth = BayseAuth(api_key='pk_test', api_secret='sk_test')

    headers = auth.sign(
        method='POST',
        path='/v1/pm/orders',
        body={'hello': 'world'},
        timestamp='1710000000.987',
    )

    assert headers['X-Timestamp'] == '1710000000'
    assert headers['X-Public-Key'] == 'pk_test'
    assert headers['X-Signature']
