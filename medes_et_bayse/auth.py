from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Mapping, MutableMapping, Optional


logger = logging.getLogger(__name__)


def _normalize_body(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8")
    if isinstance(body, str):
        return body
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _body_hash(body: Any) -> str:
    normalized = _normalize_body(body)
    if normalized == "":
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_timestamp(timestamp: Optional[str]) -> str:
    if timestamp is None:
        return str(int(time.time()))

    raw = str(timestamp).strip()
    try:
        normalized = str(int(float(raw)))
    except (TypeError, ValueError):
        return raw

    if raw != normalized:
        logger.debug("Normalized Bayse X-Timestamp from %s to %s", raw, normalized)
    return normalized


def build_canonical_request(method: str, path: str, timestamp: str, body: Any = None) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{timestamp}.{method.upper()}.{normalized_path}.{_body_hash(body)}"


def sign_hmac_sha256(secret: str, message: str, output: str = "hex") -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    if output == "base64":
        return base64.b64encode(digest).decode("utf-8")
    return digest.hex()


@dataclasses.dataclass(frozen=True)
class BayseAuth:
    api_key: str
    api_secret: str
    api_key_header: str = "X-Public-Key"
    timestamp_header: str = "X-Timestamp"
    signature_header: str = "X-Signature"
    signature_encoding: str = "base64"

    def sign(
        self,
        method: str,
        path: str,
        body: Any = None,
        timestamp: Optional[str] = None,
    ) -> MutableMapping[str, str]:
        if not self.api_secret or not self.api_secret.strip():
            raise ValueError("Bayse secret key is required for request signing")

        ts = _normalize_timestamp(timestamp)
        normalized_path = path if path.startswith("/") else f"/{path}"
        body_hash = _body_hash(body)
        canonical_request = f"{ts}.{method.upper()}.{normalized_path}.{body_hash}"

        logger.debug("Bayse X-Timestamp=%s", ts)
        logger.debug("Bayse body_hash=%s", body_hash)
        logger.debug("Bayse string_to_sign=%s", canonical_request)

        signature = sign_hmac_sha256(self.api_secret, canonical_request, output=self.signature_encoding)
        logger.debug("Bayse X-Signature=%s", signature)

        return {
            self.api_key_header: self.api_key,
            self.timestamp_header: ts,
            self.signature_header: signature,
        }
