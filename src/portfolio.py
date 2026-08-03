"""
Portfolio state: what's open, what's at risk, and whether we're allowed to trade.

Drawdown is measured against a peak equity high-water mark persisted in the
journal, so restarting the bot does not reset a drawdown pause.
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
class PortfolioState:
    account: Account
    positions: list[Position] = field(default_factory=list)
    open_risk: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    day_pnl_pct: float = 0.0

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


class PortfolioManager:
    """Reads live state from the broker and tracks the equity high-water mark."""

    def __init__(self, broker, settings: Settings, journal=None) -> None:
        self.broker = broker
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
        account = self.broker.get_account()
        positions = self.broker.get_positions()

        peak = self._update_peak(account.equity)
        drawdown = ((peak - account.equity) / peak * 100) if peak > 0 else 0.0

        state = PortfolioState(
            account=account,
            positions=positions,
            open_risk=self._open_risk(positions),
            peak_equity=peak,
            drawdown_pct=max(0.0, drawdown),
            day_pnl_pct=account.day_pnl_pct,
        )
        if self.journal is not None:
            self.journal.record_equity(account.equity, account.cash, state.exposure)
        return state

    def _open_risk(self, positions: list[Position]) -> float:
        """
        Dollars still at risk across open positions.

        Uses the recorded stop where the journal has one, and falls back to
        assuming the full per-trade budget is still exposed — deliberately the
        pessimistic reading, since an unknown stop is not a safe stop.
        """
        total = 0.0
        stops = self.journal.open_stops() if self.journal is not None else {}
        for pos in positions:
            stop = stops.get(pos.symbol)
            if stop:
                total += abs(pos.avg_entry - stop) * abs(pos.qty)
            else:
                total += pos.notional * self.settings.risk_per_trade_pct / 100.0
        return total

    def _update_peak(self, equity: float) -> float:
        peak = equity
        if self._state_file.exists():
            try:
                peak = max(
                    float(json.loads(self._state_file.read_text(encoding="utf-8")).get("peak_equity", 0)),
                    equity,
                )
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                peak = equity
        try:
            self._state_file.write_text(json.dumps({"peak_equity": peak}), encoding="utf-8")
        except OSError:
            pass
        return peak

    def reset_peak(self) -> None:
        """Clear the high-water mark after a reviewed drawdown pause."""
        self._state_file.unlink(missing_ok=True)
        log.warning("portfolio.peak_reset", note="drawdown pause cleared by operator")
