"""Performance metrics."""
from __future__ import annotations

import pytest

from src.metrics import MIN_MEANINGFUL_TRADES, compute_metrics


def trade(pnl: float, r: float, strategy: str = "trend_breakout") -> dict:
    return {
        "status": "closed",
        "pnl": pnl,
        "r_multiple": r,
        "strategy": strategy,
        "symbol": "AAPL",
    }


def test_no_trades_is_all_zeros():
    m = compute_metrics([])
    assert m.total_trades == 0
    assert m.summary() == "No closed trades yet."


def test_open_trades_are_excluded():
    trades = [trade(100, 1.0), {"status": "open", "pnl": None, "r_multiple": None}]
    assert compute_metrics(trades).total_trades == 1


def test_win_rate_and_pnl():
    m = compute_metrics([trade(200, 2.0), trade(-100, -1.0), trade(150, 1.5), trade(-100, -1.0)])
    assert m.total_trades == 4
    assert m.wins == 2 and m.losses == 2
    assert m.win_rate == pytest.approx(50.0)
    assert m.total_pnl == pytest.approx(150.0)


def test_profit_factor():
    # 350 gross profit / 200 gross loss.
    m = compute_metrics([trade(200, 2.0), trade(150, 1.5), trade(-100, -1.0), trade(-100, -1.0)])
    assert m.profit_factor == pytest.approx(1.75)


def test_profit_factor_is_not_infinite_on_a_thin_sample():
    """No losers yet is an unfinished sample, not an infinite edge."""
    m = compute_metrics([trade(100, 1.0), trade(100, 1.0)])
    assert m.profit_factor == 0.0
    assert not m.is_significant


def test_low_win_rate_can_still_be_profitable():
    """The reason average R matters more than win rate."""
    trades = [trade(-100, -1.0)] * 6 + [trade(300, 3.0)] * 4
    m = compute_metrics(trades)

    assert m.win_rate == pytest.approx(40.0)
    assert m.total_pnl > 0
    assert m.expectancy_r > 0


def test_significance_threshold():
    assert not compute_metrics([trade(100, 1.0)] * (MIN_MEANINGFUL_TRADES - 1)).is_significant
    assert compute_metrics([trade(100, 1.0)] * MIN_MEANINGFUL_TRADES).is_significant


def test_thin_samples_are_flagged_in_the_summary():
    assert "below the" in compute_metrics([trade(100, 1.0)] * 5).summary()


def test_max_drawdown_from_equity_curve():
    curve = [
        {"equity": 10_000},
        {"equity": 12_000},  # peak
        {"equity": 9_000},   # -25%
        {"equity": 11_000},
    ]
    m = compute_metrics([trade(100, 1.0)], curve)
    assert m.max_drawdown_pct == pytest.approx(25.0)


def test_breakdown_by_strategy():
    m = compute_metrics(
        [
            trade(100, 1.0, "trend_breakout"),
            trade(-50, -0.5, "trend_breakout"),
            trade(200, 2.0, "mean_reversion"),
        ]
    )
    assert m.by_strategy["trend_breakout"]["trades"] == 2
    assert m.by_strategy["mean_reversion"]["total_pnl"] == pytest.approx(200.0)


def test_metrics_serialise():
    payload = compute_metrics([trade(100, 1.0), trade(-50, -0.5)]).to_dict()
    assert payload["total_trades"] == 2
    assert "expectancy_r" in payload
