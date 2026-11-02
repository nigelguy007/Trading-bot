"""
One portfolio across two brokers.

The test that matters here is `test_the_daily_stop_fires_on_the_combined_loss`.
Two brokers run as two independent bots give you two independent -3% limits,
so a -3% day at each is a -6% day that nothing stopped. Everything else in
this file is the same hole in a different rule: the position cap counting to
five twice, the correlation cap unable to see that the same bet was placed at
both venues, the drawdown mark measured against half the book.
"""
from __future__ import annotations

import pytest

from src.broker.base import Broker
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.journal import TradeJournal
from src.models import Account, Position, Signal
from src.portfolio import PortfolioManager
from src.risk import RiskManager


class StubBroker(Broker):
    """A broker that reports exactly what a test tells it to."""

    def __init__(
        self,
        name: str,
        equity: float,
        positions: list[Position] | None = None,
        *,
        last_equity: float = 0.0,
        cash: float | None = None,
        fail: str = "",
        asset_classes: tuple[str, ...] = ("equity",),
    ) -> None:
        self.name = name
        self._equity = equity
        self._last_equity = last_equity
        self._cash = equity if cash is None else cash
        self._positions = positions or []
        self._fail = fail
        self._asset_classes = asset_classes

    def get_account(self) -> Account:
        if self._fail:
            raise RuntimeError(self._fail)
        return Account(
            equity=self._equity,
            cash=self._cash,
            buying_power=self._cash,
            last_equity=self._last_equity,
        )

    def get_positions(self) -> list[Position]:
        if self._fail:
            raise RuntimeError(self._fail)
        return list(self._positions)

    def submit_bracket_order(self, signal, sizing):  # pragma: no cover
        raise NotImplementedError

    def close_position(self, symbol):  # pragma: no cover
        raise NotImplementedError

    def supports(self, asset_class: str) -> bool:
        return asset_class in self._asset_classes


def pos(symbol: str, venue: str, qty: float = 10, entry: float = 100, price: float = 100):
    return Position(
        symbol=symbol, qty=qty, avg_entry=entry, market_price=price,
        unrealized_pnl=(price - entry) * qty, venue=venue,
    )


def settings_for(tmp_path, **kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("log_dir", str(tmp_path / "logs"))
    return Settings(_env_file=None, **kw)


def router_of(*brokers: StubBroker, settings: Settings | None = None) -> BrokerRouter:
    return BrokerRouter(brokers[0], extra=list(brokers[1:]), settings=settings)


def a_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0,
        target=106.0, confidence=0.9, reasoning="test",
    )
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- #
# Totals
# ---------------------------------------------------------------------- #
def test_equity_and_cash_are_summed_across_venues(tmp_path):
    ig = StubBroker("ig", equity=6_000, cash=5_000)
    t212 = StubBroker("t212", equity=4_000, cash=1_000)
    pm = PortfolioManager(router_of(ig, t212), settings_for(tmp_path))

    state = pm.snapshot()
    assert state.account.equity == 10_000
    assert state.account.cash == 6_000
    assert [v.name for v in state.venues] == ["ig", "t212"]


def test_positions_from_both_venues_appear_once_each_tagged(tmp_path):
    ig = StubBroker("ig", 5_000, [pos("FTSE", "ig")])
    t212 = StubBroker("t212", 5_000, [pos("AAPL", "t212"), pos("MSFT", "t212")])
    state = PortfolioManager(router_of(ig, t212), settings_for(tmp_path)).snapshot()

    assert len(state.positions) == 3
    assert state.symbols == {"FTSE", "AAPL", "MSFT"}
    assert {p.venue for p in state.positions} == {"ig", "t212"}
    assert [p.symbol for p in state.positions_at("t212")] == ["AAPL", "MSFT"]


def test_the_same_ticker_at_two_venues_is_two_positions(tmp_path):
    """Held at both, it is twice the exposure — and it must look like it."""
    ig = StubBroker("ig", 5_000, [pos("AAPL", "ig")])
    t212 = StubBroker("t212", 5_000, [pos("AAPL", "t212")])
    state = PortfolioManager(router_of(ig, t212), settings_for(tmp_path)).snapshot()

    assert len(state.positions) == 2
    assert state.exposure == 2_000
    assert {p.key for p in state.positions} == {"ig:AAPL", "t212:AAPL"}


def test_a_single_broker_still_works_unchanged(tmp_path):
    """PortfolioManager must accept a bare broker, as every existing caller does."""
    state = PortfolioManager(
        StubBroker("alpaca", 10_000, [pos("AAPL", "alpaca")]), settings_for(tmp_path)
    ).snapshot()
    assert state.account.equity == 10_000
    assert len(state.positions) == 1


# ---------------------------------------------------------------------- #
# The circuit breakers — the reason this module exists
# ---------------------------------------------------------------------- #
def test_the_daily_stop_fires_on_the_combined_loss(tmp_path):
    """
    THE regression. Each venue is down 3% of its own equity. Run separately,
    neither breaches a -3% daily limit and trading continues. Combined, the
    book is down 3% of 10,000 and the day is over.
    """
    s = settings_for(tmp_path, daily_loss_limit_pct=3.0)
    ig = StubBroker("ig", equity=4_850, last_equity=5_000)      # -3.0%
    t212 = StubBroker("t212", equity=4_850, last_equity=5_000)  # -3.0%
    pm = PortfolioManager(router_of(ig, t212), s)
    state = pm.snapshot()

    assert state.account.equity == 9_700
    assert state.day_pnl_pct == pytest.approx(-3.0)

    decision = RiskManager(s, pm).check_circuit_breakers(state)
    assert not decision.approved
    assert decision.halt
    assert "DAILY LOSS LIMIT" in decision.reason


def test_a_loss_at_one_venue_is_diluted_by_the_other(tmp_path):
    """The flip side, and just as important: -6% at one venue is -3% of the
    combined book, which is at the limit, not past it."""
    s = settings_for(tmp_path, daily_loss_limit_pct=5.0)
    ig = StubBroker("ig", equity=4_700, last_equity=5_000)      # -6%
    t212 = StubBroker("t212", equity=5_000, last_equity=5_000)  # flat
    pm = PortfolioManager(router_of(ig, t212), s)
    state = pm.snapshot()

    assert state.day_pnl_pct == pytest.approx(-3.0)
    assert RiskManager(s, pm).check_circuit_breakers(state).approved


def test_the_position_cap_counts_both_venues(tmp_path):
    s = settings_for(tmp_path, max_open_positions=3)
    ig = StubBroker("ig", 5_000, [pos("FTSE", "ig"), pos("DAX", "ig")])
    t212 = StubBroker("t212", 5_000, [pos("AAPL", "t212")])
    pm = PortfolioManager(router_of(ig, t212), s)

    decision = RiskManager(s, pm).evaluate(a_signal(symbol="NVDA"), pm.snapshot())
    assert not decision.approved
    assert "3-position limit" in decision.reason


def test_the_correlation_cap_sees_across_venues(tmp_path):
    """A big-tech name at each venue is still two correlated positions."""
    s = settings_for(tmp_path, max_correlated_positions=2)
    ig = StubBroker("ig", 5_000, [pos("AAPL", "ig")])
    t212 = StubBroker("t212", 5_000, [pos("MSFT", "t212")])
    pm = PortfolioManager(router_of(ig, t212), s)

    decision = RiskManager(s, pm).evaluate(a_signal(symbol="GOOGL"), pm.snapshot())
    assert not decision.approved
    assert "correlated" in decision.reason


def test_holding_a_symbol_at_one_venue_blocks_buying_it_at_the_other(tmp_path):
    s = settings_for(tmp_path)
    ig = StubBroker("ig", 5_000, [pos("AAPL", "ig")])
    t212 = StubBroker("t212", 5_000)
    pm = PortfolioManager(router_of(ig, t212), s)

    decision = RiskManager(s, pm).evaluate(a_signal(symbol="AAPL"), pm.snapshot())
    assert not decision.approved
    assert "Already holding AAPL" in decision.reason


def test_drawdown_is_measured_on_the_combined_high_water_mark(tmp_path):
    s = settings_for(tmp_path, max_drawdown_pct=15.0)
    ig = StubBroker("ig", equity=5_000)
    t212 = StubBroker("t212", equity=5_000)
    pm = PortfolioManager(router_of(ig, t212), s)
    pm.snapshot()  # sets the peak at 10,000

    ig._equity = 4_000
    t212._equity = 4_000  # combined 8,000 = -20% from peak
    state = pm.snapshot()

    assert state.peak_equity == 10_000
    assert state.drawdown_pct == pytest.approx(20.0)
    decision = RiskManager(s, pm).check_circuit_breakers(state)
    assert decision.halt
    assert "MAX DRAWDOWN" in decision.reason


# ---------------------------------------------------------------------- #
# Day P&L without a broker baseline
# ---------------------------------------------------------------------- #
def test_day_pnl_falls_back_to_the_journal_when_no_broker_reports_it(tmp_path):
    """
    Trading 212 reports no start-of-day equity. Without the journal fallback
    the daily stop would read 0.00% every day and never fire — a circuit
    breaker that looks armed and is not.
    """
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    journal.record_equity(10_000.0, 10_000.0, 0.0)  # this morning

    t212 = StubBroker("t212", equity=9_600, last_equity=0.0)
    state = PortfolioManager(router_of(t212), s, journal).snapshot()

    assert state.day_pnl_pct == pytest.approx(-4.0)
    journal.close()


def test_a_broker_baseline_is_preferred_when_it_exists(tmp_path):
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    journal.record_equity(20_000.0, 0.0, 0.0)  # deliberately misleading

    alpaca = StubBroker("alpaca", equity=9_900, last_equity=10_000)
    state = PortfolioManager(router_of(alpaca), s, journal).snapshot()

    assert state.day_pnl_pct == pytest.approx(-1.0)
    journal.close()


def test_a_partial_baseline_is_not_used_at_all(tmp_path):
    """
    One venue reports last_equity and the other does not. Summing only the
    half that answered would produce a confident, wrong percentage — so the
    sum is discarded and the journal is used instead.
    """
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    journal.record_equity(10_000.0, 0.0, 0.0)

    alpaca = StubBroker("alpaca", equity=4_900, last_equity=5_000)
    t212 = StubBroker("t212", equity=4_900, last_equity=0.0)
    state = PortfolioManager(router_of(alpaca, t212), s, journal).snapshot()

    assert state.account.last_equity == 0.0
    assert state.day_pnl_pct == pytest.approx(-2.0)  # 9,800 vs the journal's 10,000
    journal.close()


def test_the_first_reading_of_the_day_reports_flat_not_a_false_loss(tmp_path):
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    state = PortfolioManager(router_of(StubBroker("t212", 10_000)), s, journal).snapshot()
    assert state.day_pnl_pct == 0.0
    journal.close()


# ---------------------------------------------------------------------- #
# One venue down
# ---------------------------------------------------------------------- #
def test_an_unreachable_venue_halts_new_trades(tmp_path):
    """
    Equity is understated by the whole of the missing account, and every
    limit is a percentage of equity. Sizing against that number would risk
    far more of the real book than configured.
    """
    s = settings_for(tmp_path)
    ig = StubBroker("ig", 5_000)
    t212 = StubBroker("t212", 5_000, fail="connection refused")
    pm = PortfolioManager(router_of(ig, t212), s)
    state = pm.snapshot()

    assert not state.complete
    assert state.unreachable == ["t212"]
    assert state.account.equity == 5_000  # only what could be read

    decision = RiskManager(s, pm).check_circuit_breakers(state)
    assert not decision.approved
    assert decision.halt
    assert "t212" in decision.reason


def test_an_outage_cannot_invent_a_drawdown(tmp_path):
    """
    A reading taken while a venue is down is low by that venue's balance. If
    it were compared to — or written into — the high-water mark, the bot
    would pause itself for a drawdown that never happened.
    """
    s = settings_for(tmp_path)
    ig = StubBroker("ig", 5_000)
    t212 = StubBroker("t212", 5_000)
    pm = PortfolioManager(router_of(ig, t212), s)
    pm.snapshot()  # peak 10,000

    t212._fail = "timeout"
    during = pm.snapshot()
    assert during.peak_equity == 10_000  # not lowered to 5,000
    # And no drawdown is reported against the half-read book. With every
    # venue down this read 100% before it was fixed.
    assert during.drawdown_pct == 0.0
    assert not during.complete

    t212._fail = ""
    after = pm.snapshot()
    assert after.peak_equity == 10_000
    assert after.drawdown_pct == pytest.approx(0.0)


def test_an_outage_is_not_written_to_the_equity_curve(tmp_path):
    """Otherwise it becomes tomorrow's start-of-day baseline."""
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    ig = StubBroker("ig", 5_000)
    t212 = StubBroker("t212", 5_000, fail="down")
    PortfolioManager(router_of(ig, t212), s, journal).snapshot()

    assert journal.equity_curve() == []
    journal.close()


def test_a_healthy_read_is_recorded(tmp_path):
    s = settings_for(tmp_path)
    journal = TradeJournal(tmp_path / "j.db")
    PortfolioManager(router_of(StubBroker("ig", 10_000)), s, journal).snapshot()

    curve = journal.equity_curve()
    assert len(curve) == 1
    assert curve[0]["equity"] == 10_000
    journal.close()


def test_every_venue_down_does_not_report_a_total_wipeout(tmp_path):
    """
    The case that actually showed up on the dashboard: both brokers
    unreachable, equity reads 0, and the drawdown against a stored peak came
    out at 100%. Alarming, and wrong — nothing had been lost.
    """
    s = settings_for(tmp_path)
    ig = StubBroker("ig", 5_000)
    t212 = StubBroker("t212", 5_000)
    pm = PortfolioManager(router_of(ig, t212), s)
    pm.snapshot()  # peak 10,000

    ig._fail = t212._fail = "down"
    state = pm.snapshot()

    assert state.account.equity == 0
    assert state.drawdown_pct == 0.0
    assert state.unreachable == ["ig", "t212"]
    assert not RiskManager(s, pm).check_circuit_breakers(state).approved
