"""
Position sizing, calculated from stop distance rather than gut feel.

    risk_dollars = equity * risk_per_trade_pct / 100
    shares       = risk_dollars / abs(entry - stop)

That is the whole idea: the stop decides the size, so a wide stop gives a small
position and a tight stop gives a larger one, and the dollar loss if you are
wrong is the same either way.

Three caps sit on top, and whichever binds first is recorded so the alert can
say why the size is what it is.

Spread betting (IG) uses the identical formula, and that is not a coincidence:

    stake_per_point = risk_budget / stop_distance_in_points

is the same division. What changes is everything around it. The result is a
stake in currency-per-point rather than a share count, nothing is "bought" so
the cash cap is meaningless, exposure is geared and routinely exceeds equity,
and IG enforces a minimum stake. Those differences are handled in one branch
below rather than by a second sizing function, so there is only ever one place
where the 1%-risk rule lives.
"""
from __future__ import annotations

import math

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
    unit = settings.size_unit
    risk_per_share = signal.risk_per_share
    if risk_per_share <= 0 or equity <= 0 or signal.entry <= 0:
        return PositionSizing(0, 0, risk_per_share, 0, 0, capped_by="invalid inputs", unit=unit)

    risk_budget = equity * settings.risk_per_trade_pct / 100.0
    raw_shares = risk_budget / risk_per_share

    if settings.spread_betting:
        return _size_spread_bet(signal, equity, settings, raw_shares, risk_per_share, buying_power)

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
        unit=unit,
    )


def _size_spread_bet(
    signal: Signal,
    equity: float,
    settings: Settings,
    raw_stake: float,
    risk_per_point: float,
    buying_power: float | None,
) -> PositionSizing:
    """
    The IG branch: `raw_stake` is already currency-per-point.

    Note what is NOT here: a cash cap. A spread bet costs margin, not the full
    exposure, so capping the stake by cash on hand would be wrong by a factor
    of five or more. The gearing cap does that job instead, and IG rejects the
    deal outright if the margin genuinely is not there.
    """
    unit = settings.size_unit
    capped_by = ""

    # Cap 1 — gearing. Exposure is stake x price, and may exceed equity, but
    # not without limit.
    max_exposure = equity * settings.ig_max_exposure_pct / 100.0
    if raw_stake * signal.entry > max_exposure:
        raw_stake = max_exposure / signal.entry
        capped_by = f"gearing cap ({settings.ig_max_exposure_pct:g}% of equity in exposure)"

    # Cap 2 — approximate margin. A cheap early no, so an obviously unfundable
    # stake is not sent to IG just to be refused.
    if buying_power is not None and buying_power > 0:
        affordable = buying_power / (settings.ig_margin_pct / 100.0) / signal.entry
        if raw_stake > affordable:
            raw_stake = affordable
            capped_by = (
                f"available margin (~{settings.ig_margin_pct:g}% deposit assumed; "
                "IG's real requirement varies by market)"
            )

    # IG quotes stakes to 2dp. Round DOWN, so rounding can only reduce risk.
    stake = math.floor(raw_stake * 100) / 100.0

    if stake < settings.ig_min_deal_size:
        return PositionSizing(
            shares=0.0,
            dollar_risk=0.0,
            risk_per_share=risk_per_point,
            notional=0.0,
            account_risk_pct=0.0,
            capped_by=(
                f"stake {stake:g} is below IG's {settings.ig_min_deal_size:g} "
                f"{unit} minimum — the stop is too wide to risk only "
                f"{settings.risk_per_trade_pct:g}% at this account size"
            ),
            unit=unit,
        )

    currency_risk = stake * risk_per_point
    return PositionSizing(
        shares=stake,
        dollar_risk=currency_risk,
        risk_per_share=risk_per_point,
        notional=stake * signal.entry,
        account_risk_pct=(currency_risk / equity * 100.0) if equity else 0.0,
        capped_by=capped_by,
        unit=unit,
    )
