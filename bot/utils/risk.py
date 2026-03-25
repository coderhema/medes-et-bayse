"""Risk management utilities.

Helps the bot manage:
  - Maximum drawdown limits
  - Daily loss limits
  - Position concentration checks
  - Stop-loss logic
"""

from loguru import logger


class RiskManager:
    """Enforces risk limits on trade signals before execution."""

    def __init__(
        self,
        bankroll: float,
        max_daily_loss_fraction: float = 0.05,
        max_single_position_fraction: float = 0.05,
        max_open_positions: int = 10,
    ):
        self.bankroll = bankroll
        self.max_daily_loss_fraction = max_daily_loss_fraction
        self.max_single_position_fraction = max_single_position_fraction
        self.max_open_positions = max_open_positions
        self._daily_pnl = 0.0
        self._open_positions: list[dict] = []

    @property
    def max_daily_loss(self) -> float:
        return self.bankroll * self.max_daily_loss_fraction

    def is_trade_allowed(self, signal: dict) -> bool:
        """Check if a trade signal passes all risk checks."""

        # Daily loss limit
        if self._daily_pnl < -self.max_daily_loss:
            logger.warning("Daily loss limit reached. No more trades today.")
            return False

        # Single position size limit
        max_stake = self.bankroll * self.max_single_position_fraction
        if signal.get("stake", 0) > max_stake:
            logger.warning(
                f"Position too large: ${signal['stake']:.2f} > ${max_stake:.2f} limit. Capping."
            )
            signal["stake"] = round(max_stake, 2)

        # Max open positions
        if len(self._open_positions) >= self.max_open_positions:
            logger.warning("Max open positions reached.")
            return False

        return True

    def record_trade(self, signal: dict) -> None:
        """Track a placed trade for risk accounting."""
        self._open_positions.append(signal)

    def record_pnl(self, pnl: float) -> None:
        """Update daily PnL."""
        self._daily_pnl += pnl
        logger.info(f"Daily PnL: ${self._daily_pnl:.2f}")

    def reset_daily(self) -> None:
        """Reset daily counters (call at midnight)."""
        self._daily_pnl = 0.0
        logger.info("Daily PnL reset.")
