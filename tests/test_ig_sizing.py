"""
Sizing on a spread betting account.

The arithmetic is the same division as for shares, which is the point — but the
result means something different, and getting the difference wrong is how a
"1% risk" trade becomes a 3x geared one. These tests pin the differences:
a stake per point rather than a share count, exposure allowed above equity,
no cash cap, rounding that can only reduce risk, and IG's minimum stake.
"""
from __future__ import annotations

import pytest

from src.config import Settings
from src.sizing import calculate_size

from tests.test_ig_broker import a_signal


def ig_settings(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("ig_api_key", "igkey123")
    kw.setdefault("ig_username", "tester")
    kw.setdefault("ig_password", "hunter2")
    return Settings(_env_file=None, **kw)


def share_settings(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("alpaca_api_key", "PKREAL")
    kw.setdefault("alpaca_secret_key", "SECRET")
    return Settings(_env_file=None, **kw)


# ---------------------------------------------------------------------- #
def test_the_stake_is_the_risk_budget_divided_by_the_stop_distance():
    """
    £10,000 equity, 1% risk = £100. A 40-point stop means £2.50 a point:
    40 points against you is exactly the £100 you budgeted.
    """
    s = ig_settings()
    signal = a_signal(entry=8000, stop=7960, target=8100)  # 40 points
    sizing = calculate_size(signal, 10_000.0, s)

    assert sizing.shares == pytest.approx(2.5)
    assert sizing.unit == "GBP/point"
    assert sizing.size_label == "2.5 GBP/point"
    assert sizing.dollar_risk == pytest.approx(100.0)
    assert sizing.account_risk_pct == pytest.approx(1.0)


def test_a_wider_stop_gives_a_smaller_stake_for_the_same_risk():
    s = ig_settings()
    tight = calculate_size(a_signal(entry=8000, stop=7960, target=8100), 10_000.0, s)
    wide = calculate_size(a_signal(entry=8000, stop=7900, target=8300), 10_000.0, s)

    assert tight.shares > wide.shares
    # Both stops are wide enough to clear the gearing cap, so the money at
    # risk is the same either way. That is the whole idea.
    assert tight.dollar_risk == pytest.approx(wide.dollar_risk, rel=0.02)
    assert tight.capped_by == wide.capped_by == ""


def test_a_very_tight_stop_is_capped_by_gearing_and_the_setting_says_when():
    """
    The spread betting analogue of implied_min_stop_pct. At a 300% gearing cap
    and a 1% risk budget, anything tighter than a 0.33% stop gets trimmed and
    risks less than the configured 1% — safe, but worth knowing rather than
    discovering in the sizing output.
    """
    s = ig_settings(ig_max_exposure_pct=300.0, risk_per_trade_pct=1.0)
    assert s.implied_min_stop_pct == pytest.approx(0.3333, rel=1e-3)

    # A 20-point stop on an 8000 market is 0.25% — inside the threshold.
    tight = calculate_size(a_signal(entry=8000, stop=7980, target=8100), 10_000.0, s)
    assert "gearing cap" in tight.capped_by
    assert tight.account_risk_pct < 1.0


def test_the_unit_follows_the_account_currency():
    s = ig_settings(ig_currency="USD")
    assert calculate_size(a_signal(), 10_000.0, s).unit == "USD/point"


def test_exposure_is_allowed_above_equity_because_the_bet_is_geared():
    """
    The share-dealing MAX_POSITION_PCT cap would crush this: £2.50/point on an
    8000 index is £20,000 of exposure on a £10,000 account. That is what a
    spread bet is, and the gearing cap — not the position cap — governs it.
    """
    s = ig_settings(max_position_pct=50.0, ig_max_exposure_pct=300.0)
    sizing = calculate_size(a_signal(entry=8000, stop=7960, target=8100), 10_000.0, s)

    assert sizing.shares == pytest.approx(2.5)
    assert sizing.notional == pytest.approx(20_000.0)  # 200% of equity
    assert sizing.capped_by == ""


def test_the_gearing_cap_does_bind_eventually():
    s = ig_settings(ig_max_exposure_pct=100.0)
    sizing = calculate_size(a_signal(entry=8000, stop=7960, target=8100), 10_000.0, s)

    assert sizing.notional == pytest.approx(10_000.0)
    assert "gearing cap" in sizing.capped_by
    # Capped size risks LESS than the budget, never more.
    assert sizing.account_risk_pct < 1.0


def test_cash_on_hand_does_not_cap_a_spread_bet():
    """
    A share order spends its full notional; a spread bet posts margin. Capping
    the stake by available cash would undersize every trade by the gearing
    factor.
    """
    s = ig_settings(ig_margin_pct=20.0)
    signal = a_signal(entry=8000, stop=7960, target=8100)
    # £5,000 of margin at a 20% deposit factor supports £25,000 of exposure,
    # comfortably more than the £20,000 this stake needs.
    sizing = calculate_size(signal, 10_000.0, s, buying_power=5_000.0)

    assert sizing.shares == pytest.approx(2.5)
    assert sizing.capped_by == ""


def test_an_obviously_unaffordable_stake_is_trimmed_before_ig_sees_it():
    s = ig_settings(ig_margin_pct=20.0)
    signal = a_signal(entry=8000, stop=7960, target=8100)
    sizing = calculate_size(signal, 10_000.0, s, buying_power=800.0)

    # £800 margin / 20% = £4,000 exposure / 8000 = 0.5 a point.
    assert sizing.shares == pytest.approx(0.5)
    assert "available margin" in sizing.capped_by


def test_the_stake_rounds_down_to_two_decimals():
    """Rounding must only ever reduce risk, so it floors rather than rounds."""
    s = ig_settings()
    # 1% of 10,000 = 100; a 33-point stop gives 3.0303... a point.
    sizing = calculate_size(a_signal(entry=8000, stop=7967, target=8100), 10_000.0, s)

    assert sizing.shares == 3.03
    assert sizing.dollar_risk < 100.0


def test_a_stake_below_igs_minimum_is_refused_with_an_explanation():
    """
    A small account with a wide stop cannot bet 0.1 a point — IG will not take
    it. Returning zero is correct; returning the minimum instead would
    silently risk several times the budget.
    """
    s = ig_settings(ig_min_deal_size=0.5)
    # 1% of £500 = £5 over a 200-point stop = 0.025 a point.
    sizing = calculate_size(a_signal(entry=8000, stop=7800, target=8400), 500.0, s)

    assert sizing.shares == 0.0
    assert sizing.dollar_risk == 0.0
    assert "below IG's 0.5" in sizing.capped_by
    assert "stop is too wide" in sizing.capped_by


def test_the_minimum_is_configurable_per_market():
    s = ig_settings(ig_min_deal_size=5.0)
    sizing = calculate_size(a_signal(entry=8000, stop=7960, target=8100), 10_000.0, s)
    assert sizing.shares == 0.0
    assert "below IG's 5" in sizing.capped_by


def test_invalid_inputs_still_carry_the_right_unit():
    s = ig_settings()
    sizing = calculate_size(a_signal(entry=8000, stop=8000, target=8100), 10_000.0, s)
    assert sizing.shares == 0
    assert sizing.unit == "GBP/point"


def test_the_unit_survives_the_approval_round_trip(tmp_path):
    """
    An approval is written to JSON and read back minutes later. If `unit` were
    dropped there, a £2.50-a-point stake would come back labelled "2.5 shares"
    on the very screen where you press Approve.
    """
    from src.approvals import ApprovalStore, signal_from_entry

    s = ig_settings()
    signal = a_signal(entry=8000, stop=7960, target=8100)
    sizing = calculate_size(signal, 10_000.0, s)

    store = ApprovalStore(tmp_path / "approvals.json")
    store.add(signal, sizing)

    reloaded = ApprovalStore(tmp_path / "approvals.json")
    _, restored = signal_from_entry(reloaded.resolve(signal.id, "approved"))

    assert restored.unit == "GBP/point"
    assert restored.size_label == "2.5 GBP/point"


def test_an_older_approval_without_a_unit_still_loads(tmp_path):
    """Approvals written before the field existed must not crash on read."""
    import json

    from src.approvals import ApprovalStore, signal_from_entry

    signal = a_signal()
    path = tmp_path / "approvals.json"
    path.write_text(
        json.dumps(
            {
                signal.id: {
                    "signal": signal.to_dict(),
                    "sizing": {
                        "shares": 50.0,
                        "dollar_risk": 100.0,
                        "risk_per_share": 2.0,
                        "notional": 5000.0,
                        "account_risk_pct": 1.0,
                        "capped_by": "",
                    },
                    "created_at": "2099-01-01T00:00:00+00:00",
                    "status": "pending",
                }
            }
        )
    )
    _, restored = signal_from_entry(ApprovalStore(path).resolve(signal.id, "approved"))
    assert restored.unit == "shares"


def test_the_alert_names_the_unit_and_calls_exposure_what_it_is():
    from src.alerts.formatter import signal_text
    from src.models import RiskDecision

    s = ig_settings()
    signal = a_signal(entry=8000, stop=7960, target=8100)
    sizing = calculate_size(signal, 10_000.0, s)
    text = signal_text(signal, sizing, RiskDecision(approved=True), paper=True)

    assert "2.5 GBP/point" in text
    assert "Exposure 20,000.00" in text
    assert "LEVERAGED" in text
    # No dollar signs on a sterling account.
    assert "$" not in text
    assert "/point)" in text  # the stop distance is per point, not per share


def test_a_share_alert_is_unchanged():
    from src.alerts.formatter import signal_text
    from src.models import RiskDecision

    s = share_settings()
    signal = a_signal(symbol="AAPL", entry=100, stop=98, target=106)
    sizing = calculate_size(signal, 10_000.0, s)
    text = signal_text(signal, sizing, RiskDecision(approved=True), paper=True)

    assert "50 shares" in text
    assert "Notional $5,000.00" in text
    assert "LEVERAGED" not in text
    assert "/share)" in text


# ---------------------------------------------------------------------- #
def test_share_accounts_are_untouched_by_any_of_this():
    """The IG branch must not leak into share dealing."""
    s = share_settings()
    sizing = calculate_size(a_signal(entry=100, stop=98, target=106), 10_000.0, s)

    assert sizing.unit == "shares"
    assert sizing.shares == 50  # £100 risk / £2 stop, whole shares
    assert sizing.size_label == "50 shares"


def test_broker_override_switches_the_sizing_unit():
    """BROKER=ig with keys present is what flips the unit, nothing implicit."""
    both = dict(
        anthropic_api_key="t",
        alpaca_api_key="PKREAL",
        alpaca_secret_key="SECRET",
        ig_api_key="k",
        ig_username="u",
        ig_password="p",
    )
    assert Settings(_env_file=None, broker="ig", **both).size_unit == "GBP/point"
    assert Settings(_env_file=None, broker="alpaca", **both).size_unit == "shares"
    # auto prefers IG when its keys are there.
    assert Settings(_env_file=None, **both).active_broker == "ig"
