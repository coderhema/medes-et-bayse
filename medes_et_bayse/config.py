from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional


@dataclass(frozen=True)
class BayseRuntimeConfig:
    public_key: str
    secret_key: str
    user_id: str
    bayse_email: str = "opedepodesolu@gmail.com"
    base_url: str = "https://relay.bayse.markets"
    poke_api_key: Optional[str] = None


DEFAULT_BAYSE_PUBLIC_KEY = "pk_live_wuy17mUpR_MMxUp6Z4qDzSJw"
DEFAULT_BAYSE_SECRET_KEY = "sk_live_sGF6wMlbK_0-KT83QIEkOqRP_OExSHe7WHngZQXzSZWsYWFc"
DEFAULT_BAYSE_USER_ID = "5310fdaa-e06e-4501-b1a3-423639a71043"


def _env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def load_runtime_config() -> BayseRuntimeConfig:
    return BayseRuntimeConfig(
        public_key=_env_value("BAYSE_API_KEY", "BAYSE_PUBLIC_KEY", default=DEFAULT_BAYSE_PUBLIC_KEY),
        secret_key=_env_value("BAYSE_API_SECRET", "BAYSE_SECRET_KEY", default=DEFAULT_BAYSE_SECRET_KEY),
        user_id=os.getenv("BAYSE_USER_ID", DEFAULT_BAYSE_USER_ID),
        bayse_email=os.getenv("BAYSE_EMAIL", "opedepodesolu@gmail.com"),
        base_url=os.getenv("BAYSE_BASE_URL", "https://relay.bayse.markets"),
        poke_api_key=os.getenv("POKE_API_KEY"),
    )


runtime_config = load_runtime_config()


def build_client(config: BayseRuntimeConfig = runtime_config):
    from .client import BayseClient

    return BayseClient(
        api_key=config.public_key,
        api_secret=config.secret_key,
        user_id=config.user_id,
        base_url=config.base_url,
    )
