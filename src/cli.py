"""
Command line interface.

    python -m src.cli doctor              # check the setup before anything else
    python -m src.cli scan                # one pass over the watchlist
    python -m src.cli run                 # the scheduled loop
    python -m src.cli serve               # dashboard + TradingView webhook
    python -m src.cli backtest --bars 500
    python -m src.cli pending             # what is waiting on you
    python -m src.cli approve sig_abc123
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.config import LIVE_CONFIRMATION_PHRASE, REPO_ROOT, Settings, get_settings
from src.logging_setup import configure_logging


# ====================================================================== #
#  doctor — the troubleshooting checklist, as a command
# ====================================================================== #
def cmd_doctor(args: argparse.Namespace) -> int:
    ok, warn, bad = [], [], []

    if sys.version_info >= (3, 11):
        ok.append(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        bad.append(f"Python {sys.version_info.major}.{sys.version_info.minor} — need 3.11+")

    for module in ("anthropic", "httpx", "pandas", "numpy", "pydantic_settings",
                   "structlog", "apscheduler", "fastapi"):
        try:
            __import__(module)
            ok.append(f"package {module}")
        except ImportError:
            bad.append(f"package {module} missing — pip install -r requirements-trader.txt")

    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        ok.append(".env present")
    else:
        bad.append(".env not found — run: cp .env.example .env, then fill in your keys")

    gitignore = REPO_ROOT / ".gitignore"
    if gitignore.exists() and ".env" in gitignore.read_text(encoding="utf-8"):
        ok.append(".env is gitignored")
    else:
        bad.append("SECURITY: .env is not in .gitignore — never push API keys")

    try:
        settings = get_settings()
    except Exception as exc:
        bad.append(f"settings failed to load: {exc}")
        _print_doctor(ok, warn, bad)
        return 1

    # Settings already stripped these; report what's actually in the file.
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                if value != value.strip():
                    warn.append(f"{key.strip()} has surrounding whitespace in .env")

    if settings.anthropic_api_key or settings.llm_provider == "openrouter":
        ok.append(f"LLM provider: {settings.llm_provider} ({settings.claude_model})")
    else:
        bad.append("ANTHROPIC_API_KEY is empty — the analysis brain cannot run")

    if settings.has_broker_keys:
        ok.append("Alpaca keys present")
    else:
        warn.append("no Alpaca keys — the bot will use the local simulator and CSV data")

    if settings.news_api_key:
        ok.append("NewsAPI key present")
    else:
        warn.append("no NEWS_API_KEY — the AI will see price only, no headlines")

    if settings.discord_webhook_url or (settings.telegram_bot_token and settings.telegram_chat_id):
        ok.append("alerts configured")
    else:
        warn.append("no alert channel — you will not be told about signals")

    if settings.live_trading_enabled:
        warn.append("LIVE TRADING IS ENABLED — real orders, real money")
    else:
        ok.append(f"paper mode (blockers: {', '.join(settings.live_mode_blockers())})")

    if settings.execution_mode == "auto":
        warn.append("EXECUTION_MODE=auto — orders are placed without your approval")
    else:
        ok.append("alert mode — nothing is ordered without your approval")

    ok.append(
        f"risk: {settings.risk_per_trade_pct:g}%/trade, "
        f"-{settings.daily_loss_limit_pct:g}% daily stop, "
        f"-{settings.max_drawdown_pct:g}% max drawdown"
    )

    # Live connectivity checks are opt-in — `doctor` should be safe offline.
    if args.check_apis:
        _check_apis(settings, ok, warn, bad)

    _print_doctor(ok, warn, bad)
    return 1 if bad else 0


def _check_apis(settings: Settings, ok: list, warn: list, bad: list) -> None:
    try:
        from src.ai.client import LLMClient

        client = LLMClient(settings)
        response = client.complete("Reply with exactly: OK", "Say OK.", max_tokens=16)
        client.close()
        ok.append(f"Claude reachable ({response.model or settings.claude_model})")
    except Exception as exc:
        bad.append(f"Claude call failed: {exc}")

    if settings.has_broker_keys:
        try:
            from src.broker.alpaca import AlpacaBroker

            broker = AlpacaBroker(settings)
            account = broker.get_account()
            ok.append(
                f"Alpaca reachable — equity ${account.equity:,.2f} "
                f"({'paper' if broker.is_paper else 'LIVE'})"
            )
            broker.close()
        except Exception as exc:
            bad.append(f"Alpaca call failed: {exc}")


def _print_doctor(ok: list, warn: list, bad: list) -> None:
    print("\n" + "=" * 58)
    print("SETUP CHECK")
    print("=" * 58)
    for item in ok:
        print(f"  [ ok ] {item}")
    for item in warn:
        print(f"  [warn] {item}")
    for item in bad:
        print(f"  [FAIL] {item}")
    print("=" * 58)
    print("Ready." if not bad else f"{len(bad)} problem(s) to fix before running.")
    print()


# ====================================================================== #
#  Trading commands
# ====================================================================== #
def _build(args: argparse.Namespace):
    from src.app import build_app

    return build_app(
        strategies=args.strategies.split(",") if getattr(args, "strategies", None) else None,
        use_debate=getattr(args, "debate", False),
        always_analyse=getattr(args, "always_analyse", False),
        force_simulated=getattr(args, "simulated", False),
    )


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
        result = ctx.pipeline.run_cycle(symbols)
        print("\n" + result.summary())
        for error in result.errors:
            print(f"  error: {error}")
        if result.pending:
            print(f"\n{result.pending} signal(s) awaiting approval — "
                  "run `python -m src.cli pending`.")
        return 2 if result.halted else 0
    finally:
        ctx.close()


def cmd_run(args: argparse.Namespace) -> int:
    from src.scheduler import BotScheduler

    ctx = _build(args)
    try:
        BotScheduler(ctx).start()
        return 0
    finally:
        ctx.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from src.webhook_server import run_server

    run_server()
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from src.backtest import Backtester
    from src.data.market_data import build_market_data
    from strategies.base import load_strategies

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_path)

    market_data = build_market_data(settings)
    strategies = load_strategies(args.strategies.split(",") if args.strategies else None)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else settings.all_symbols

    result = Backtester(settings, market_data, strategies, args.equity).run(symbols, args.bars)
    print("\n" + result.report())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        print("\n" + ctx.pipeline.daily_report())
        return 0
    finally:
        ctx.close()


def cmd_analyze(args: argparse.Namespace) -> int:
    from src.data.indicators import build_snapshot

    ctx = _build(args)
    try:
        symbol = args.symbol.upper()
        candles = ctx.market_data.get_candles(symbol, "1Day", limit=250)
        snapshot = build_snapshot(symbol, candles)
        if snapshot is None:
            print(f"Not enough history for {symbol}.")
            return 1

        headlines = ctx.news.get_headlines(symbol)
        news = ctx.analyst.news_analysis(symbol, headlines)

        print(f"\n=== {symbol} ===")
        print(f"Last {snapshot.last_price:,.2f} ({snapshot.change_pct:+.2f}%) | "
              f"{snapshot.trend} | RSI {snapshot.rsi14:.0f} | ATR {snapshot.atr14:.2f}")
        print(f"\n--- NEWS ({news.headline_count} headlines, impact {news.impact}) ---")
        print(news.summary or "(no headlines)")
        print("\n--- MARKET ANALYSIS ---")
        print(ctx.analyst.market_analysis(snapshot) or "(no response)")
        print("\n--- CHART BREAKDOWN ---")
        print(ctx.analyst.chart_breakdown(snapshot) or "(no response)")
        return 0
    finally:
        ctx.close()


def cmd_pending(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        entries = ctx.approvals.pending()
        if not entries:
            print("Nothing awaiting approval.")
            return 0
        print(f"\n{len(entries)} signal(s) awaiting approval:\n")
        for entry in entries:
            s, z = entry["signal"], entry["sizing"]
            print(f"  {s['id']}  {s['symbol']:<8} {s['direction']:<5} "
                  f"entry {s['entry']:>9,.2f}  stop {s['stop']:>9,.2f}  "
                  f"target {s['target']:>9,.2f}  R:R {s.get('risk_reward', 0):.2f}")
            print(f"      size {z['shares']:g}  risk ${z['dollar_risk']:,.2f} "
                  f"({z['account_risk_pct']:.2f}%)")
            print(f"      {(s.get('reasoning') or '')[:160]}\n")
        print("Approve with: python -m src.cli approve <id>")
        return 0
    finally:
        ctx.close()


def cmd_approve(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        ok, message = ctx.execution.approve(args.signal_id)
        print(message)
        return 0 if ok else 1
    finally:
        ctx.close()


def cmd_deny(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        ok, message = ctx.execution.deny(args.signal_id)
        print(message)
        return 0 if ok else 1
    finally:
        ctx.close()


def cmd_positions(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        state = ctx.portfolio.snapshot()
        print(f"\nEquity ${state.account.equity:,.2f} | day {state.day_pnl_pct:+.2f}% | "
              f"drawdown -{state.drawdown_pct:.2f}% | open risk ${state.open_risk:,.2f}")
        if not state.positions:
            print("No open positions.")
            return 0
        print()
        for p in state.positions:
            print(f"  {p.symbol:<10} {p.qty:>10g} @ {p.avg_entry:>9,.2f}  "
                  f"now {p.market_price:>9,.2f}  {p.unrealized_pnl:+,.2f}")
        return 0
    finally:
        ctx.close()


def cmd_close(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        ok, message = ctx.execution.close_position(args.symbol.upper(), reason="manual close")
        print(message)
        return 0 if ok else 1
    finally:
        ctx.close()


def cmd_metrics(args: argparse.Namespace) -> int:
    from src.metrics import compute_metrics

    ctx = _build(args)
    try:
        m = compute_metrics(ctx.journal.closed_trades(limit=5000), ctx.journal.equity_curve())
        print("\n" + m.summary())
        if m.by_strategy:
            print("\nBy strategy:")
            for name, stats in sorted(m.by_strategy.items()):
                print(f"  {name:<18} {stats['trades']:>4} trades  "
                      f"win {stats['win_rate']:>5.1f}%  avg {stats['avg_r']:+.2f}R  "
                      f"${stats['total_pnl']:+,.2f}")
        return 0
    finally:
        ctx.close()


def cmd_review(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        if args.trade_id:
            trade = ctx.journal.get_trade(args.trade_id)
            if trade is None:
                print(f"No trade {args.trade_id}.")
                return 1
            trades = [trade]
        else:
            trades = ctx.journal.closed_trades(limit=args.limit)
            if not trades:
                print("No closed trades to review yet.")
                return 0

        for trade in trades:
            review = ctx.reviewer.review_trade(trade)
            if review:
                ctx.journal.save_review(trade["id"], review)
                print(f"\n=== {trade['symbol']} {trade['id']} "
                      f"({trade['r_multiple']:+.2f}R) ===\n{review}")
        return 0
    finally:
        ctx.close()


def cmd_optimize(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        trades = ctx.journal.closed_trades(limit=1000, strategy=args.strategy)
        print("\n" + (ctx.reviewer.optimize_strategy(args.strategy, trades) or "(no response)"))
        return 0
    finally:
        ctx.close()


def cmd_portfolio_review(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        state = ctx.portfolio.snapshot()
        print("\n" + (ctx.analyst.portfolio_review(
            state.positions, state.account.equity, state.open_risk
        ) or "(no response)"))
        return 0
    finally:
        ctx.close()


def cmd_export(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        path = ctx.journal.export_csv(Path(args.out))
        print(f"Exported to {path}")
        return 0
    finally:
        ctx.close()


def cmd_reset_drawdown(args: argparse.Namespace) -> int:
    ctx = _build(args)
    try:
        state = ctx.portfolio.snapshot()
        print(f"Current drawdown: -{state.drawdown_pct:.2f}% from peak "
              f"${state.peak_equity:,.2f} (equity ${state.account.equity:,.2f})")
        if not args.yes:
            answer = input("Clear the drawdown pause? Only do this AFTER reviewing "
                           "what went wrong. [y/N] ")
            if answer.strip().lower() != "y":
                print("Left as-is.")
                return 0
        ctx.portfolio.reset_peak()
        print("Drawdown pause cleared. The high-water mark resets to current equity.")
        return 0
    finally:
        ctx.close()


# ====================================================================== #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="AI Trading Bot — data in, AI analysis, risk plan, paper trade, alerts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Live trading requires TRADING_MODE=live, ALPACA_PAPER=false and "
               f"CONFIRM_LIVE={LIVE_CONFIRMATION_PHRASE}. Paper-trade first.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--strategies", help="comma-separated: trend_breakout,mean_reversion")
        p.add_argument("--debate", action="store_true",
                       help="run the bull/bear/risk-manager debate (3x the model calls)")
        p.add_argument("--always-analyse", dest="always_analyse", action="store_true",
                       help="send every symbol to the AI, not just strategy triggers")
        p.add_argument("--simulated", action="store_true",
                       help="run fully offline: simulated broker + data/csv/*.csv prices")

    p = sub.add_parser("doctor", help="check the setup and diagnose problems")
    p.add_argument("--check-apis", action="store_true",
                   help="also make one live call to Claude and Alpaca")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("scan", help="run one scan of the watchlist")
    p.add_argument("--symbols", help="override the watchlist for this run")
    add_common(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("run", help="run the scheduled scan loop")
    add_common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="run the dashboard and TradingView webhook")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("backtest", help="backtest the mechanical rules on past data")
    p.add_argument("--symbols")
    p.add_argument("--bars", type=int, default=500)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--strategies")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("report", help="build and send the daily report")
    add_common(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("analyze", help="one-off AI analysis of a symbol")
    p.add_argument("symbol")
    add_common(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("pending", help="list signals awaiting your approval")
    add_common(p)
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("approve", help="approve a pending signal and place the order")
    p.add_argument("signal_id")
    add_common(p)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("deny", help="deny a pending signal")
    p.add_argument("signal_id")
    add_common(p)
    p.set_defaults(func=cmd_deny)

    p = sub.add_parser("positions", help="show account and open positions")
    add_common(p)
    p.set_defaults(func=cmd_positions)

    p = sub.add_parser("close", help="close an open position")
    p.add_argument("symbol")
    add_common(p)
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("metrics", help="performance metrics from the journal")
    add_common(p)
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("review", help="AI review of closed trades")
    p.add_argument("--trade-id")
    p.add_argument("--limit", type=int, default=5)
    add_common(p)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("optimize", help="AI review of a strategy's trade history")
    p.add_argument("strategy")
    add_common(p)
    p.set_defaults(func=cmd_optimize)

    p = sub.add_parser("portfolio-review", help="AI review of concentration and correlation")
    add_common(p)
    p.set_defaults(func=cmd_portfolio_review)

    p = sub.add_parser("export", help="export closed trades to CSV")
    p.add_argument("--out", default="logs/trades.csv")
    add_common(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("reset-drawdown", help="clear a max-drawdown pause after review")
    p.add_argument("--yes", action="store_true")
    add_common(p)
    p.set_defaults(func=cmd_reset_drawdown)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print(f"\nError: {exc}\n", file=sys.stderr)
        print("Run `python -m src.cli doctor` to check your setup.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
