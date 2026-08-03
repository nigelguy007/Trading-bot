"""
Trading rules and prompt templates.

Strategies are mechanical filters that run *before* the AI: they decide whether
a symbol is even worth a model call. That keeps token spend proportional to
opportunity and gives the backtester something deterministic to test, since you
cannot backtest a model call from six months ago.
"""
from strategies.base import Strategy, load_strategies
from strategies.mean_reversion import MeanReversion
from strategies.trend_breakout import TrendBreakout

__all__ = ["Strategy", "TrendBreakout", "MeanReversion", "load_strategies"]
