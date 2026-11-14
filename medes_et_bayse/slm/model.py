"""Runtime for the Bayse prediction SLM (CPU, numpy only + optional model2vec encoder).

Design: a small static-embedding language model (model2vec ``potion-base-8M``, a
distilled MiniLM-class sentence encoder, ~30 MB, no torch) embeds the market question;
the embedding is projected to a few PCA components and combined with leakage-safe market
features in an L2-regularised logistic head trained on resolved Bayse markets. The head is
stored as JSON (``artifacts/bayse_slm.json``). If the encoder cannot be loaded (offline,
package missing) the artifact's text-free head is used, so the model always works standalone. Which head is the default is decided on a
time-based validation split during training (``shipped_head`` in the artifact).
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .features import MarketSnapshot, numeric_features, sigmoid

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT = Path(__file__).with_name("artifacts") / "bayse_slm.json"
ENCODER_NAME = "minishlab/potion-base-8M"


@lru_cache(maxsize=2)
def load_encoder(name: str = ENCODER_NAME) -> Any:
    from model2vec import StaticModel  # optional dependency

    return StaticModel.from_pretrained(name)


class BayseSLM:
    def __init__(self, artifact: dict[str, Any], *, use_text: Optional[bool] = None) -> None:
        """use_text=None follows the artifact's validated choice (``shipped_head``); env SLM_USE_TEXT overrides."""
        self.artifact = artifact
        self.version = artifact.get("version", "unknown")
        if use_text is None:
            env = os.getenv("SLM_USE_TEXT", "").strip().lower()
            use_text = env in {"1", "true", "yes"} if env else artifact.get("shipped_head") == "text"
        if os.getenv("SLM_DISABLE_TEXT", "").strip().lower() in {"1", "true", "yes"}:
            use_text = False
        self.encoder = None
        if use_text and artifact.get("text_head") is not None:
            try:
                self.encoder = load_encoder(artifact.get("encoder", ENCODER_NAME))
            except Exception as exc:  # pragma: no cover - depends on network / optional package
                logger.warning("SLM text encoder unavailable (%s); using text-free head", exc)
        self.head = artifact["text_head"] if self.encoder is not None else artifact["numeric_head"]

    @classmethod
    def load(cls, path: str | Path | None = None, **kwargs: Any) -> "BayseSLM":
        path = Path(path or os.getenv("SLM_ARTIFACT_PATH") or DEFAULT_ARTIFACT)
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh), **kwargs)

    @property
    def uses_text(self) -> bool:
        return self.encoder is not None

    def _text_features(self, texts: list[str]) -> np.ndarray:
        head = self.head
        emb = np.asarray(self.encoder.encode(texts), dtype=np.float64)
        mean = np.asarray(head["pca_mean"]); comps = np.asarray(head["pca_components"])
        return (emb - mean) @ comps.T

    def predict_proba(self, snapshots: list[MarketSnapshot]) -> list[float]:
        if not snapshots:
            return []
        head = self.head
        X = np.asarray([numeric_features(s) for s in snapshots], dtype=np.float64)
        if self.encoder is not None:
            X = np.hstack([X, self._text_features([s.text for s in snapshots])])
        X = (X - np.asarray(head["mu"])) / np.asarray(head["sigma"])
        z = X @ np.asarray(head["coef"]) + float(head["intercept"])
        return [float(min(0.99, max(0.01, sigmoid(v)))) for v in z]

    def predict_one(self, snapshot: MarketSnapshot) -> float:
        return self.predict_proba([snapshot])[0]
