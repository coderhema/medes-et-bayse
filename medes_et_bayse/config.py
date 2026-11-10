from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BayseRuntimeConfig:
    public_key: str = ''
    secret_key: str = ''
    user_id: str = ''
    bayse_email: str = ''
    base_url: str = 'https://relay.bayse.markets'
    poke_api_key: Optional[str] = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.public_key.strip() and self.secret_key.strip())


def _mask_env_value(value: str) -> str:
    trimmed = value.strip()
    return f"{trimmed[:3]}***{trimmed[-3:]}"


def _env_value(*names: str, required: bool = False, default: str = '') -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != '':
            logger.debug('Loaded %s from env (%s)', name, _mask_env_value(value))
            return value
    if required:
        raise RuntimeError(f"Missing required environment variable: {names[0]}")
    return default


def load_runtime_config(*, require_credentials: bool = False) -> BayseRuntimeConfig:
    """Read Bayse settings from the environment.

    Importing the package never needs real keys (public market data works without them);
    pass require_credentials=True, or call build_client(), when signed requests are needed.
    """
    return BayseRuntimeConfig(
        public_key=_env_value('BAYSE_PUBLIC_KEY', required=require_credentials),
        secret_key=_env_value('BAYSE_SECRET_KEY', required=require_credentials),
        user_id=_env_value('BAYSE_USER_ID'),
        bayse_email=_env_value('BAYSE_EMAIL'),
        base_url=_env_value('BAYSE_API_URL', default='https://relay.bayse.markets'),
        poke_api_key=_env_value('POKE_API_KEY') or None,
    )


runtime_config = load_runtime_config()


def build_client(config: Optional[BayseRuntimeConfig] = None, *, require_credentials: bool = True):
    from .client import BayseClient

    config = config or load_runtime_config()
    if require_credentials:
        if not config.public_key or not config.public_key.strip():
            raise ValueError('Bayse public key is required (set BAYSE_PUBLIC_KEY)')
        if not config.secret_key or not config.secret_key.strip():
            raise ValueError('Bayse secret key is required (set BAYSE_SECRET_KEY)')

    return BayseClient(
        api_key=config.public_key,
        api_secret=config.secret_key,
        user_id=config.user_id or None,
        base_url=config.base_url,
    )
