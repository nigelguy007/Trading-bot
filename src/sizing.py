"""
Position sizing, calculated from stop distance rather than gut feel.

    risk_dollars = equity * risk_per_trade_pct / 100
    shares       = risk_dollars / abs(entry - stop)

That is the whole idea: the stop decides the size, so a wide stop gives a small
position and a tight stop gives a larger one, and the dollar loss if you are
wrong is the same either way.

Three caps sit on top, and whichever binds first is recorded so the alert can
say why the size is what it is.
"""
from __future__ import annotations

from src.config import Settings
from src.models import PositionSizing, Signal


def calculate_size(
    signal: Signal,
    equity: float,
    settings: Settings,
    *,
    buying_power: float | None = None,
    allow_fractional: bool | None = None,
) -> PositionSizing:
    """Size a signal against account equity. Returns 0 shares if it can't be sized."""
    risk_per_share = signal.risk_per_share
    if risk_per_share <= 0 or equity <= 0 or signal.entry <= 0:
        return PositionSizing(0, 0, risk_per_share, 0, 0, capped_by="invalid inputs")

    risk_budget = equity * settings.risk_per_trade_pct / 100.0
    raw_shares = risk_budget / risk_per_share
    capped_by = ""

    # Cap 1 — no single position may dominate the account, however tight the stop.
    max_notional = equity * settings.max_position_pct / 100.0
    if raw_shares * signal.entry > max_notional:
        raw_shares = max_notional / signal.entry
        capped_by = f"max position size ({settings.max_position_pct:g}% of equity)"

    # Cap 2 — can't spend money that isn't there.
    if buying_power is not None and raw_shares * signal.entry > buying_power:
        raw_shares = buying_power / signal.entry
        capped_by = "available buying power"

    # Cap 3 — crypto trades fractionally, equities round down to whole shares.
    fractional = allow_fractional if allow_fractional is not None else signal.asset_class == "crypto"
    shares = round(raw_shares, 6) if fractional else float(int(raw_shares))

    dollar_risk = shares * risk_per_share
    return PositionSizing(
        shares=shares,
        dollar_risk=dollar_risk,
        risk_per_share=risk_per_share,
        notional=shares * signal.entry,
        account_risk_pct=(dollar_risk / equity * 100.0) if equity else 0.0,
        capped_by=capped_by,
    )
