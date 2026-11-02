"""
Portfolio state: what's open, what's at risk, and whether we're allowed to trade.

Drawdown is measured against a peak equity high-water mark persisted in the
journal, so restarting the bot does not reset a drawdown pause.

ONE PORTFOLIO, HOWEVER MANY BROKERS.

This reads every configured venue and adds them up, and that is the whole
point of the module. Run IG and Trading 212 as two independent bots and you
get two independent sets of limits: lose 3% at each on the same day and the
-3% daily stop never fires, because neither half ever saw more than 3%. The
same hole opens under the position cap (5 each, not 5 total) and the
correlation cap, which cannot notice that a FTSE spread bet at IG and a UK
tracker at T212 are the same bet twice.

So equity, cash, positions, exposure and open risk are summed across venues
before any rule looks at them, and every position carries the venue that
holds it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings
from src.logging_setup import get_logger
from src.models import Account, Position

log = get_logger(__name__)

# Correlation groups. Five tech longs is one position with five tickets, and the
# cap on correlated names is what stops that happening by accident.
DEFAULT_CORRELATION_GROUPS: dict[str, str] = {
    "AAPL": "big-tech", "MSFT": "big-tech", "GOOGL": "big-tech", "GOOG": "big-tech",
    "AMZN": "big-tech", "META": "big-tech", "NFLX": "big-tech",
    "NVDA": "semis", "AMD": "semis", "INTC": "semis", "AVGO": "semis",
    "MU": "semis", "TSM": "semis", "SMH": "semis",
    "TSLA": "ev", "RIVN": "ev", "LCID": "ev", "NIO": "ev",
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "WFC": "financials", "MS": "financials", "XLF": "financials",
    "XOM": "energy", "CVX": "energy", "COP": "energy", "XLE": "energy",
    "SPY": "index", "QQQ": "index", "IWM": "index", "DIA": "index", "VOO": "index",
    "BTC/USD": "crypto", "ETH/USD": "crypto", "SOL/USD": "crypto", "COIN": "crypto",
    "MSTR": "crypto",
}


@dataclass
class VenueState:
    """One broker's contribution, kept for reporting and for `doctor`."""

    name: str
    account: Account
    positions: list[Position] = field(default_factory=list)
    error: str = ""

    @property
    def exposure(self) -> float:
        return sum(p.notional for p in self.positions)


@dataclass
class PortfolioState:
    account: Account
    positions: list[Position] = field(default_factory=list)
    open_risk: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    day_pnl_pct: float = 0.0
    venues: list[VenueState] = field(default_factory=list)
    # Venues that could not be read this cycle. Any rule that depends on
    # seeing the whole book has to know the book is incomplete.
    unreachable: list[str] = field(default_factory=list)

    @property
    def exposure(self) -> float:
        return sum(p.notional for p in self.positions)

    @property
    def exposure_pct(self) -> float:
        return (self.exposure / self.account.equity * 100) if self.account.equity else 0.0

    @property
    def open_risk_pct(self) -> float:
        return (self.open_risk / self.account.equity * 100) if self.account.equity else 0.0

    @property
    def symbols(self) -> set[str]:
        return {p.symbol for p in self.positions}

    @property
    def complete(self) -> bool:
        """False when a broker failed, so the totals understate the real book."""
        return not self.unreachable

    def positions_at(self, venue: str) -> list[Position]:
        return [p for p in self.positions if p.venue == venue]


class PortfolioManager:
    """Reads live state from every broker and tracks the equity high-water mark."""

    def __init__(self, broker, settings: Settings, journal=None) -> None:
        # Accepts either a single Broker or a BrokerRouter. One argument that
        # takes both keeps every existing caller and test working, and means
        # the single-broker path is the same code as the two-broker one rather
        # than a separate branch that could drift.
        self.brokers = list(broker.all()) if hasattr(broker, "all") else [broker]
        self.broker = self.brokers[0]
        self.settings = settings
        self.journal = journal
        self._state_file: Path = settings.data_path / "portfolio_state.json"
        self._correlation = dict(DEFAULT_CORRELATION_GROUPS)
        self._load_correlation_overrides()

    # ------------------------------------------------------------------ #
    def _load_correlation_overrides(self) -> None:
        """Optional config/correlation_groups.json merges over the defaults."""
        override = Path(__file__).resolve().parent.parent / "config" / "correlation_groups.json"
        if not override.exists():
            return
        try:
            data = json.loads(override.read_text(encoding="utf-8"))
            self._correlation.update({k.upper(): v for k, v in data.items()})
            log.info("portfolio.correlation_overrides_loaded", count=len(data))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("portfolio.correlation_overrides_failed", error=str(exc))

    def group_for(self, symbol: str) -> str:
        """Unmapped symbols get their own group, so they never look correlated."""
        return self._correlation.get(symbol.upper(), f"ungrouped:{symbol.upper()}")

    # ------------------------------------------------------------------ #
    def snapshot(self) -> PortfolioState:
        venues, unreachable = self._read_venues()
        account = self._combine(venues)
        positions = [p for v in venues for p in v.positions]

        peak = self._update_peak(account.equity, trustworthy=not unreachable)
        if unreachable:
            # Equity is short by the whole of the missing account, so measuring
            # it against the peak invents a drawdown — with every venue down it
            # reads a terrifying 100%. Report none: `complete` is already False,
            # and the caller halts on that rather than on a fabricated number.
            drawdown = 0.0
        else:
            drawdown = ((peak - account.equity) / peak * 100) if peak > 0 else 0.0

        state = PortfolioState(
            account=account,
            positions=positions,
            open_risk=self._open_risk(positions),
            peak_equity=peak,
            drawdown_pct=max(0.0, drawdown),
            day_pnl_pct=self._day_pnl_pct(account),
            venues=venues,
            unreachable=unreachable,
        )
        if self.journal is not None and not unreachable:
            # Only record a complete reading. A row written while one broker
            # was down would become tomorrow's start-of-day baseline and a
            # false drawdown, long after the outage is forgotten.
            self.journal.record_equity(account.equity, account.cash, state.exposure)
        return state

    # ------------------------------------------------------------------ #
    def _read_venues(self) -> tuple[list[VenueState], list[str]]:
        """
        Read each broker, surviving one of them being down.

        A failed venue is reported, not swallowed and not fatal: the bot can
        still manage what it can see, but the caller is told the totals are
        short so it can refuse to open anything new.
        """
        venues: list[VenueState] = []
        unreachable: list[str] = []
        for broker in self.brokers:
            try:
                account = broker.get_account()
                positions = broker.get_positions()
            except Exception as exc:
                log.error("portfolio.venue_unreachable", venue=broker.name, error=str(exc))
                unreachable.append(broker.name)
                venues.append(
                    VenueState(name=broker.name, account=Account(0, 0, 0), error=str(exc))
                )
                continue
            for pos in positions:
                if not pos.venue:  # a stand-in broker in a test may not set it
                    pos.venue = broker.name
            venues.append(VenueState(name=broker.name, account=account, positions=positions))
        return venues, unreachable

    @staticmethod
    def _combine(venues: list[VenueState]) -> Account:
        """
        Sum the accounts into the single view every risk rule reads.

        `last_equity` sums only where every venue reports one. A partial sum
        would be worse than none: it would produce a day-P&L percentage that
        looks authoritative and is wrong, which is exactly how a daily stop
        fails to fire.
        """
        reported = [v.account.last_equity for v in venues if v.account.last_equity]
        return Account(
            equity=sum(v.account.equity for v in venues),
            cash=sum(v.account.cash for v in venues),
            buying_power=sum(v.account.buying_power for v in venues),
            last_equity=(
                sum(v.account.last_equity for v in venues)
                if len(reported) == len(venues) and venues
                else 0.0
            ),
        )

    def _day_pnl_pct(self, account: Account) -> float:
        """
        Today's move, from the broker when it says, from the journal when not.

        Trading 212 reports no start-of-day equity at all and IG's is an
        approximation, so without this fallback the daily loss limit would
        read 0.00% forever and never trip.
        """
        if account.last_equity:
            return account.day_pnl_pct

        baseline = None
        if self.journal is not None:
            try:
                baseline = self.journal.start_of_day_equity()
            except Exception as exc:  # pragma: no cover - journal is local
                log.warning("portfolio.start_of_day_lookup_failed", error=str(exc))

        if not baseline:
            # First cycle of the day: there is genuinely nothing to compare
            # against yet, and the row written below becomes the baseline.
            log.debug("portfolio.no_day_baseline", note="first reading of the day")
            return 0.0
        return (account.equity - baseline) / baseline * 100.0

    def _open_risk(self, positions: list[Position]) -> float:
        """
        Money still at risk across open positions, at every venue.

        Uses the recorded stop where the journal has one, and falls back to
        assuming the full per-trade budget is still exposed — deliberately the
        pessimistic reading, since an unknown stop is not a safe stop.
        """
        total = 0.0
        stops = self.journal.open_stops() if self.journal is not None else {}
        for pos in positions:
            # Journal stops are keyed by symbol. With two venues holding the
            # same ticker that is ambiguous, so prefer the venue-qualified key
            # and fall back to the bare symbol.
            stop = stops.get(pos.key) or stops.get(pos.symbol)
            if stop:
                total += abs(pos.avg_entry - stop) * abs(pos.qty)
            else:
                total += pos.notional * self.settings.risk_per_trade_pct / 100.0
        return total

    def _update_peak(self, equity: float, trustworthy: bool = True) -> float:
        """
        Track the equity high-water mark that the drawdown pause measures from.

        A reading taken while a broker was unreachable is low by that broker's
        whole balance. Comparing it to the peak would invent a drawdown and
        pause trading for a reason that never happened — so an incomplete
        reading is measured against the stored peak but never written back,
        and never allowed to lower it.
        """
        stored = 0.0
        if self._state_file.exists():
            try:
                stored = float(
                    json.loads(self._state_file.read_text(encoding="utf-8")).get("peak_equity", 0)
                )
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                stored = 0.0

        if not trustworthy:
            return stored or equity

        peak = max(stored, equity)
        try:
            self._state_file.write_text(json.dumps({"peak_equity": peak}), encoding="utf-8")
        except OSError:
            pass
        return peak

    def reset_peak(self) -> None:
        """Clear the high-water mark after a reviewed drawdown pause."""
        self._state_file.unlink(missing_ok=True)
        log.warning("portfolio.peak_reset", note="drawdown pause cleared by operator")
