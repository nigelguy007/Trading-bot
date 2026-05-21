"""
Standalone CLI runner — executes one full workflow cycle.
Usage: python scripts/run_bot.py [--live] [--bankroll 1000]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog

from agents.risk_agent import PortfolioState
from orchestrator.workflow import TradingWorkflow

log = structlog.get_logger(__name__)


async def main(dry_run: bool, bankroll: float) -> None:
    log.info("bot.starting", dry_run=dry_run, bankroll=bankroll)

    workflow = TradingWorkflow(dry_run=dry_run)
    portfolio = PortfolioState(
        bankroll=bankroll,
        daily_pnl=0.0,
        open_positions_value=0.0,
        open_position_count=0,
        daily_trades=0,
    )

    try:
        result = await workflow.run(portfolio=portfolio)
        print("\n" + "=" * 60)
        print(f"  Run ID          : {result.run_id}")
        print(f"  Markets Scanned : {result.total_markets_scanned}")
        print(f"  Opportunities   : {result.opportunities_found}")
        print(f"  Trades Attempted: {result.trades_attempted}")
        print(f"  Trades Filled   : {result.trades_filled}")
        print(f"  Errors          : {len(result.errors)}")
        print("=" * 60 + "\n")

        if result.results:
            print("Trade Details:")
            for pr in result.results:
                if pr.trade and pr.trade.status == "filled":
                    print(
                        f"  [{pr.opportunity.platform}] {pr.opportunity.question[:60]}..."
                        f"\n    Direction: {pr.edge.direction.upper()} | "
                        f"Edge: {pr.edge.edge*100:.1f}% | "
                        f"Size: ${pr.risk.position_size_usd:.2f} | "
                        f"Fill: {pr.trade.avg_fill_price:.4f}"
                    )
    finally:
        await workflow.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run one trading cycle.")
    parser.add_argument("--live", action="store_true", help="Execute real trades (default: dry-run)")
    parser.add_argument("--bankroll", type=float, default=1000.0, help="Portfolio bankroll in USD")
    args = parser.parse_args()

    asyncio.run(main(dry_run=not args.live, bankroll=args.bankroll))
