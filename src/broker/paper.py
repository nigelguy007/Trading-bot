"""
A local simulated broker.

Exists so the entire pipeline — scan, analyse, gate, size, "fill", journal,
alert — runs end to end with no broker account at all. It fills at the signal's
entry price and holds positions in a JSON file, and it is what the backtester
trades through.

It is not a market simulator: no slippage, no partial fills, no queue position.
Real paper trading through Alpaca is the next step up, and live is the step
after that.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.broker.base import Broker
from src.config import Settings
from src.logging_setup import get_logger
from src.models import Account, OrderResult, Position, PositionSizing, Signal

log = get_logger(__name__)

STARTING_EQUITY = 10_000.0


class SimulatedBroker(Broker):
    name = "simulated"

    def __init__(self, settings: Settings, starting_equity: float = STARTING_EQUITY) -> None:
        self.settings = settings
        self.path: Path = settings.data_path / "simulated_broker.json"
        self.state = self._load(starting_equity)

    @property
    def is_paper(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    def _load(self, starting_equity: float) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("broker.sim_state_corrupt", path=str(self.path))
        return {
            "cash": starting_equity,
            "start_of_day_equity": starting_equity,
            "positions": {},  # symbol -> {qty, avg_entry, price, stop, target}
        }

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("broker.sim_state_save_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    def get_account(self) -> Account:
        held = sum(
            abs(p["qty"]) * p.get("price", p["avg_entry"])
            for p in self.state["positions"].values()
        )
        equity = self.state["cash"] + held
        return Account(
            equity=equity,
            cash=self.state["cash"],
            buying_power=self.state["cash"],
            last_equity=self.state.get("start_of_day_equity", equity),
        )

    def get_positions(self) -> list[Position]:
        out = []
        for symbol, p in self.state["positions"].items():
            price = p.get("price", p["avg_entry"])
            out.append(
                Position(
                    symbol=symbol,
                    qty=p["qty"],
                    avg_entry=p["avg_entry"],
                    market_price=price,
                    unrealized_pnl=(price - p["avg_entry"]) * p["qty"],
                    side="long" if p["qty"] >= 0 else "short",
                )
            )
        return out

    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult:
        if sizing.shares <= 0:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=0, side="", message="position size is zero",
            )

        cost = sizing.shares * signal.entry
        if cost > self.state["cash"]:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side=signal.direction,
                message=f"insufficient simulated cash (${self.state['cash']:,.2f} < ${cost:,.2f})",
            )

        qty = sizing.shares if signal.direction == "long" else -sizing.shares
        self.state["cash"] -= cost
        self.state["positions"][signal.symbol] = {
            "qty": qty,
            "avg_entry": signal.entry,
            "price": signal.entry,
            "stop": signal.stop,
            "target": signal.target,
        }
        self._save()

        log.info("broker.sim_filled", symbol=signal.symbol, qty=qty, price=signal.entry)
        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=signal.symbol,
            qty=sizing.shares,
            side="buy" if signal.direction == "long" else "sell",
            broker_order_id=f"sim-{signal.id}",
            status="filled",
        )

    def close_position(self, symbol: str, price: float | None = None) -> OrderResult:
        pos = self.state["positions"].pop(symbol, None)
        if pos is None:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message="no such position",
            )
        exit_price = price if price is not None else pos.get("price", pos["avg_entry"])
        self.state["cash"] += abs(pos["qty"]) * exit_price
        self._save()
        return OrderResult(
            accepted=True, broker=self.name, symbol=symbol,
            qty=abs(pos["qty"]), side="close", status="filled",
        )

    # -- simulation helpers, used by the backtester ---------------------- #
    def mark_to_market(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self.state["positions"]:
                self.state["positions"][symbol]["price"] = price
        self._save()

    def start_new_day(self) -> None:
        self.state["start_of_day_equity"] = self.get_account().equity
        self._save()

    def reset(self, starting_equity: float = STARTING_EQUITY) -> None:
        self.state = {
            "cash": starting_equity,
            "start_of_day_equity": starting_equity,
            "positions": {},
        }
        self._save()
