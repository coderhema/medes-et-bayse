from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BayseRuntimeConfig:
    public_key: str
    secret_key: str
    user_id: str
    bayse_email: str = 'opedepodesolu@gmail.com'
    base_url: str = 'https://relay.bayse.markets'
    poke_api_key: Optional[str] = None


def _mask_env_value(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) <= 6:
        return f"{trimmed[:3]}***{trimmed[-3:]}"
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


def load_runtime_config() -> BayseRuntimeConfig:
    return BayseRuntimeConfig(
        public_key=_env_value('BAYSE_API_KEY', 'BAYSE_PUBLIC_KEY', required=True),
        secret_key=_env_value('BAYSE_API_SECRET', 'BAYSE_SECRET_KEY', required=True),
        user_id=_env_value('BAYSE_USER_ID', default='5310fdaa-e06e-4501-b1a3-423639a71043'),
        bayse_email=_env_value('BAYSE_EMAIL', default='opedepodesolu@gmail.com'),
        base_url=_env_value('BAYSE_BASE_URL', default='https://relay.bayse.markets'),
        poke_api_key=_env_value('POKE_API', 'POKE_API_KEY'),
    )


runtime_config = load_runtime_config()


def build_client(config: BayseRuntimeConfig = runtime_config):
    from .client import BayseClient

    if not config.public_key or not config.public_key.strip():
        raise ValueError('Bayse public key is required')
    if not config.secret_key or not config.secret_key.strip():
        raise ValueError('Bayse secret key is required')

    return BayseClient(
        api_key=config.public_key,
        api_secret=config.secret_key,
        user_id=config.user_id,
        base_url=config.base_url,
    )
