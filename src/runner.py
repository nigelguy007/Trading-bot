"""
The scan loop, inside the web server, switchable from the dashboard.

`serve` used to be the dashboard alone: the scan loop lived in `run`, a
separate command built on a *blocking* scheduler. So the ordinary way to
start the bot — `./start.sh`, open the page — produced a dashboard that
displayed an idle account forever and never analysed anything. Nothing
said so, because an idle account and a bot that is not running look
identical.

This is the same loop on a background scheduler, owned by the server, so
the dashboard can start and stop it and say plainly which it is.

It is NOT started automatically. Scanning costs Claude API calls on every
cycle, and in auto mode it places orders, so switching it on is an
explicit act. `AUTOSTART_SCANNER=true` opts in for an unattended host.

Nothing here weakens the trading gates. The loop calls the same pipeline
the CLI does, which still honours execution_mode and the three
independent switches that live trading requires.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.app import AppContext
from src.logging_setup import get_logger
from src.scheduler import EASTERN, market_is_open

log = get_logger(__name__)


def run_one_cycle(ctx: AppContext) -> dict[str, Any]:
    """
    One pass: repair what the brokers could not do atomically, then scan.

    Shared with the CLI's blocking scheduler so the two cannot drift. The
    reconcile step matters most on Trading 212, which has no OCO: it
    cancels the stop left resting after a target filled and flags any
    position sitting there with no stop at all.
    """
    repairs = ctx.execution.reconcile()
    if any(v.get("cancelled") or v.get("unprotected") for v in repairs.values()):
        log.info("scan.reconciled", repairs=repairs)
    result = ctx.pipeline.run_cycle()
    return result.summary()


class BackgroundScanner:
    """Owns the scan job and reports enough state for the UI to be honest."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._scheduler = BackgroundScheduler(timezone=EASTERN)
        self._lock = threading.Lock()
        self._last_run: Optional[dict[str, Any]] = None
        self._busy = False

    # -- state ------------------------------------------------------- #
    @property
    def running(self) -> bool:
        return self._scheduler.running and self._scheduler.get_job("scan") is not None

    def state(self) -> dict[str, Any]:
        job = self._scheduler.get_job("scan") if self._scheduler.running else None
        next_run = getattr(job, "next_run_time", None) if job else None
        s = self.ctx.settings
        return {
            "running": self.running,
            "busy": self._busy,
            "interval_minutes": s.scan_interval_minutes,
            "execution_mode": s.execution_mode,
            "next_run": next_run.isoformat() if next_run else None,
            "last_run": self._last_run,
            # Scanning outside market hours is a no-op for equities, so the
            # UI needs to say "waiting for the open" rather than "idle".
            "market_open": self._market_open(),
            "market_hours_only": s.market_hours_only,
        }

    def _market_open(self) -> bool:
        try:
            return market_is_open(self.ctx)
        except Exception:
            return False

    # -- the job ----------------------------------------------------- #
    def _job(self) -> None:
        crypto_only = (
            not self.ctx.settings.symbols and self.ctx.settings.crypto_symbols
        )
        if not crypto_only and not self._market_open():
            log.debug("scan.market_closed_skip")
            return
        self._run_and_record()

    def _run_and_record(self) -> None:
        # One cycle at a time. A slow Claude call must not let the next
        # tick start a second pass over the same symbols.
        if not self._lock.acquire(blocking=False):
            log.info("scan.already_running_skip")
            return
        self._busy = True
        started = datetime.now(timezone.utc)
        try:
            summary = run_one_cycle(self.ctx)
            self._last_run = {
                "at": started.isoformat(),
                "summary": summary,
                "error": None,
            }
            log.info("scan.cycle_done", summary=summary)
        except Exception as exc:
            # A failed cycle must never kill the loop; the next one may work.
            self._last_run = {
                "at": started.isoformat(),
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
            log.exception("scan.cycle_failed")
        finally:
            self._busy = False
            self._lock.release()

    # -- control ----------------------------------------------------- #
    def start(self) -> tuple[bool, str]:
        if not self.ctx.settings.has_llm_key:
            return False, (
                "No Claude API key, so there is nothing to analyse with. "
                "Add one with ./start.sh setkey and restart."
            )
        if self.running:
            return False, "Already running."
        if not self._scheduler.running:
            self._scheduler.start()
        minutes = self.ctx.settings.scan_interval_minutes
        self._scheduler.add_job(
            self._job,
            IntervalTrigger(minutes=minutes),
            id="scan",
            name="market scan",
            max_instances=1,
            coalesce=True,  # a slow cycle must not build a backlog
            misfire_grace_time=120,
            replace_existing=True,
        )
        # Don't make the user wait a whole interval to see it do something.
        # No explicit id: APScheduler deletes a one-off job once it has run,
        # and a fixed id raced with that during shutdown ("No job by the id
        # of scan_now was found" out of the scheduler thread).
        self._scheduler.add_job(self._job, name="first scan")
        log.info(
            "scan.started",
            interval_minutes=minutes,
            execution_mode=self.ctx.settings.execution_mode,
        )
        return True, f"Scanning every {minutes} min."

    def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "Not running."
        self._scheduler.remove_job("scan")
        log.info("scan.stopped")
        return True, "Stopped. Open positions and their broker-side stops are untouched."

    def scan_now(self) -> tuple[bool, str]:
        """One pass, whether or not the loop is on."""
        if not self.ctx.settings.has_llm_key:
            return False, "No Claude API key — add one with ./start.sh setkey."
        if self._busy:
            return False, "A scan is already in progress."
        if not self._scheduler.running:
            self._scheduler.start()
        self._scheduler.add_job(self._run_and_record, name="manual scan")
        return True, "Scanning now."

    def shutdown(self) -> None:
        if not self._scheduler.running:
            return
        # Drop the repeating job first so nothing new starts while we wait.
        try:
            self._scheduler.remove_all_jobs()
        except Exception:  # pragma: no cover - best effort during teardown
            pass
        # wait=True on purpose. A cycle killed between placing an entry and
        # placing its protective stop would leave a position unguarded,
        # which is a far worse outcome than a slow shutdown — and the
        # cycle is bounded by the broker and Claude request timeouts.
        # It also stops APScheduler racing to tidy a finished one-off job
        # against a jobstore that teardown has already torn down.
        self._scheduler.shutdown(wait=True)
