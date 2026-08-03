"""
The human-in-the-loop gate.

In `alert` mode a signal that clears risk becomes a *pending approval* rather
than an order. You approve it from Telegram, the dashboard, or the CLI, and
only then does anything reach the broker.

Approvals expire. A setup that was clean 40 minutes ago usually isn't any more,
and an approval sitting in a chat overnight is a trap — so a stale one is
refused rather than filled at whatever the price has become.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.logging_setup import get_logger
from src.models import PositionSizing, Signal

log = get_logger(__name__)


class ApprovalStore:
    def __init__(self, path: Path, ttl_minutes: int = 45) -> None:
        self.path = path
        self.ttl = timedelta(minutes=ttl_minutes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("approvals.state_corrupt", path=str(self.path))
            return {}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            log.warning("approvals.save_failed", error=str(exc))

    # ------------------------------------------------------------------ #
    def add(self, signal: Signal, sizing: PositionSizing) -> None:
        self._data[signal.id] = {
            "signal": signal.to_dict(),
            "sizing": asdict(sizing),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        self._save()
        log.info("approvals.pending", signal_id=signal.id, symbol=signal.symbol)

    def get(self, signal_id: str) -> Optional[dict]:
        return self._data.get(signal_id)

    def pending(self) -> list[dict]:
        self.expire_stale()
        return [v for v in self._data.values() if v["status"] == "pending"]

    def is_expired(self, entry: dict) -> bool:
        try:
            created = datetime.fromisoformat(entry["created_at"])
        except (KeyError, ValueError):
            return True
        return datetime.now(timezone.utc) - created > self.ttl

    def expire_stale(self) -> list[str]:
        expired = [
            sid
            for sid, entry in self._data.items()
            if entry["status"] == "pending" and self.is_expired(entry)
        ]
        for sid in expired:
            self._data[sid]["status"] = "expired"
            log.info("approvals.expired", signal_id=sid)
        if expired:
            self._save()
        return expired

    def resolve(self, signal_id: str, status: str) -> Optional[dict]:
        """
        Mark an approval approved/denied. Returns the entry, or None if the id
        is unknown, already resolved, or too old to act on.
        """
        entry = self._data.get(signal_id)
        if entry is None:
            log.warning("approvals.unknown", signal_id=signal_id)
            return None
        if entry["status"] != "pending":
            log.info("approvals.already_resolved", signal_id=signal_id, status=entry["status"])
            return None
        if self.is_expired(entry):
            entry["status"] = "expired"
            self._save()
            log.info("approvals.too_late", signal_id=signal_id)
            return None

        entry["status"] = status
        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        log.info("approvals.resolved", signal_id=signal_id, status=status)
        return entry

    def prune(self, keep_last: int = 200) -> None:
        if len(self._data) <= keep_last:
            return
        ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("created_at", ""))
        self._data = dict(ordered[-keep_last:])
        self._save()


def signal_from_entry(entry: dict) -> tuple[Signal, PositionSizing]:
    """Rebuild the Signal + sizing stored in an approval entry."""
    raw = dict(entry["signal"])
    raw.pop("risk_reward", None)
    created_at = raw.pop("created_at", None)
    signal = Signal(**raw)
    if created_at:
        signal.created_at = datetime.fromisoformat(created_at)
    return signal, PositionSizing(**entry["sizing"])
