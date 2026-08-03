"""
The trade journal — SQLite, in data/journal.db.

Everything the bot decides is written here: signals it generated, signals it
rejected and why, orders it placed, trades it closed, and an equity curve.
The journal is what makes forward-testing meaningful; without it you have
opinions about the last few weeks rather than a record of them.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.logging_setup import get_logger
from src.models import OrderResult, RiskDecision, Signal

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    source TEXT,
    timeframe TEXT,
    direction TEXT,
    entry REAL, stop REAL, target REAL,
    risk_reward REAL,
    confidence REAL,
    reasoning TEXT,
    invalidation TEXT,
    risk_notes TEXT,
    news_impact TEXT,
    risk_approved INTEGER,
    risk_reason TEXT,
    shares REAL, dollar_risk REAL, account_risk_pct REAL,
    status TEXT NOT NULL,
    extra TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    created_at TEXT NOT NULL,
    broker TEXT, broker_order_id TEXT,
    symbol TEXT, side TEXT, qty REAL,
    status TEXT, message TEXT, raw TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    direction TEXT,
    strategy TEXT,
    opened_at TEXT, closed_at TEXT,
    qty REAL, entry REAL, exit REAL, stop REAL, target REAL,
    pnl REAL, r_multiple REAL, fees REAL,
    status TEXT NOT NULL,
    exit_reason TEXT,
    review TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL, cash REAL, exposure REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    scanned INTEGER, signals INTEGER, approved INTEGER, errors INTEGER,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals(status);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_closed   ON trades(closed_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeJournal:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # Signals
    # ------------------------------------------------------------------ #
    def log_signal(
        self,
        signal: Signal,
        decision: RiskDecision,
        status: str,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        sizing = decision.sizing
        self.conn.execute(
            """INSERT OR REPLACE INTO signals VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.id,
                signal.created_at.isoformat(),
                signal.symbol,
                signal.source,
                signal.timeframe,
                signal.direction,
                signal.entry,
                signal.stop,
                signal.target,
                round(signal.risk_reward, 3),
                signal.confidence,
                signal.reasoning,
                json.dumps(signal.invalidation),
                signal.risk_notes,
                signal.news_impact,
                int(decision.approved),
                decision.reason,
                sizing.shares if sizing else 0.0,
                sizing.dollar_risk if sizing else 0.0,
                sizing.account_risk_pct if sizing else 0.0,
                status,
                json.dumps(extra or {}),
            ),
        )
        self.conn.commit()

    def update_signal_status(self, signal_id: str, status: str) -> None:
        self.conn.execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))
        self.conn.commit()

    def get_signal(self, signal_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None

    def recent_signals(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def signals_since(self, iso_ts: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM signals WHERE created_at >= ? ORDER BY created_at", (iso_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Orders and trades
    # ------------------------------------------------------------------ #
    def log_order(self, order: OrderResult, signal_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.id,
                signal_id,
                _now(),
                order.broker,
                order.broker_order_id,
                order.symbol,
                order.side,
                order.qty,
                order.status or ("accepted" if order.accepted else "rejected"),
                order.message,
                json.dumps(order.raw)[:4000],
            ),
        )
        self.conn.commit()

    def open_trade(self, signal: Signal, qty: float, strategy: str = "") -> str:
        trade_id = f"trd_{signal.id.split('_', 1)[-1]}"
        self.conn.execute(
            """INSERT OR REPLACE INTO trades
               (id, signal_id, symbol, direction, strategy, opened_at, closed_at,
                qty, entry, exit, stop, target, pnl, r_multiple, fees, status,
                exit_reason, review)
               VALUES (?,?,?,?,?,?,NULL,?,?,NULL,?,?,NULL,NULL,0,'open',NULL,NULL)""",
            (
                trade_id,
                signal.id,
                signal.symbol,
                signal.direction,
                strategy or signal.source,
                _now(),
                qty,
                signal.entry,
                signal.stop,
                signal.target,
            ),
        )
        self.conn.commit()
        return trade_id

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str = "",
        fees: float = 0.0,
        closed_at: Optional[str] = None,
    ) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if row is None:
            log.warning("journal.close_unknown_trade", trade_id=trade_id)
            return None

        entry, stop, qty = row["entry"], row["stop"], row["qty"]
        direction = row["direction"]
        pnl = (exit_price - entry) * qty if direction == "long" else (entry - exit_price) * qty
        pnl -= fees
        risk_per_share = abs(entry - stop)
        # R-multiple: profit measured in units of the risk actually taken. The
        # only P&L number that is comparable across position sizes.
        r_multiple = (pnl / (risk_per_share * abs(qty))) if risk_per_share and qty else 0.0

        self.conn.execute(
            """UPDATE trades SET closed_at=?, exit=?, pnl=?, r_multiple=?, fees=?,
                                 status='closed', exit_reason=? WHERE id=?""",
            (closed_at or _now(), exit_price, pnl, r_multiple, fees, exit_reason, trade_id),
        )
        self.conn.commit()
        log.info("journal.trade_closed", trade_id=trade_id, pnl=round(pnl, 2),
                 r=round(r_multiple, 2), reason=exit_reason)
        return self.get_trade(trade_id)

    def get_trade(self, trade_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None

    def open_trades(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
        return [dict(r) for r in rows]

    def closed_trades(self, limit: int = 500, strategy: str | None = None) -> list[dict]:
        if strategy:
            rows = self.conn.execute(
                """SELECT * FROM trades WHERE status='closed' AND strategy=?
                   ORDER BY closed_at DESC LIMIT ?""",
                (strategy, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def open_stops(self) -> dict[str, float]:
        """symbol -> stop price, for open-risk accounting."""
        rows = self.conn.execute(
            "SELECT symbol, stop FROM trades WHERE status='open'"
        ).fetchall()
        return {r["symbol"]: r["stop"] for r in rows if r["stop"]}

    def save_review(self, trade_id: str, review: str) -> None:
        self.conn.execute("UPDATE trades SET review = ? WHERE id = ?", (review, trade_id))
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Equity and runs
    # ------------------------------------------------------------------ #
    def record_equity(self, equity: float, cash: float, exposure: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity VALUES (?,?,?,?)", (_now(), equity, cash, exposure)
        )
        self.conn.commit()

    def equity_curve(self, limit: int = 1000) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, scanned, signals, approved, errors) VALUES (?,0,0,0,0)",
            (_now(),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, scanned: int, signals: int, approved: int, errors: int, notes: str = ""
    ) -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=?, scanned=?, signals=?, approved=?,
                               errors=?, notes=? WHERE id=?""",
            (_now(), scanned, signals, approved, errors, notes, run_id),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def export_csv(self, destination: Path) -> Path:
        trades = self.closed_trades(limit=100_000)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "id", "symbol", "direction", "strategy", "opened_at", "closed_at",
            "qty", "entry", "exit", "stop", "target", "pnl", "r_multiple",
            "fees", "exit_reason",
        ]
        with destination.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(trades)
        log.info("journal.exported", path=str(destination), rows=len(trades))
        return destination
