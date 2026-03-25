"""Bayesian belief updating utilities.

The BayesianEstimator provides a principled way to estimate the true
probability of an event, starting from the market-implied price as a
prior and updating with any available signals.

Formula (Beta-Binomial conjugate model):
  Prior: Beta(alpha, beta) with mean = alpha / (alpha + beta)
  Update: For each 'yes' signal, alpha += strength; for 'no', beta += strength
  Posterior mean = (alpha + data_yes) / (alpha + beta + n)

In production, signals could include:
  - News sentiment score (positive -> higher yes probability)
  - Historical resolution accuracy of the market creator
  - Volume-weighted price momentum
  - External prediction market consensus (Polymarket, Kalshi)
"""

import numpy as np
from scipy.stats import beta as beta_dist


class BayesianEstimator:
    """Bayesian probability estimator using Beta-Binomial conjugate."""

    def __init__(self, prior_strength: float = 10.0):
        """
        Args:
            prior_strength: Total prior weight. Higher = more weight on
                            market price, less on external signals.
        """
        self.prior_strength = prior_strength

    def estimate(
        self,
        market_implied_prob: float,
        event: dict,
        external_signals: dict | None = None,
    ) -> float:
        """Estimate true probability given market price and optional signals.

        Args:
            market_implied_prob: The current YES share price (0-1).
            event: Event dict from the Bayse API.
            external_signals: Optional dict of signal_name -> probability.
                              e.g. {"polymarket": 0.62, "news_sentiment": 0.65}

        Returns:
            Our posterior estimate of P(YES).
        """
        # Set prior from market price
        alpha = self.prior_strength * market_implied_prob
        beta = self.prior_strength * (1 - market_implied_prob)

        # Incorporate external signals with lower weight
        if external_signals:
            signal_weight = 2.0  # Each signal counts for 2 pseudo-observations
            for signal_prob in external_signals.values():
                if 0 < signal_prob < 1:
                    alpha += signal_weight * signal_prob
                    beta += signal_weight * (1 - signal_prob)

        # Posterior mean of Beta(alpha, beta)
        posterior_mean = alpha / (alpha + beta)

        # Volume signal: high-volume markets are more efficient, trust market more
        volume = float(event.get("volume") or event.get("totalVolume") or 0)
        if volume > 5000:  # Highly liquid market -> shrink toward market price
            shrinkage = min(0.8, volume / 20000)
            posterior_mean = (
                shrinkage * market_implied_prob
                + (1 - shrinkage) * posterior_mean
            )

        return float(np.clip(posterior_mean, 0.01, 0.99))

    def credible_interval(
        self,
        market_implied_prob: float,
        confidence: float = 0.90,
    ) -> tuple[float, float]:
        """Return a credible interval for the true probability.

        Args:
            market_implied_prob: Market's current implied probability.
            confidence: Width of the interval (e.g., 0.90 = 90% CI).

        Returns:
            (lower, upper) bounds.
        """
        alpha = self.prior_strength * market_implied_prob
        beta_param = self.prior_strength * (1 - market_implied_prob)
        lower = beta_dist.ppf((1 - confidence) / 2, alpha, beta_param)
        upper = beta_dist.ppf(1 - (1 - confidence) / 2, alpha, beta_param)
        return float(lower), float(upper)
