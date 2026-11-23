"""
The kill switch — one action that stops all trading, from anywhere, at once.

It is a file, `data/KILL_SWITCH.json`, and that is deliberate:

* It survives restarts. A crash-looping bot comes back up still stopped.
* Any process can pull it. The CLI, the dashboard button and the Telegram
  `/kill` command can all engage it while the scheduler runs elsewhere, because
  the bot RE-READS the file before every cycle and before every order — no
  cache, no signal handling, no shared memory.
* It fails closed. If the file exists but cannot be read or parsed, the switch
  counts as engaged. When in doubt about whether you are allowed to trade, you
  are not.

Engaging is cheap and safe, so every channel can do it. Releasing is not: it is
CLI-only (`python -m src.cli resume --yes`), never over HTTP or chat, so a
leaked webhook URL can stop the bot but never restart it.

Engaging stops NEW orders. Open positions keep their broker-side stops and
targets unless you also flatten (`kill --flatten`), which market-closes them.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.logging_setup import get_logger

log = get_logger(__name__)

FILENAME = "KILL_SWITCH.json"


@dataclass(frozen=True)
class KillState:
    reason: str
    source: str
    engaged_at: str

    def describe(self) -> str:
        return f"kill switch engaged by {self.source} at {self.engaged_at}: {self.reason}"


class KillSwitch:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / FILENAME

    def state(self) -> KillState | None:
        """The current state, read fresh from disk every call."""
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return KillState(
                reason=str(raw.get("reason", "unspecified")),
                source=str(raw.get("source", "unknown")),
                engaged_at=str(raw.get("engaged_at", "unknown")),
            )
        except (OSError, ValueError, AttributeError) as exc:
            log.error("killswitch.unreadable_failing_closed", path=str(self.path), error=str(exc))
            return KillState(reason=f"kill file unreadable ({exc})", source="fail-closed", engaged_at="unknown")

    @property
    def engaged(self) -> bool:
        return self.state() is not None

    def engage(self, reason: str = "manual", source: str = "unknown") -> KillState:
        """Engage (idempotent — an existing engagement keeps its original reason)."""
        current = self.state()
        if current is not None:
            return current
        state = KillState(
            reason=reason or "manual",
            source=source,
            engaged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic: no reader ever sees a half-written file
        log.critical("killswitch.engaged", reason=state.reason, source=source)
        return state

    def release(self) -> bool:
        """Release. Returns False if it was not engaged."""
        if not self.path.exists():
            return False
        self.path.unlink()
        log.warning("killswitch.released")
        return True
