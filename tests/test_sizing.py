"""Position sizing — the arithmetic that decides how much money is at risk."""
from __future__ import annotations

import pytest

from src.config import Settings
from src.models import Signal
from src.sizing import calculate_size


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test",
        risk_per_trade_pct=1.0,
        max_position_pct=50.0,
        _env_file=None,
    )


def make_signal(entry: float, stop: float, target: float, **kw) -> Signal:
    return Signal(
        symbol=kw.pop("symbol", "AAPL"),
        direction=kw.pop("direction", "long"),
        entry=entry,
        stop=stop,
        target=target,
        confidence=0.8,
        reasoning="test",
        **kw,
    )


def test_size_comes_from_stop_distance(settings):
    # $10,000 account, 1% risk = $100. Stop $2 away -> 50 shares.
    sizing = calculate_size(make_signal(100.0, 98.0, 106.0), 10_000.0, settings)
    assert sizing.shares == 50
    assert sizing.dollar_risk == pytest.approx(100.0)
    assert sizing.account_risk_pct == pytest.approx(1.0)


def test_wider_stop_means_smaller_position(settings):
    # Both stops are wide enough that the notional cap doesn't bind.
    tight = calculate_size(make_signal(100.0, 97.0, 109.0), 10_000.0, settings)
    wide = calculate_size(make_signal(100.0, 94.0, 118.0), 10_000.0, settings)

    assert tight.shares > wide.shares
    # ...but the dollars at risk are the same, which is the whole point.
    assert tight.dollar_risk == pytest.approx(wide.dollar_risk, rel=0.05)


def test_position_cap_binds_before_risk_budget(settings):
    # A 10-cent stop would otherwise justify 10,000 shares of a $100 stock.
    sizing = calculate_size(make_signal(100.0, 99.90, 101.0), 10_000.0, settings)

    assert sizing.notional <= 10_000.0 * 0.50 + 1e-6
    assert "max position size" in sizing.capped_by
    # Capping size also caps risk below the budget — never above it.
    assert sizing.account_risk_pct < 1.0


def test_implied_min_stop_documents_where_the_cap_bites(settings):
    """1% risk against a 50% position cap starts trimming below a 2% stop."""
    assert settings.implied_min_stop_pct == pytest.approx(2.0)

    uncapped = calculate_size(make_signal(100.0, 97.5, 110.0), 10_000.0, settings)
    capped = calculate_size(make_signal(100.0, 99.0, 104.0), 10_000.0, settings)

    assert uncapped.capped_by == ""
    assert uncapped.account_risk_pct == pytest.approx(1.0)
    assert "max position size" in capped.capped_by


def test_buying_power_caps_size(settings):
    sizing = calculate_size(
        make_signal(100.0, 98.0, 106.0), 100_000.0, settings, buying_power=1_000.0
    )
    assert sizing.notional <= 1_000.0 + 1e-6
    assert sizing.capped_by == "available buying power"


def test_equities_round_down_to_whole_shares(settings):
    sizing = calculate_size(make_signal(33.33, 30.0, 40.0), 10_000.0, settings)
    assert sizing.shares == int(sizing.shares)


def test_crypto_sizes_fractionally(settings):
    signal = make_signal(50_000.0, 48_000.0, 56_000.0, symbol="BTC/USD", asset_class="crypto")
    sizing = calculate_size(signal, 10_000.0, settings)

    assert 0 < sizing.shares < 1
    assert sizing.dollar_risk == pytest.approx(100.0, rel=1e-3)


def test_zero_stop_distance_is_unsizeable(settings):
    sizing = calculate_size(make_signal(100.0, 100.0, 110.0), 10_000.0, settings)
    assert sizing.shares == 0
    assert sizing.capped_by == "invalid inputs"


def test_account_too_small_for_the_stop(settings):
    # $100 account, 1% = $1 budget, $5 stop distance -> less than one share.
    sizing = calculate_size(make_signal(100.0, 95.0, 115.0), 100.0, settings)
    assert sizing.shares == 0
