from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Dict, Mapping, Optional
from urllib import error, parse, request

from .auth import BayseAuth
from .models import BayseError, Order, OrderResponse, Quote, QuoteResponse


logger = logging.getLogger(__name__)


class BayseClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, error: Optional[BayseError] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error = error


@dataclasses.dataclass
class BayseClient:
    api_key: str
    api_secret: str
    user_id: Optional[str] = None
    base_url: str = "https://relay.bayse.markets"
    api_version: str = "v1"
    timeout: float = 30.0
    signature_encoding: str = "base64"
    api_key_header: str = "X-Public-Key"
    timestamp_header: str = "X-Timestamp"
    signature_header: str = "X-Signature"

    def __post_init__(self) -> None:
        self._auth = BayseAuth(
            api_key=self.api_key,
            api_secret=self.api_secret,
            api_key_header=self.api_key_header,
            timestamp_header=self.timestamp_header,
            signature_header=self.signature_header,
            signature_encoding=self.signature_encoding,
        )

    def _normalize_path(self, path: str) -> str:
        return path if path.startswith("/") else f"/{path}"

    def _versioned_path(self, path: str) -> str:
        normalized = self._normalize_path(path)
        prefix = f"/{self.api_version}"
        if normalized.startswith(prefix):
            return normalized
        return f"{prefix}{normalized}"

    def _build_url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        full_path = self._versioned_path(path)
        url = f"{self.base_url.rstrip('/')}{full_path}"
        if params:
            query = parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"
        return url

    def _scoped_params(self, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        query: Dict[str, Any] = dict(params or {})
        if self.user_id:
            query.setdefault("userId", self.user_id)
        return query

    def _parse_response(self, payload: str) -> Dict[str, Any]:
        if not payload:
            return {}
        return json.loads(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        auth: str = "none",
    ) -> Dict[str, Any]:
        body_bytes = None
        body_for_signing: Any = None
        headers: Dict[str, str] = {"Accept": "application/json"}

        if json_body is not None:
            serialized_body = json.dumps(json_body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            body_bytes = serialized_body
            body_for_signing = serialized_body
            headers["Content-Type"] = "application/json"

        versioned_path = self._versioned_path(path)
        request_path = versioned_path
        if params:
            query = parse.urlencode({k: v for k, v in params.items() if v is not None})
            request_path = f"{versioned_path}?{query}"

        request_url = f"{self.base_url.rstrip('/')}{request_path}"

        if auth == "read":
            headers[self.api_key_header] = self.api_key
        elif auth == "private":
            logger.debug("Bayse authenticated request method=%s url=%s auth=%s", method.upper(), request_url, auth)
            headers.update(self._auth.sign(method=method, path=request_path, body=body_for_signing))
            if self.user_id:
                headers.setdefault("X-User-Id", self.user_id)
        elif auth == "write":
            logger.debug("Bayse authenticated request method=%s url=%s auth=%s", method.upper(), request_url, auth)
            headers.update(self._auth.sign(method=method, path=request_path, body=body_for_signing))
            if self.user_id:
                headers.setdefault("X-User-Id", self.user_id)
        elif auth == "session":
            raise ValueError("session auth requires explicit session headers")

        if auth == "read":
            logger.debug("Bayse authenticated request method=%s url=%s auth=%s", method.upper(), request_url, auth)

        if extra_headers:
            headers.update(extra_headers)

        req = request.Request(
            request_url,
            data=body_bytes,
            headers=headers,
            method=method.upper(),
        )

        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
                parsed = self._parse_response(payload)
                if path in {"/pm/balance", "/pm/portfolio", "/wallet/assets"} and isinstance(parsed, dict):
                    logger.info("Bayse fetched %s successfully", path)
                return parsed
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            parsed_error: Optional[BayseError] = None
            if raw:
                try:
                    parsed_error = BayseError.from_dict(json.loads(raw))
                except Exception:
                    parsed_error = BayseError(code=str(exc.code), message=raw or exc.reason, raw={"status": exc.code})
            raise BayseClientError(
                parsed_error.message if parsed_error else exc.reason,
                status_code=exc.code,
                error=parsed_error,
            ) from exc
        except error.URLError as exc:
            raise BayseClientError(str(exc.reason)) from exc

    def _session_headers(self, token: str, device_id: str) -> Dict[str, str]:
        return {
            "x-auth-token": token,
            "x-device-id": device_id,
        }

    # Public and read endpoints
    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    def version(self) -> Dict[str, Any]:
        return self._request("GET", "/version")

    def list_events(self, *, page: int = 1, size: int = 20, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        query: Dict[str, Any] = {"page": page, "size": size}
        if params:
            query.update(params)
        return self._request("GET", "/pm/events", params=query, auth="read")

    def search_events(self, keyword: str, *, page: int = 1, size: int = 20, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        query: Dict[str, Any] = {"page": page, "size": size, "keyword": keyword}
        if params:
            query.update(params)
        return self._request("GET", "/pm/events", params=query, auth="read")

    def get_event(self, event_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/pm/events/{event_id}", auth="read")

    def get_event_by_slug(self, slug: str) -> Dict[str, Any]:
        return self._request("GET", f"/pm/events/slug/{slug}", auth="read")

    def get_balance(self) -> Dict[str, Any]:
        return self._request("GET", "/pm/balance", params=self._scoped_params(), auth="private")

    def get_portfolio(self) -> Dict[str, Any]:
        return self._request("GET", "/pm/portfolio", params=self._scoped_params(), auth="private")

    def get_assets(self) -> Dict[str, Any]:
        return self._request("GET", "/wallet/assets", params=self._scoped_params(), auth="private")

    def list_orders(self, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", "/pm/orders", params=self._scoped_params(params), auth="private")

    def get_order(self, order_id: str) -> OrderResponse:
        payload = self._request("GET", f"/pm/orders/{order_id}", auth="read")
        return OrderResponse.from_dict(payload)

    def get_ticker(self, market_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/pm/markets/{market_id}/ticker", auth="read")

    def get_orderbook(self, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", "/pm/books", params=params, auth="read")

    def get_trades(self, *, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", "/pm/trades", params=params, auth="read")

    # Login and API-key management
    def login(self, email: str, password: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/user/login",
            json_body={"email": email, "password": password},
        )

    def create_api_key(self, token: str, device_id: str, name: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/user/me/api-keys",
            json_body={"name": name},
            extra_headers=self._session_headers(token, device_id),
        )

    def list_api_keys(self, token: str, device_id: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/user/me/api-keys",
            extra_headers=self._session_headers(token, device_id),
        )

    def revoke_api_key(self, token: str, device_id: str, key_id: str) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            f"/user/me/api-keys/{key_id}",
            extra_headers=self._session_headers(token, device_id),
        )

    def rotate_api_key(self, token: str, device_id: str, key_id: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/user/me/api-keys/{key_id}/rotate",
            extra_headers=self._session_headers(token, device_id),
        )

    # Trading endpoints
    def get_quote(self, symbol: str) -> QuoteResponse:
        payload = self.get_ticker(symbol)
        return QuoteResponse.from_dict(payload)

    def get_market_quote(self, event_id: str, market_id: str, quote: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/pm/events/{event_id}/markets/{market_id}/quote",
            json_body=dict(quote) if quote is not None else None,
            auth="read",
        )

    def place_order(
        self,
        event_id: str,
        market_id: str,
        *,
        outcome_id: str,
        side: str,
        amount: float,
        currency: str,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "outcomeId": outcome_id,
            "side": side.upper(),
            "amount": amount,
            "type": order_type.upper(),
            "currency": currency,
        }
        if price is not None:
            body["price"] = price
        return self._request(
            "POST",
            f"/pm/events/{event_id}/markets/{market_id}/orders",
            json_body=body,
            auth="write",
        )

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/pm/orders/{order_id}", auth="write")

    def mint_shares(self, market_id: str, quantity: float, *, currency: str = "USD") -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/pm/markets/{market_id}/mint",
            json_body={"quantity": quantity, "currency": currency.upper()},
            auth="write",
        )

    def burn_shares(self, market_id: str, quantity: float, *, currency: str = "USD") -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/pm/markets/{market_id}/burn",
            json_body={"quantity": quantity, "currency": currency.upper()},
            auth="write",
        )

    # Compatibility helpers retained for existing integrations
    def quote(self, symbol: str) -> Quote:
        payload = self.get_ticker(symbol)
        return Quote.from_dict(payload)

    def order(self, order_id: str) -> Order:
        return self.get_order(order_id).order
