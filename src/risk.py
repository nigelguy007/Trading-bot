"""
The risk gate.

This module sits between the AI and the broker on purpose: the model never
talks to the money, the rules do. Every rule here is a hard reject, not a
score to be weighed against a strong-looking setup — that is the entire value
of putting it in code instead of leaving it to judgement at 3pm.

Rules enforced (guide §8):
  1. Every trade has a stop, set before entry, on the correct side.
  2. Risk per trade <= 1% of account.
  3. Reward must justify risk (min R:R).
  4. Daily loss limit -3%: stop trading for the day.
  5. Max drawdown -15%: pause and review, and stay paused across restarts.
  6. Position sizing from stop distance, never gut feel.
  7. No stacking correlated positions.
  8. Caps on open positions and total portfolio risk.
"""
from __future__ import annotations

from src.config import Settings
from src.logging_setup import get_logger
from src.models import RiskDecision, Signal
from src.portfolio import PortfolioManager, PortfolioState
from src.sizing import calculate_size

log = get_logger(__name__)


class RiskManager:
    def __init__(self, settings: Settings, portfolio: PortfolioManager) -> None:
        self.settings = settings
        self.portfolio = portfolio

    # ------------------------------------------------------------------ #
    # Portfolio-level circuit breakers — checked once per cycle, before any
    # symbol is analysed. If one trips, the whole cycle stops.
    # ------------------------------------------------------------------ #
    def check_circuit_breakers(self, state: PortfolioState) -> RiskDecision:
        s = self.settings

        if state.drawdown_pct >= s.max_drawdown_pct:
            reason = (
                f"MAX DRAWDOWN BREACHED: -{state.drawdown_pct:.2f}% from peak "
                f"${state.peak_equity:,.2f} (limit -{s.max_drawdown_pct:g}%). "
                "Trading is paused until you review the system and clear the pause "
                "with `python -m src.cli reset-drawdown`."
            )
            log.error("risk.max_drawdown", drawdown_pct=state.drawdown_pct)
            return RiskDecision(approved=False, reasons=[reason], halt=True)

        if state.day_pnl_pct <= -s.daily_loss_limit_pct:
            reason = (
                f"DAILY LOSS LIMIT HIT: {state.day_pnl_pct:.2f}% today "
                f"(limit -{s.daily_loss_limit_pct:g}%). No new trades until tomorrow."
            )
            log.warning("risk.daily_loss_limit", day_pnl_pct=state.day_pnl_pct)
            return RiskDecision(approved=False, reasons=[reason], halt=True)

        warnings = []
        if state.day_pnl_pct <= -(s.daily_loss_limit_pct * 0.66):
            warnings.append(
                f"Approaching the daily loss limit ({state.day_pnl_pct:.2f}% of "
                f"-{s.daily_loss_limit_pct:g}%)."
            )
        if state.drawdown_pct >= s.max_drawdown_pct * 0.66:
            warnings.append(
                f"Drawdown at -{state.drawdown_pct:.2f}% of a -{s.max_drawdown_pct:g}% limit."
            )
        return RiskDecision(approved=True, warnings=warnings)

    # ------------------------------------------------------------------ #
    # Per-signal evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, signal: Signal, state: PortfolioState) -> RiskDecision:
        s = self.settings
        reasons: list[str] = []
        warnings: list[str] = []

        # --- 1. Stop discipline ---
        if signal.stop <= 0:
            return RiskDecision(False, ["No stop loss. Every trade has one, set before entry."])
        if not signal.stop_is_coherent():
            return RiskDecision(
                False,
                [
                    f"Incoherent levels for a {signal.direction}: entry {signal.entry}, "
                    f"stop {signal.stop}, target {signal.target}."
                ],
            )
        if signal.risk_per_share <= 0:
            return RiskDecision(False, ["Stop equals entry — zero-distance stop."])

        # --- 2. Reward has to justify the risk ---
        if signal.risk_reward < s.min_risk_reward:
            reasons.append(
                f"R:R {signal.risk_reward:.2f} is below the {s.min_risk_reward:g} minimum."
            )

        # --- 3. Confidence floor ---
        if signal.confidence < s.min_confidence:
            reasons.append(
                f"Confidence {signal.confidence:.2f} is below the {s.min_confidence:g} floor."
            )

        # --- 4. Don't double up on something already held ---
        if signal.symbol in state.symbols:
            reasons.append(f"Already holding {signal.symbol}; not adding to it.")

        # --- 5. Position count cap ---
        if len(state.positions) >= s.max_open_positions:
            reasons.append(
                f"At the {s.max_open_positions}-position limit "
                f"({len(state.positions)} open)."
            )

        # --- 6. Correlation cap: don't stack 5 tech longs ---
        group = self.portfolio.group_for(signal.symbol)
        correlated = [p for p in state.positions if self.portfolio.group_for(p.symbol) == group]
        if len(correlated) >= s.max_correlated_positions:
            held = ", ".join(p.symbol for p in correlated)
            reasons.append(
                f"Already {len(correlated)} correlated position(s) in '{group}' ({held}); "
                f"limit is {s.max_correlated_positions}."
            )

        # --- 7. Sizing, from stop distance ---
        sizing = calculate_size(
            signal,
            state.account.equity,
            s,
            buying_power=state.account.buying_power,
        )
        if sizing.shares <= 0:
            reasons.append(
                "Position size rounds to zero — the stop is too wide for a "
                f"{s.risk_per_trade_pct:g}% risk budget at this account size."
            )
        elif sizing.account_risk_pct > s.risk_per_trade_pct + 1e-6:
            # Belt and braces: sizing should make this impossible.
            reasons.append(
                f"Computed risk {sizing.account_risk_pct:.2f}% exceeds the "
                f"{s.risk_per_trade_pct:g}% per-trade limit."
            )

        # --- 8. Total portfolio risk cap ---
        projected_risk_pct = state.open_risk_pct + (
            sizing.account_risk_pct if sizing.shares > 0 else 0.0
        )
        if projected_risk_pct > s.max_portfolio_risk_pct:
            reasons.append(
                f"Total open risk would reach {projected_risk_pct:.2f}%, over the "
                f"{s.max_portfolio_risk_pct:g}% portfolio cap."
            )

        # --- Non-blocking observations ---
        if sizing.capped_by:
            warnings.append(f"Size limited by {sizing.capped_by}.")
        if signal.news_impact == "high":
            warnings.append("High-impact news is live on this name — expect wider slippage.")
        if not signal.invalidation:
            warnings.append("No explicit invalidation levels were provided.")

        approved = not reasons
        log.info(
            "risk.evaluated",
            symbol=signal.symbol,
            approved=approved,
            shares=sizing.shares,
            risk_pct=round(sizing.account_risk_pct, 3),
            rr=round(signal.risk_reward, 2),
            reasons=reasons or None,
        )
        return RiskDecision(
            approved=approved,
            reasons=reasons,
            warnings=warnings,
            sizing=sizing if sizing.shares > 0 else None,
        )
