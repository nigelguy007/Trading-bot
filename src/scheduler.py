"""
The scan loop.

Runs the pipeline every SCAN_INTERVAL_MINUTES during market hours and fires
the daily report at the close. Crypto has no market hours, so a crypto-only
watchlist keeps scanning around the clock.
"""
from __future__ import annotations

import signal as signal_module
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.app import AppContext
from src.logging_setup import get_logger

log = get_logger(__name__)

EASTERN = ZoneInfo("America/New_York")


def market_is_open(ctx: AppContext) -> bool:
    """
    Ask the broker's clock; fall back to a simple Eastern-time window.

    The broker knows about holidays and half-days, which is exactly the sort of
    thing a hand-rolled calendar gets wrong twice a year.
    """
    if not ctx.settings.market_hours_only:
        return True
    try:
        return ctx.broker.is_market_open()
    except Exception:
        now = datetime.now(EASTERN)
        if now.weekday() >= 5:
            return False
        return (now.hour, now.minute) >= (9, 30) and now.hour < 16


class BotScheduler:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.scheduler = BlockingScheduler(timezone=EASTERN)

    def _scan_job(self) -> None:
        crypto_only = not self.ctx.settings.symbols and self.ctx.settings.crypto_symbols
        if not crypto_only and not market_is_open(self.ctx):
            log.debug("scheduler.market_closed_skip")
            return
        try:
            result = self.ctx.pipeline.run_cycle()
            log.info("scheduler.cycle_done", summary=result.summary())
        except Exception:
            # A failed cycle must not kill the scheduler — the next one may work.
            log.exception("scheduler.cycle_failed")

    def _report_job(self) -> None:
        try:
            self.ctx.pipeline.daily_report()
        except Exception:
            log.exception("scheduler.report_failed")

    def start(self) -> None:
        s = self.ctx.settings

        self.scheduler.add_job(
            self._scan_job,
            IntervalTrigger(minutes=s.scan_interval_minutes),
            id="scan",
            name="market scan",
            max_instances=1,
            coalesce=True,  # a slow cycle must not queue up a backlog
            misfire_grace_time=120,
        )
        self.scheduler.add_job(
            self._report_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour=s.report_time.hour,
                minute=s.report_time.minute,
                timezone=EASTERN,
            ),
            id="daily_report",
            name="daily report",
        )

        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            signal_module.signal(sig, self._shutdown)

        log.info(
            "scheduler.starting",
            interval_minutes=s.scan_interval_minutes,
            report_at=s.daily_report_time,
            symbols=len(s.all_symbols),
            execution_mode=s.execution_mode,
            paper=self.ctx.broker.is_paper,
        )
        print(
            f"Scanning {len(s.all_symbols)} symbols every {s.scan_interval_minutes} min "
            f"({'PAPER' if self.ctx.broker.is_paper else 'LIVE'} / {s.execution_mode} mode). "
            "Ctrl-C to stop."
        )

        # Don't wait a full interval for the first scan.
        self._scan_job()
        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass

    def _shutdown(self, *_args) -> None:
        log.info("scheduler.shutting_down")
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
