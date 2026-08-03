"""
Structured run logs.

Console output stays human-readable; a JSONL copy lands in logs/bot.jsonl so a
bad run can be reconstructed after the fact. "Log everything; test one piece at
a time" is the only debugging strategy that works on a live pipeline.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

_CONFIGURED = False


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "bot.jsonl", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(file_handler)

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str):
    return structlog.get_logger(name)
