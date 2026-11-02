"""
`doctor`, the setup check.

It is the first thing anyone runs and the only place several
misconfigurations ever surface. Its branching was verified by hand across
five broker configurations while the multi-broker work went in; these pin
the cases where a silent wrong answer costs you something:

  * a route pointing at a broker that is not running (those symbols go
    somewhere else without a word),
  * Trading 212 with no price source (the bot scans nothing and looks like
    it simply found no setups),
  * a second broker that is read for risk but never traded,
  * and the leverage and no-bracket warnings, which must appear whether or
    not the venue is the primary.
"""
from __future__ import annotations

import pytest

from src.cli import _check_brokers
from src.config import Settings


def check(**kw) -> tuple[list, list, list]:
    """Run the broker section of doctor and return (ok, warn, bad)."""
    kw.setdefault("anthropic_api_key", "test")
    settings = Settings(_env_file=None, **kw)
    ok: list[str] = []
    warn: list[str] = []
    bad: list[str] = []
    _check_brokers(settings, ok, warn, bad)
    return ok, warn, bad


def joined(lines: list[str]) -> str:
    return " || ".join(lines)


IG = dict(ig_api_key="k", ig_username="u", ig_password="p")
T212 = dict(t212_api_key="tk")
ALPACA = dict(alpaca_api_key="PKREAL", alpaca_secret_key="SECRET")


# ---------------------------------------------------------------------- #
# Single broker
# ---------------------------------------------------------------------- #
def test_no_keys_says_it_will_use_the_simulator():
    ok, warn, bad = check()
    assert "simulator" in joined(warn)
    assert not bad


def test_alpaca_placeholders_are_called_out_as_absent():
    ok, warn, bad = check(alpaca_api_key="your_paper_key", alpaca_secret_key="your_paper_secret")
    assert "placeholder" in joined(warn)


def test_ig_always_warns_about_leverage():
    ok, warn, bad = check(**IG)
    assert "LEVERAGED" in joined(warn)
    assert "ig-spread-betting.md" in joined(warn)
    assert "IG (DEMO)" in joined(ok)


def test_ig_warns_when_no_epics_are_pinned():
    _, warn, _ = check(**IG)
    assert "IG_EPICS" in joined(warn)


def test_pinned_epics_are_reported_instead():
    ok, warn, _ = check(**IG, ig_epics="FTSE=IX.D.FTSE.DAILY.IP")
    assert "1 IG epic(s) pinned" in joined(ok)
    assert "no IG_EPICS mapping" not in joined(warn)


def test_crypto_on_ig_is_flagged():
    _, warn, _ = check(**IG, crypto_watchlist="BTC/USD")
    assert "no crypto" in joined(warn)


def test_t212_always_warns_about_the_missing_bracket():
    ok, warn, _ = check(**T212, **ALPACA, broker="t212")
    assert "NO bracket orders" in joined(warn)
    assert "trading-212.md" in joined(warn)


def test_t212_without_a_price_source_is_a_hard_failure():
    """
    T212 has no price API. With no Alpaca keys the bot scans nothing, which
    looks exactly like finding no setups — so it has to fail, not warn.
    """
    _, _, bad = check(**T212, broker="t212")
    assert "no price API" in joined(bad)
    assert "DATA" in joined(bad)


def test_t212_with_alpaca_keys_reports_where_prices_come_from():
    ok, _, bad = check(**T212, **ALPACA, broker="t212")
    assert "prices via Alpaca" in joined(ok)
    assert not bad


# ---------------------------------------------------------------------- #
# Two brokers
# ---------------------------------------------------------------------- #
def test_two_brokers_say_they_are_one_portfolio():
    """The headline fact: the limits are shared, not doubled."""
    ok, _, _ = check(**IG, **T212, broker="ig", secondary_broker="t212")
    text = joined(ok)
    assert "ig + t212" in text
    assert "ONE portfolio" in text


def test_a_route_to_an_unconfigured_broker_fails():
    _, _, bad = check(
        **IG, **T212, broker="ig", secondary_broker="t212", broker_routes="AAPL=alpaca"
    )
    assert "BROKER_ROUTES points at alpaca" in joined(bad)
    assert "silently" in joined(bad)


def test_a_second_broker_with_no_routes_warns_that_it_is_never_traded():
    _, warn, _ = check(**IG, **T212, broker="ig", secondary_broker="t212")
    text = joined(warn)
    assert "no BROKER_ROUTES" in text
    assert "never traded" in text


def test_valid_routes_are_counted():
    ok, warn, bad = check(
        **IG, **T212, broker="ig", secondary_broker="t212",
        broker_routes="FTSE=ig,AAPL=t212",
    )
    assert "2 symbol route(s) configured" in joined(ok)
    assert "no BROKER_ROUTES" not in joined(warn)
    assert not bad


def test_both_venues_warnings_appear_together():
    """A warning must not be skipped just because its venue is secondary."""
    _, warn, _ = check(
        **IG, **T212, broker="ig", secondary_broker="t212", broker_routes="AAPL=t212"
    )
    text = joined(warn)
    assert "LEVERAGED" in text          # IG, the primary
    assert "NO bracket orders" in text  # T212, the secondary


@pytest.mark.parametrize("primary,secondary", [("ig", "t212"), ("t212", "ig")])
def test_the_order_of_the_two_does_not_lose_a_venue(primary, secondary):
    ok, _, _ = check(
        **IG, **T212, **ALPACA, broker=primary, secondary_broker=secondary,
        broker_routes=f"AAPL={secondary}",
    )
    text = joined(ok)
    assert f"{primary} + {secondary}" in text
