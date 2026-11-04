"""
The web layer: TradingView webhook, mobile approve/deny, and a live dashboard.

Three jobs:

* `/webhook/tradingview` — a TradingView alert fires a scan of the symbols it
  names, so chart alerts can drive the bot instead of only the timer.
* `/webhook/telegram` — the Approve/Deny buttons come back here.
* `/` — the portfolio dashboard: equity, exposure, open positions, pending
  approvals, recent signals and running metrics in one view.

The TradingView endpoint is unauthenticated by nature — anyone can POST to it
— so a shared secret is mandatory and requests without it are dropped. It can
only ever trigger *analysis* of watchlisted symbols; it can never place an
order.
"""
from __future__ import annotations

import hmac
import subprocess
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.app import AppContext, build_app
from src.config import get_settings
from src.broker.base import BrokerError
from src.logging_setup import get_logger
from src.metrics import compute_metrics
from src.auth import DashboardAuth
from src.runner import BackgroundScanner

log = get_logger(__name__)

_ctx: Optional[AppContext] = None


def get_context() -> AppContext:
    if _ctx is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("application context is not initialised")
    return _ctx


@lru_cache(maxsize=1)
def build_id() -> str:
    """
    The commit this server is running, shown on the page.

    This exists because "I pulled but the page hasn't changed" took three
    rounds to diagnose and the answer was never visible from the browser:
    an old process kept serving old HTML while `git pull` reported success.
    A short SHA in the header settles it in one glance — compare it to
    `git rev-parse --short HEAD` and you know whether the running process
    predates the pull.

    Cached: this is read on every /api/health poll and the commit cannot
    change under a running process.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # No git, no repo, or a tarball download. The page still works; it
        # just cannot name the commit.
        pass
    return "unknown"


_scanner: Optional["BackgroundScanner"] = None


def get_scanner() -> "BackgroundScanner":
    if _scanner is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("scanner is not initialised")
    return _scanner


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ctx, _scanner
    # The dashboard is useful without a key — it shows the portfolio, the
    # journal and past signals. Only live analysis needs Claude.
    _ctx = build_app(require_llm=False)

    # The scan loop now lives here rather than only in the `run` command.
    # `./start.sh` starts `serve`, so before this the ordinary way to run
    # the bot produced a dashboard that never analysed anything, and
    # nothing on the page said so.
    _scanner = BackgroundScanner(_ctx)
    if _ctx.settings.autostart_scanner:
        started, message = _scanner.start()
        log.info("server.autostart", started=started, message=message)

    log.info("server.started", symbols=len(_ctx.settings.all_symbols))
    try:
        yield
    finally:
        _scanner.shutdown()
        _scanner = None
        _ctx.close()
        _ctx = None


app = FastAPI(title="AI Trading Bot", version="1.0.0", lifespan=lifespan)

# Every endpoint below sits behind this. Without DASHBOARD_PASSWORD it
# serves loopback only, so a local checkout is unchanged and a public
# deploy cannot accidentally expose the account.
app.add_middleware(DashboardAuth, settings=get_settings())


@app.get("/api/live")
async def live() -> dict:
    """
    Liveness for a platform health check. Deliberately says nothing about
    the account, because it is the one endpoint reachable without
    credentials.
    """
    return {"status": "ok"}


# ====================================================================== #
#  TradingView
# ====================================================================== #
@app.post("/webhook/tradingview")
async def tradingview_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    ctx = get_context()
    expected = ctx.settings.tradingview_webhook_secret

    if not expected:
        raise HTTPException(
            status_code=503,
            detail="TRADINGVIEW_WEBHOOK_SECRET is not configured; endpoint disabled.",
        )

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")

    if not hmac.compare_digest(str(payload.get("secret", "")), expected):
        log.warning("server.tradingview_bad_secret", client=request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="bad secret")

    raw = payload.get("symbol") or payload.get("ticker") or ""
    symbols = [s.strip().upper() for s in str(raw).replace(";", ",").split(",") if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbol in payload")

    # Only analyse names already on the watchlist. An alert must not be able to
    # introduce a new instrument to the bot.
    known = set(ctx.settings.all_symbols)
    accepted = [s for s in symbols if s in known]
    ignored = [s for s in symbols if s not in known]
    if not accepted:
        return JSONResponse(
            {"status": "ignored", "reason": "not on the watchlist", "symbols": ignored}
        )

    background.add_task(_run_scan, accepted)
    log.info("server.tradingview_alert", symbols=accepted, ignored=ignored or None)
    return JSONResponse({"status": "scanning", "symbols": accepted, "ignored": ignored})


def _run_scan(symbols: list[str]) -> None:
    try:
        result = get_context().pipeline.run_cycle(symbols)
        log.info("server.scan_done", summary=result.summary())
    except Exception:
        log.exception("server.scan_failed", symbols=symbols)


# ====================================================================== #
#  Telegram approve / deny
# ====================================================================== #
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    ctx = get_context()
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="body must be JSON")

    callback = update.get("callback_query")
    if not callback:
        return JSONResponse({"status": "ignored"})

    # Only the configured chat may approve trades.
    chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
    if chat_id and chat_id != str(ctx.settings.telegram_chat_id):
        log.warning("server.telegram_wrong_chat", chat_id=chat_id)
        raise HTTPException(status_code=403, detail="unauthorised chat")

    action, _, signal_id = str(callback.get("data", "")).partition(":")
    if action not in ("approve", "deny") or not signal_id:
        return JSONResponse({"status": "ignored"})

    ok, message = (
        ctx.execution.approve(signal_id) if action == "approve" else ctx.execution.deny(signal_id)
    )
    ctx.alerts.telegram.answer_callback(callback.get("id", ""), message)
    ctx.alerts.telegram.send(message)
    return JSONResponse({"status": "ok" if ok else "failed", "message": message})


# ====================================================================== #
#  JSON API
# ====================================================================== #
@app.get("/api/health")
async def health() -> dict:
    ctx = get_context()
    return {
        "status": "ok",
        "build": build_id(),
        "mode": "live" if ctx.settings.live_trading_enabled else "paper",
        "execution_mode": ctx.settings.execution_mode,
        "model": ctx.llm.model,
        "llm_ready": getattr(ctx.llm, "enabled", True),
        "broker": " + ".join(ctx.brokers.names),
        # The broker learns the real account currency at login; fall back to
        # the configured one until then.
        "currency_symbol": _symbol_for(
            getattr(ctx.broker, "_currency", "") or ctx.settings.ig_currency
        )
        if ctx.settings.spread_betting
        else "$",
        "size_unit": ctx.settings.size_unit,
        "symbols": ctx.settings.all_symbols,
        # The configured limits, so the dashboard can show each figure
        # against the line that stops trading. "-0.8%" means nothing on its
        # own; "-0.8% of -3%" tells you how much rope is left.
        "risk_per_trade_pct": ctx.settings.risk_per_trade_pct,
        "daily_loss_limit_pct": ctx.settings.daily_loss_limit_pct,
        "max_drawdown_pct": ctx.settings.max_drawdown_pct,
        "max_portfolio_risk_pct": ctx.settings.max_portfolio_risk_pct,
        "max_open_positions": ctx.settings.max_open_positions,
    }


CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CHF": "CHF "}


def _symbol_for(currency: str) -> str:
    code = (currency or "").upper()
    return CURRENCY_SYMBOLS.get(code, f"{code} " if code else "$")

EMPTY_PORTFOLIO = {
    "equity": 0.0, "cash": 0.0, "buying_power": 0.0, "day_pnl": 0.0,
    "day_pnl_pct": 0.0, "drawdown_pct": 0.0, "peak_equity": 0.0,
    "exposure": 0.0, "exposure_pct": 0.0, "open_risk": 0.0,
    "open_risk_pct": 0.0, "positions": [], "venues": [], "unreachable": [],
}


@app.get("/api/portfolio")
async def portfolio() -> dict:
    ctx = get_context()
    try:
        state = ctx.portfolio.snapshot()
    except BrokerError as exc:
        # A wrong key or a broker outage must not blank the whole dashboard.
        # The page fetches five endpoints together, so a 500 here used to leave
        # the user staring at "loading…" with the real reason only in the log.
        log.error("dashboard.broker_unreachable", error=str(exc))
        return {**EMPTY_PORTFOLIO, "error": str(exc)}
    return {
        "equity": state.account.equity,
        "cash": state.account.cash,
        "buying_power": state.account.buying_power,
        "day_pnl": state.account.day_pnl,
        "day_pnl_pct": state.day_pnl_pct,
        "drawdown_pct": state.drawdown_pct,
        "peak_equity": state.peak_equity,
        "exposure": state.exposure,
        "exposure_pct": state.exposure_pct,
        "open_risk": state.open_risk,
        "open_risk_pct": state.open_risk_pct,
        "unreachable": state.unreachable,
        "venues": [
            {
                "name": v.name,
                "equity": v.account.equity,
                "cash": v.account.cash,
                "exposure": v.exposure,
                "positions": len(v.positions),
                "error": v.error,
            }
            for v in state.venues
        ],
        "positions": [
            {
                "symbol": p.symbol,
                "venue": p.venue,
                "qty": p.qty,
                "avg_entry": p.avg_entry,
                "market_price": p.market_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in state.positions
        ],
    }


@app.get("/api/signals")
async def signals(limit: int = 25) -> dict:
    return {"signals": get_context().journal.recent_signals(limit)}


@app.get("/api/pending")
async def pending() -> dict:
    return {"pending": get_context().approvals.pending()}


@app.get("/api/trades")
async def trades(limit: int = 50) -> dict:
    journal = get_context().journal
    return {"open": journal.open_trades(), "closed": journal.closed_trades(limit)}


@app.get("/api/equity")
async def equity(limit: int = 240) -> dict:
    """
    The equity curve, for the chart.

    The journal has recorded this on every cycle since the first run and
    nothing ever surfaced it. Trimmed to `limit` points because the chart is
    a few hundred pixels wide and an eight-month curve at one point per scan
    is tens of thousands of rows.
    """
    curve = get_context().journal.equity_curve(limit=limit)
    return {
        "points": [
            {"ts": row["ts"], "equity": row["equity"], "exposure": row.get("exposure", 0.0)}
            for row in curve
        ]
    }


@app.get("/api/metrics")
async def metrics() -> dict:
    journal = get_context().journal
    return compute_metrics(journal.closed_trades(limit=1000), journal.equity_curve()).to_dict()


@app.post("/api/signals/{signal_id}/approve")
async def approve(signal_id: str) -> JSONResponse:
    ok, message = get_context().execution.approve(signal_id)
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


@app.post("/api/signals/{signal_id}/deny")
async def deny(signal_id: str) -> JSONResponse:
    ok, message = get_context().execution.deny(signal_id)
    return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)


@app.post("/api/scan")
async def scan(background: BackgroundTasks) -> dict:
    background.add_task(_run_scan, get_context().settings.all_symbols)
    return {"status": "scanning"}


# ====================================================================== #
#  Starting and stopping the bot
# ====================================================================== #
# Before these, the only way to make the bot actually work was a second
# command in a second terminal (`./start.sh run`). The dashboard showed an
# idle account and gave no hint that nothing was scanning.
@app.get("/api/bot")
async def bot_state() -> dict:
    return get_scanner().state()


@app.post("/api/bot/start")
async def bot_start() -> JSONResponse:
    ok, message = get_scanner().start()
    return JSONResponse(
        {"ok": ok, "message": message, **get_scanner().state()},
        status_code=200 if ok else 409,
    )


@app.post("/api/bot/stop")
async def bot_stop() -> JSONResponse:
    ok, message = get_scanner().stop()
    return JSONResponse(
        {"ok": ok, "message": message, **get_scanner().state()},
        status_code=200 if ok else 409,
    )


@app.post("/api/bot/scan-now")
async def bot_scan_now() -> JSONResponse:
    ok, message = get_scanner().scan_now()
    return JSONResponse(
        {"ok": ok, "message": message, **get_scanner().state()},
        status_code=200 if ok else 409,
    )


# ====================================================================== #
#  Dashboard
# ====================================================================== #
# Both pages are generated fresh on every request and are tiny, so there is
# nothing to gain by caching them and a great deal to lose: `/` served the
# dashboard until recently, and a browser holding that cached copy shows the
# OLD page after an upgrade with no way for the user to tell. That cost
# several rounds of debugging. no-store ends it — a plain reload is always
# enough, and Cmd-Shift-R is never needed.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """
    The opening page: one full-screen frame, then a click through to the desk.

    This is a separate page rather than a band at the top of the dashboard.
    A hero that shares a scroll with a risk table is not an opening — it is
    a header, and it read as one.
    """
    return HTMLResponse(LANDING_HTML, headers=_NO_STORE)


@app.get("/desk", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML, headers=_NO_STORE)


LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading desk</title>
<style>
  /* ------------------------------------------------------------------
     The opening page: Anchor's composition, in Tradeo's dark surfaces,
     carrying Alpha Ledger's numerals.

     ANCHOR (scrolltide.co/templates/anchor) gives the bones. Its own
     description is the brief: "the composition sits low and firm in the
     frame rather than floating, so the page reads as settled rather than
     eager", for products "where trust is the purchase decision". Its
     hero is a crowd in motion with one figure standing still, a two-line
     statement bottom-left, a factual paragraph and a pill bottom-right,
     and two whispered marks at the outer edges. Motion is "weighted and
     unhurried" — and that is the half I missed the first time, so the
     page came out as a static black box.

     The crowd becomes the equity curve: the market moves all day, the
     figure in the foreground does not. Same argument, our own data.

     TRADEO (tradeo-saas.webflow.io) gives the surface ladder and the
     type, read off the running page rather than remembered: #060610
     through #191928, strokes #171720 and #252537, muted #a9a9ac, accent
     #9968FF, and a headline at weight 500 with -0.07em tracking.

     ALPHA LEDGER (dribbble 25375649) gives the data voice: tiny
     uppercase letterspaced micro-labels, and big light numerals with
     small muted captions in a row along the floor.
     ------------------------------------------------------------------ */
  :root {
    color-scheme: dark;
    --bg:        #060610;
    --surface-1: #0b0b15;
    --surface-2: #11121c;
    --stroke-1:  #171720;
    --stroke-2:  #252537;
    --accent:    #9968ff;
    --accent-dim:#7d79e7;
    --text:      #ffffff;
    --muted:     #a9a9ac;
    --pos:       #1fc16b;
    --neg:       #ff2b2b;
    /* Anchor's whole signature. Nothing here is allowed to be snappy:
       the weight of the motion is what signals dependability. */
    --ease:      cubic-bezier(.16, 1, .3, 1);
  }
  * { box-sizing:border-box; }
  html, body { height:100%; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    /* Tradeo sets Inter. No webfont: this page is served from localhost
       and must not need the internet to render. SF Pro on the Mac and
       Segoe on Windows are the closest system faces. */
    font-family:Inter,-apple-system,"SF Pro Display",BlinkMacSystemFont,
                "Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;
    font-feature-settings:"ss01","cv01","tnum";
  }
  [hidden] { display:none !important; }

  .stage {
    position:relative; overflow:hidden;
    min-height:100vh; min-height:100dvh;
    display:flex; flex-direction:column;
    background:
      radial-gradient(130% 95% at 50% 0%, #10111b 0%, var(--surface-1) 52%, #05050c 100%);
  }

  /* The backdrop drifts in slowly and never stops being the floor the
     figures stand on. 16s, because Anchor's reads as geology. */
  .stage-bg {
    position:absolute; inset:0; opacity:.7;
    transform:scale(1.06); transform-origin:50% 42%;
    animation:settle 16s var(--ease) forwards;
  }
  .stage-bg svg { width:100%; height:100%; display:block; }
  @keyframes settle { to { transform:scale(1); } }

  /* Heaviest at the floor, so the numerals read straight off the curve
     without a panel behind them. Anchor does the same over its photo. */
  .stage-veil {
    position:absolute; inset:0; pointer-events:none;
    background:linear-gradient(to bottom,
      rgba(6,6,16,.30) 0%, rgba(6,6,16,.52) 42%, rgba(6,6,16,.95) 100%);
  }
  .stage-note {
    position:absolute; z-index:2; left:0; right:0; top:40%;
    transform:translateY(-50%); text-align:center; padding:0 28px;
    color:var(--accent-dim); font-size:13px; pointer-events:none;
  }

  /* --- Top rail ---------------------------------------------------- */
  .top {
    position:relative; z-index:2; display:flex; align-items:center;
    justify-content:space-between; gap:16px; padding:24px 40px 0;
  }
  .brand { display:flex; align-items:center; gap:10px; font-size:14.5px;
           font-weight:500; letter-spacing:-.01em; }
  .brand .mark {
    width:22px; height:22px; border-radius:50%; display:grid;
    place-items:center; border:1.5px solid var(--accent);
  }
  .brand .mark i {
    width:7px; height:7px; border-radius:50%; background:var(--accent);
    display:block;
  }
  .flags { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
           justify-content:flex-end; }
  .badge { padding:5px 11px; border-radius:999px; font-size:10.5px;
           letter-spacing:.09em; white-space:nowrap; }
  .paper { background:#9968ff1c; color:var(--accent); border:1px solid #9968ff40; }
  .live  { background:#ff2b2b1c; color:var(--neg); border:1px solid #ff2b2b55; }
  .chip  { padding:5px 11px; border-radius:999px; font-size:11.5px;
           background:var(--surface-2); border:1px solid var(--stroke-2);
           color:var(--muted); white-space:nowrap; }
  .chip b { color:var(--text); font-weight:500; }

  /* --- Anchor's edge marks ----------------------------------------- */
  /* Two whispers at the outer edges, level with each other, well above
     the floor. In Anchor they read "Everyone's moving." and "Only some
     arrive." — the thesis, said quietly, twice. */
  .marks {
    position:absolute; z-index:2; top:30%; left:0; right:0;
    display:flex; justify-content:space-between; gap:20px;
    padding:0 40px; pointer-events:none;
    font-size:10.5px; letter-spacing:.13em; text-transform:uppercase;
    color:#ffffff6e;
  }
  .marks span { min-width:0; white-space:nowrap; overflow:hidden;
                text-overflow:ellipsis; }
  .marks span:last-child { text-align:right; }

  /* --- The floor ---------------------------------------------------- */
  /* margin-top:auto is the composition. Whatever the viewport height,
     everything is pinned to the bottom edge. */
  .floor { position:relative; z-index:2; margin-top:auto; padding:0 40px 34px; }

  .floor-main {
    display:flex; align-items:flex-end; justify-content:space-between;
    gap:40px; margin-bottom:30px;
  }

  /* Anchor's two-line statement, in Tradeo's type: weight 500, very
     tight tracking, lines close together. */
  .claim {
    margin:0; font-size:clamp(31px, 4.3vw, 58px); font-weight:500;
    line-height:1.06; letter-spacing:-.05em; max-width:18ch;
  }
  .claim span { display:block; opacity:0; transform:translateY(16px);
                animation:rise 1.1s var(--ease) forwards; }
  .claim span:nth-child(1) { animation-delay:.10s; }
  .claim span:nth-child(2) { animation-delay:.24s; }
  @keyframes rise { to { opacity:1; transform:none; } }

  .aside { max-width:380px; opacity:0; transform:translateY(16px);
           animation:rise 1.1s var(--ease) .40s forwards; }
  .copy { color:var(--muted); font-size:13.5px; line-height:1.6; margin:0 0 20px; }
  .cta { display:flex; gap:14px; align-items:center; justify-content:flex-end;
         flex-wrap:wrap; }
  .enter {
    display:inline-flex; align-items:center; gap:9px; text-decoration:none;
    background:var(--accent); color:#0c0618; font-size:14px; font-weight:600;
    padding:14px 24px; border-radius:999px; letter-spacing:-.01em;
    transition:filter .45s var(--ease), transform .45s var(--ease);
  }
  .enter:hover { filter:brightness(1.1); transform:translateY(-2px); }
  .enter:focus-visible { outline:2px solid #fff; outline-offset:3px; }
  .ghost {
    text-decoration:none; color:var(--muted); font-size:13.5px;
    border-bottom:1px solid transparent; padding-bottom:2px;
    transition:color .45s var(--ease), border-color .45s var(--ease);
  }
  .ghost:hover { color:var(--text); border-bottom-color:var(--stroke-2); }

  /* --- Alpha Ledger's stat row -------------------------------------- */
  /* Big light numerals, small muted captions, a hairline above. The
     live account stated as three facts — Anchor's brief says a specific
     number always beats a strong adjective for this audience. */
  .stats {
    display:flex; gap:56px; flex-wrap:wrap;
    border-top:1px solid var(--stroke-2); padding-top:20px;
  }
  .stat { opacity:0; transform:translateY(14px);
          animation:rise 1.1s var(--ease) forwards; min-width:0; }
  .stat:nth-child(1) { animation-delay:.54s; }
  .stat:nth-child(2) { animation-delay:.64s; }
  .stat:nth-child(3) { animation-delay:.74s; }
  .stat .cap {
    font-size:10px; letter-spacing:.15em; text-transform:uppercase;
    color:var(--muted); margin-bottom:9px;
  }
  .stat .num {
    font-size:clamp(23px, 2.9vw, 36px); font-weight:500;
    letter-spacing:-.04em; line-height:1; white-space:nowrap;
  }
  .pos { color:var(--pos); } .neg { color:var(--neg); }

  .hint { color:#ffffff40; font-size:11px; margin:22px 0 0; letter-spacing:.02em; }
  .hint kbd {
    font-family:ui-monospace,Menlo,monospace; font-size:10.5px;
    background:var(--surface-2); border:1px solid var(--stroke-2);
    border-radius:5px; padding:2px 6px; color:var(--muted);
  }

  @media (max-width: 860px) {
    .top { padding:18px 20px 0; flex-direction:column; align-items:flex-start;
           gap:12px; }
    .flags { justify-content:flex-start; }
    .marks { top:auto; bottom:auto; display:none; }
    .floor { padding:0 20px 26px; }
    .floor-main { flex-direction:column; align-items:stretch; gap:26px;
                  margin-bottom:24px; }
    .claim { max-width:none; }
    .aside { max-width:none; }
    .cta { flex-direction:column; align-items:stretch; gap:8px; }
    .enter { justify-content:center; white-space:nowrap; padding:16px 20px; }
    .ghost { text-align:center; padding:12px 4px; }
    .stats { gap:26px 34px; }
    .hint { display:none; }
  }

  /* Anchor's motion is the point, but it is not worth making anyone ill. */
  @media (prefers-reduced-motion: reduce) {
    .stage-bg { animation:none; transform:none; }
    .claim span, .aside, .stat { animation:none; opacity:1; transform:none; }
    .enter, .ghost { transition:none; }
  }
</style>
</head>
<body>

<main class="stage">
  <div class="stage-bg" id="equity-chart"></div>
  <div class="stage-veil"></div>
  <p class="stage-note" id="note" hidden></p>

  <div class="top">
    <div class="brand"><span class="mark"><i></i></span> <span>Trading desk</span></div>
    <div class="flags" id="flags"></div>
  </div>

  <div class="marks">
    <span id="mark-left">&nbsp;</span>
    <span id="mark-right">&nbsp;</span>
  </div>

  <div class="floor">
    <div class="floor-main">
      <h1 class="claim"><span>The market moves.</span><span>Your limits don&rsquo;t.</span></h1>
      <div class="aside">
        <p class="copy" id="copy">Reading the account&hellip;</p>
        <div class="cta">
          <a class="enter" href="/desk" id="enter">Enter the trading desk <span>&rarr;</span></a>
          <a class="ghost" href="/desk#approvals">Pending approvals</a>
        </div>
      </div>
    </div>

    <div class="stats">
      <div class="stat"><div class="cap">Equity</div><div class="num" id="equity">&mdash;</div></div>
      <div class="stat"><div class="cap">Today</div><div class="num" id="day">&mdash;</div></div>
      <div class="stat"><div class="cap">Open risk</div><div class="num" id="risk">&mdash;</div></div>
    </div>

    <p class="hint">Press <kbd>Enter</kbd> to continue</p>
  </div>
</main>

<script>
let CUR = "$";
const money = n => (n<0?"-":"") + CUR +
  Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pct = n => (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// The curve is Anchor's crowd: the thing that moves, behind the figure
// that does not. Inline SVG on purpose — this page is served from
// localhost and must not need the internet to draw a line.
function curve(points) {
  const host = document.getElementById("equity-chart");
  const note = document.getElementById("note");
  const W = 1000, H = 1000, TOP = 150, BOT = H * 0.58;

  // Fewer than two readings is not a curve. An empty backdrop made the
  // whole screen look like a page that had failed to load, so draw the
  // baseline the account is sitting on — dashed and neutral, because a
  // green line across a new account implies a rise that never happened.
  if (points.length < 2) {
    const MID = (TOP + BOT) / 2;
    host.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
           aria-label="No equity history recorded yet">
        <line x1="0" y1="${MID}" x2="${W}" y2="${MID}" stroke="var(--accent-dim)"
              stroke-width="1.5" stroke-dasharray="2 9" opacity=".8"
              vector-effect="non-scaling-stroke"/>
      </svg>`;
    if (note) {
      note.textContent = points.length
        ? "Your equity curve draws itself here once there is more than one reading."
        : "Your equity curve draws itself here once the bot records its first reading.";
      note.hidden = false;
    }
    return;
  }
  if (note) note.hidden = true;

  const ys = points.map(p => p.equity);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const flat = hi === lo, span = flat ? 1 : hi - lo;
  const x = i => i * W / (points.length - 1);
  const y = v => flat ? (TOP + BOT) / 2 : BOT - ((v - lo) / span) * (BOT - TOP);
  const line = points.map((p, i) =>
    `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const up = ys[ys.length - 1] >= ys[0];
  const stroke = up ? "var(--pos)" : "var(--neg)";

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="Equity over the last ${points.length} readings">
      <defs><linearGradient id="f" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${stroke}" stop-opacity=".13"/>
        <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
      </linearGradient></defs>
      <path id="eqfill" d="${line}L${x(points.length-1).toFixed(1)},${H}L0,${H}Z"
            fill="url(#f)" opacity="0"/>
      <path id="eqline" d="${line}" fill="none" stroke="${stroke}" stroke-width="2.2"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>`;

  // Draw the line on rather than snapping it in. Slow on purpose: this
  // is the one piece of Anchor that cannot be faked with a gradient.
  const path = document.getElementById("eqline");
  const fill = document.getElementById("eqfill");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (path && !reduce) {
    const len = path.getTotalLength();
    path.style.strokeDasharray = len;
    path.style.strokeDashoffset = len;
    path.style.transition = "stroke-dashoffset 2.9s cubic-bezier(.16,1,.3,1)";
    fill.style.transition = "opacity 1.6s cubic-bezier(.16,1,.3,1) .9s";
    requestAnimationFrame(() => {
      path.style.strokeDashoffset = "0";
      fill.style.opacity = "1";
    });
  } else if (fill) {
    fill.style.opacity = "1";
  }

  const m = document.getElementById("mark-right");
  if (m) m.textContent += flat ? ` · flat at ${money(hi)}`
                               : ` · ${money(lo)} – ${money(hi)}`;
}

async function load() {
  // The page must open even when the broker is unreachable: the way
  // through to the desk is the point, and it is already in the markup.
  try {
    const [health, pf, eq, bot] = await Promise.all(
      ["/api/health", "/api/portfolio", "/api/equity", "/api/bot"]
        .map(u => fetch(u).then(r => r.json())));

    CUR = health.currency_symbol || "$";

    document.getElementById("flags").innerHTML =
      (health.mode === "live"
        ? '<span class="badge live">LIVE MONEY</span>'
        : '<span class="badge paper">PAPER</span>') +
      `<span class="chip">Venue <b>${esc(health.broker)}</b></span>` +
      `<span class="chip">build ${esc(health.build || "unknown")}</span>`;

    // Alpha Ledger's stat row, carrying the live account.
    document.getElementById("equity").textContent = money(pf.equity);

    const day = document.getElementById("day");
    day.textContent = `${pct(pf.day_pnl_pct)}  ${money(pf.day_pnl)}`;
    day.className = "num " +
      (pf.day_pnl_pct > 0 ? "pos" : pf.day_pnl_pct < 0 ? "neg" : "");

    document.getElementById("risk").textContent =
      `${pf.open_risk_pct.toFixed(2)}% of ${(health.max_portfolio_risk_pct ?? 0).toFixed(1)}%`;

    // Anchor's two marks: the thesis, said quietly, at the outer edges.
    // Whether it is actually scanning belongs on the opening page: an
    // idle account and a bot that was never switched on look identical.
    const working = bot.running
      ? (bot.market_hours_only && !bot.market_open ? "waiting for the open" : "working")
      : "paused";
    document.getElementById("mark-left").textContent =
      `${health.mode === "live" ? "Live" : "Paper"} · ${health.broker} · ` +
      `${health.execution_mode} mode · ${working}`;
    document.getElementById("mark-right").textContent =
      `${health.symbols.length} symbols · ${(pf.venues || []).length || 1} venue(s)`;

    // Plain facts. Anchor's own guidance: a specific number will always
    // outperform a strong adjective for this audience.
    document.getElementById("copy").textContent =
      `${money(pf.cash)} in cash, ${money(pf.exposure)} exposed ` +
      `(${pf.exposure_pct.toFixed(1)}%), across ${pf.positions.length} position(s). ` +
      `Trading halts by itself at ${(health.daily_loss_limit_pct ?? 0).toFixed(1)}% down on the day.`;

    curve(eq.points || []);
  } catch (e) {
    document.getElementById("copy").textContent =
      "Cannot reach the bot's API right now. The desk still opens — " +
      "run ./start.sh doctor if the figures stay empty.";
    curve([]);
  }
}

// Enter opens the desk, so the keyboard alone gets you in.
addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    location.href = "/desk";
  }
});

load();
</script>
</body>
</html>
"""


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trading Bot</title>
<style>
  /* ------------------------------------------------------------------
     Design tokens.

     The surface ladder and the violet accent are Tradeo's (read off its
     own CSS variables); the layout discipline is Alpha Ledger's — an icon
     rail, a greeting header, dense metric tables, and big light numerals
     with small muted captions.

     The one rule that governs the rest: colour belongs to data. Violet
     marks what you can act on, green and red mean money moved. Nothing
     else is allowed to be bright, which is why a losing day is visible
     from across the room.
     ------------------------------------------------------------------ */
  :root {
    color-scheme: dark;
    --bg:        #060610;
    --surface-1: #0b0b15;
    --surface-2: #11121c;
    --surface-3: #191928;
    --stroke-1:  #171720;
    --stroke-2:  #252537;
    --stroke-3:  #2f3237;
    --accent:    #9968ff;
    --accent-dim:#5d6294;
    --text:      #ffffff;
    --muted:     #a9a9ac;
    --pos:       #46d19e;
    --neg:       #ff6b6b;
    --warn:      #e3b341;
    --rail:      68px;
    --radius:    14px;
  }
  * { box-sizing: border-box; }
  /* An author `display` rule beats the browser's own [hidden] rule whatever
     the specificity, so h2{display:flex} was keeping a hidden heading on
     screen. Restore the attribute's meaning for everything. */
  [hidden] { display:none !important; }
  html, body { max-width:100%; overflow-x:hidden; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:500 15px/1.55 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  /* Tradeo's soft glow, pinned behind the header only. Fixed and
     non-interactive so it never eats a tap. */
  body::before {
    content:""; position:fixed; inset:-20% 0 auto 0; height:520px; z-index:0;
    background:
      radial-gradient(60% 100% at 22% 0%, #9968ff1c 0%, transparent 70%),
      radial-gradient(45% 90% at 85% 10%, #5d629426 0%, transparent 70%);
    pointer-events:none;
  }

  /* --- Icon rail (Alpha Ledger) ------------------------------------- */
  .rail {
    position:fixed; left:0; top:0; bottom:0; width:var(--rail); z-index:3;
    background:var(--surface-1); border-right:1px solid var(--stroke-1);
    display:flex; flex-direction:column; align-items:center; gap:4px; padding:18px 0;
  }
  .rail .mark {
    width:34px; height:34px; border-radius:10px; margin-bottom:14px;
    background:linear-gradient(145deg,var(--accent),#5f3fc4);
    display:grid; place-items:center; font-size:15px; color:#fff;
    text-decoration:none; border:0;
  }
  /* The mark is the way back to the opening page, so it must not pick up
     the plain rail-link hover and lose its gradient. */
  .rail a.mark:hover, .rail a.mark:focus-visible {
    background:linear-gradient(145deg,var(--accent),#5f3fc4);
    filter:brightness(1.12); border:0;
  }
  .rail a {
    width:40px; height:40px; border-radius:11px; display:grid; place-items:center;
    color:var(--muted); text-decoration:none; font-size:16px;
    border:1px solid transparent; transition:background .15s, color .15s;
  }
  .rail a:hover, .rail a.on { background:var(--surface-3); color:var(--text);
                              border-color:var(--stroke-2); }
  .rail .spacer { flex:1; }

  /* --- The bot's on/off control -------------------------------------- */
  .bot-row { display:flex; align-items:flex-end; justify-content:space-between;
             gap:20px; flex-wrap:wrap; }
  .bot-state { display:flex; align-items:center; gap:9px; font-size:20px;
               font-weight:500; margin-top:8px; letter-spacing:-.02em; }
  .bot-sub { color:var(--muted); font-size:12.5px; margin-top:6px; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--muted);
         flex:none; }
  /* Working is the only state that gets to pulse. Paused and
     market-closed are steady, because neither is doing anything. */
  .dot.on   { background:var(--pos); animation:beat 2.4s ease-in-out infinite; }
  .dot.wait { background:var(--warn); }
  .dot.off  { background:var(--stroke-3); }
  @keyframes beat { 0%,100% { opacity:1; } 50% { opacity:.35; } }
  .bot-actions { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  button.pill, .ghostbtn {
    font:inherit; cursor:pointer; border-radius:999px;
    transition:filter .15s, background .15s, color .15s;
  }
  button.pill { border:0; background:var(--accent); color:#0c0618;
                font-size:13.5px; font-weight:600; padding:11px 20px; }
  button.pill:hover:not(:disabled) { filter:brightness(1.1); }
  button.pill:disabled { opacity:.5; cursor:default; }
  button.pill.stop { background:var(--surface-3); color:var(--text);
                     border:1px solid var(--stroke-2); }
  .ghostbtn { background:transparent; border:1px solid var(--stroke-2);
              color:var(--muted); font-size:13px; padding:10px 16px; }
  .ghostbtn:hover:not(:disabled) { color:var(--text); background:var(--surface-2); }
  .ghostbtn:disabled { opacity:.45; cursor:default; }
  .bot-note { margin-top:14px; font-size:12.5px; color:var(--muted);
              border-top:1px solid var(--stroke-1); padding-top:12px; }
  .bot-note.warn { color:var(--warn); }
  @media (max-width: 560px) {
    .bot-row { flex-direction:column; align-items:stretch; }
    .bot-actions { flex-direction:column; align-items:stretch; }
    button.pill, .ghostbtn { width:100%; }
  }

  .shell { margin-left:var(--rail); padding:26px 30px 60px; position:relative; z-index:1; }

  /* --- Header ------------------------------------------------------- */
  .head { display:flex; justify-content:space-between; align-items:flex-start;
          gap:16px; flex-wrap:wrap; margin-bottom:22px; }
  h1 { font-size:26px; font-weight:500; margin:0 0 3px; letter-spacing:-.02em; }
  .sub { color:var(--muted); font-size:13px; }
  .chips { display:flex; gap:7px; flex-wrap:wrap; align-items:center; }
  .chip {
    display:inline-flex; align-items:center; gap:6px; font-size:12px;
    padding:6px 11px; border-radius:999px;
    background:var(--surface-2); border:1px solid var(--stroke-2); color:var(--muted);
  }
  .chip b { color:var(--text); font-weight:500; }
  .badge { display:inline-block; padding:5px 11px; border-radius:999px;
           font-size:11px; letter-spacing:.06em; }
  .paper { background:#9968ff1c; color:var(--accent); border:1px solid #9968ff40; }
  .live  { background:#ff6b6b1f; color:var(--neg); border:1px solid #ff6b6b55; }

  .banner { background:#e3b3410f; border:1px solid #e3b34155; color:var(--warn);
            border-radius:var(--radius); padding:14px 16px; margin-bottom:14px;
            font-size:13.5px; font-weight:400; }
  .banner b { font-weight:500; }
  .banner code { background:#00000066; padding:2px 6px; border-radius:5px;
                 font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }

  /* --- Hero figures (Alpha Ledger's big light numerals) -------------- */
  /* --- The hero, after Anchor ---------------------------------------
     The whole move is that nothing floats. The frame is tall, the top
     two thirds are deliberately empty, and every element is pinned to
     the floor, so the block reads as settled rather than eager. The
     backdrop is the equity curve instead of a photograph: a tool should
     be textured by its own data. */
  .anchor {
    position:relative; overflow:hidden; margin-bottom:14px;
    min-height:340px; display:flex; flex-direction:column;
    border:1px solid var(--stroke-1); border-radius:var(--radius);
    background:
      radial-gradient(120% 90% at 50% 0%, #11121c 0%, var(--surface-1) 55%, #07070f 100%);
  }
  .anchor-bg { position:absolute; inset:0; opacity:.66; }
  .anchor-bg svg { width:100%; height:100%; display:block; }
  /* Darkens toward the floor so the figures read straight off the curve
     without needing a box behind them. */
  .anchor-veil {
    position:absolute; inset:0; pointer-events:none;
    background:linear-gradient(to bottom,
      rgba(6,6,16,.25) 0%, rgba(6,6,16,.55) 45%, rgba(6,6,16,.93) 100%);
  }
  /* Shown only before there is a curve to draw. The Anchor composition
     leaves the upper frame empty on purpose, but "empty because the data
     has not arrived" and "empty because the page is broken" look identical
     — and were reported as the latter. One quiet line tells them apart. */
  .anchor-note {
    position:absolute; z-index:1; left:22px; right:22px; top:40%;
    transform:translateY(-50%); text-align:center;
    color:var(--accent-dim); font-size:12.5px; font-weight:400;
    letter-spacing:.01em; pointer-events:none;
  }
  .anchor-marks {
    position:relative; z-index:1; display:flex; justify-content:space-between;
    padding:18px 22px 0; font-size:11px; letter-spacing:.1em;
    text-transform:uppercase; color:var(--muted); gap:14px;
  }
  /* One line each. Wrapped labels eat the empty space the composition
     depends on and crowd the figure beneath them. */
  .anchor-marks span {
    min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  .anchor-marks span:last-child { text-align:right; }
  .anchor-floor {
    position:relative; z-index:1; margin-top:auto;
    display:flex; align-items:flex-end; justify-content:space-between;
    gap:28px; padding:0 22px 22px;
  }
  .anchor-lead { min-width:0; }
  .anchor-lead .figure {
    font-size:58px; line-height:1.02; font-weight:600; letter-spacing:-.035em;
    margin-top:8px;
  }
  .anchor-lead .figure.sm { font-size:22px; font-weight:500; margin-top:6px; }
  /* The block sits right; the prose inside it reads left, as Anchor's does.
     Right-aligning the text itself leaves a single word stranded on the
     last line and makes every line start in a different place. */
  .anchor-aside { max-width:400px; text-align:left; }
  .anchor-copy { color:var(--muted); font-size:13.5px; font-weight:400;
                 margin:0 0 15px; line-height:1.5; }
  .anchor-cta { display:flex; gap:10px; justify-content:flex-end; align-items:center; }
  .pill {
    background:#fff; color:#0b0b15; border-radius:999px; padding:10px 20px;
    font-size:13.5px; font-weight:500; text-decoration:none; white-space:nowrap;
  }
  .pill:hover { background:#e9e4f2; }
  .pill.quiet { background:var(--surface-3); color:var(--muted);
                border:1px solid var(--stroke-2); }
  .ghost { color:var(--text); font-size:13.5px; text-decoration:none;
           padding:10px 4px; white-space:nowrap; opacity:.85; }
  .ghost:hover { opacity:1; }
  .ghost span { color:var(--muted); }

  .hero { display:grid; grid-template-columns:1.25fr 1fr 1fr; gap:14px; margin-bottom:14px; }
  .panel { background:var(--surface-1); border:1px solid var(--stroke-1);
           border-radius:var(--radius); padding:18px 20px; min-width:0; }
  .panel.keyed { background:linear-gradient(160deg,var(--surface-3),var(--surface-1) 60%);
                 border-color:var(--stroke-2); }
  .label { color:var(--muted); font-size:11px; letter-spacing:.09em;
           text-transform:uppercase; font-weight:400; }
  .figure { font-size:40px; line-height:1.1; font-weight:500; letter-spacing:-.025em;
            margin-top:10px; font-variant-numeric:tabular-nums; }
  .figure.sm { font-size:27px; }
  .delta { font-size:13px; margin-top:7px; color:var(--muted); font-weight:400; }

  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); gap:12px; }
  .card { background:var(--surface-1); border:1px solid var(--stroke-1);
          border-radius:var(--radius); padding:15px 16px; min-width:0; }
  .card .value { font-size:21px; margin-top:7px; font-weight:500;
                 font-variant-numeric:tabular-nums; }

  /* --- Section headings --------------------------------------------- */
  h2 { font-size:11px; text-transform:uppercase; letter-spacing:.11em;
       color:var(--muted); font-weight:400; margin:30px 0 11px;
       display:flex; align-items:center; gap:12px; }
  h2::after { content:""; flex:1; height:1px; background:var(--stroke-1); }

  /* --- Dense KPI table (Alpha Ledger) -------------------------------- */
  .kpis { display:grid; grid-template-columns:1fr 1fr; gap:0 26px; margin-top:14px; }
  .kpi { display:flex; justify-content:space-between; gap:12px;
         padding:7px 0; border-bottom:1px solid var(--stroke-1); font-size:13px; }
  .kpi span { color:var(--muted); font-weight:400; }
  .kpi b { font-weight:500; font-variant-numeric:tabular-nums; }

  /* --- Chart --------------------------------------------------------- */
  .chart { width:100%; height:150px; display:block; }
  .chart-empty { color:var(--muted); font-size:13px; font-weight:400; padding:34px 0;
                 text-align:center; }

  /* --- Tables -------------------------------------------------------- */
  .wrap { background:var(--surface-1); border:1px solid var(--stroke-1);
          border-radius:var(--radius); overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:13.5px; font-weight:400; }
  th { text-align:left; color:var(--muted); font-weight:400; font-size:10.5px;
       letter-spacing:.09em; text-transform:uppercase; padding:12px 14px;
       border-bottom:1px solid var(--stroke-1); background:var(--surface-2); }
  td { padding:12px 14px; border-bottom:1px solid var(--stroke-1); vertical-align:middle;
       font-variant-numeric:tabular-nums; }
  tr:last-child td { border-bottom:0; }
  .pos { color:var(--pos); } .neg { color:var(--neg); } .muted { color:var(--muted); }
  .sym { font-weight:500; }
  .venue-tag { font-size:11px; color:var(--muted); background:var(--surface-3);
               border:1px solid var(--stroke-2); border-radius:6px; padding:2px 7px; }

  button { font:inherit; font-size:13px; font-weight:500; border:0; border-radius:9px;
           padding:8px 15px; cursor:pointer; margin-right:7px; }
  .ok { background:var(--accent); color:#120520; }
  .ok:hover { background:#c07dff; }
  .no { background:var(--surface-3); color:var(--text); border:1px solid var(--stroke-2); }
  .no:hover { background:#20202f; }
  .empty { color:var(--muted); padding:22px 14px; font-weight:400; }
  td, th { white-space:nowrap; }
  .reason { max-width:460px; white-space:normal; font-weight:400; }

  /* --- Phone --------------------------------------------------------- */
  /* Approving a trade from your phone is the whole point of the Telegram
     buttons, so the same has to work here: drop the wide explanatory
     columns rather than push Approve/Deny off the right edge. */
  @media (max-width: 860px) { .hero { grid-template-columns:1fr 1fr; } }
  @media (max-width: 720px) {
    :root { --rail:0px; }
    /* The rail becomes a top strip: a 68px gutter is a lot of a 390px screen. */
    .rail {
      position:static; width:auto; height:auto; flex-direction:row;
      border-right:0; border-bottom:1px solid var(--stroke-1);
      padding:9px 12px; gap:5px; overflow-x:auto;
    }
    .rail .mark { margin:0 6px 0 0; width:30px; height:30px; }
    .rail .spacer { display:none; }
    .shell { margin-left:0; padding:16px 14px 44px; }
    .head { gap:11px; }
    h1 { font-size:21px; }
    .hero { grid-template-columns:1fr; gap:10px; }
    .figure { font-size:33px; }
    .figure.sm { font-size:24px; }

    /* The hero keeps its bottom-anchored feel but stops being a left/right
       split: at 390px the two columns would each be too narrow to read, and
       a right-aligned CTA ends up stranded in a corner. */
    .anchor { min-height:300px; }
    .anchor-marks { padding:14px 16px 0; font-size:10px; gap:10px; }
    .anchor-floor { flex-direction:column; align-items:stretch;
                    gap:16px; padding:0 16px 18px; }
    .anchor-lead .figure { font-size:42px; }
    .anchor-lead .figure.sm { font-size:18px; }
    .anchor-aside { max-width:none; text-align:left; }
    .anchor-cta { justify-content:stretch; }
    .pill { flex:1; text-align:center; padding:13px 16px; }
    .ghost { padding:13px 8px; }
    .kpis { grid-template-columns:1fr; gap:0; }
    .hide-sm { display:none; }
    .cards { grid-template-columns:repeat(auto-fit,minmax(128px,1fr)); gap:9px; }
    .card .value { font-size:18px; }
    table { font-size:12.5px; }
    td, th { padding:9px 10px; }

    /* Pending approvals become cards. As a table the action column sat
       inside a sideways-scrolling box, so reaching Approve on a phone meant
       swiping the table first — on the one interaction the whole alert mode
       exists for. Each row is now a block and the buttons are full width.
       `:not(:last-child)` keeps the "nothing yet" row visible: when the
       table is empty that single row is both the first and the last. */
    #pending tr:first-child:not(:last-child) { display:none; }
    #pending tr { display:block; padding:13px 14px; border-bottom:1px solid var(--stroke-1); }
    #pending tr:last-child { border-bottom:0; }
    #pending td { display:flex; justify-content:space-between; align-items:baseline;
                  gap:14px; padding:4px 0; border:0; white-space:normal; }
    #pending td::before {
      content:attr(data-l); color:var(--muted); font-size:10.5px;
      letter-spacing:.09em; text-transform:uppercase; flex:none;
    }
    /* The table had to drop these columns for width. A card has room
       vertically, and you want the whole setup in front of you before
       approving a trade, so they come back. */
    #pending td.hide-sm { display:flex; }
    #pending td:last-child { display:block; margin-top:11px; }
    #pending td:last-child::before { content:none; }
    #pending td.empty { display:block; }
    #pending button { display:block; width:100%; margin:0 0 7px 0; padding:13px 10px; }
  }
</style>
</head>
<body>

<nav class="rail" aria-label="Sections">
  <a href="/" class="mark" title="Back to the opening page"
     aria-label="Back to the opening page">&#9650;</a>
  <a href="#top" class="on" title="Overview" aria-label="Overview">&#9632;</a>
  <a href="#approvals" title="Awaiting approval" aria-label="Awaiting approval">&#10003;</a>
  <a href="#positions-sec" title="Positions" aria-label="Positions">&#9644;</a>
  <a href="#signals-sec" title="Signals" aria-label="Signals">&#9671;</a>
  <a href="#performance" title="Performance" aria-label="Performance">&#9650;</a>
  <span class="spacer"></span>
</nav>

<main class="shell" id="top">

<div class="head">
  <div>
    <h1>Trading desk</h1>
    <div class="sub" id="status">loading…</div>
  </div>
  <div class="chips" id="chips"></div>
</div>

<div id="banner"></div>

<!-- The three figures worth seeing from across the room. -->
<!-- The hero, after Anchor: one tall frame with everything pinned to the
     bottom and the space above left empty. The backdrop is the equity curve
     itself rather than a photograph — a tool should be textured by its own
     data, and we already record it. -->
<section class="anchor" id="hero">
  <div class="anchor-bg" id="equity-chart"></div>
  <div class="anchor-veil"></div>
  <p class="anchor-note" id="hero-note" hidden></p>

  <div class="anchor-marks">
    <span id="mark-left">&nbsp;</span>
    <span id="mark-right">&nbsp;</span>
  </div>

  <div class="anchor-floor">
    <div class="anchor-lead">
      <div class="label">Equity</div>
      <div class="figure" id="hero-equity">—</div>
      <div class="figure sm" id="hero-day">&nbsp;</div>
    </div>
    <div class="anchor-aside">
      <p class="anchor-copy" id="hero-copy">&nbsp;</p>
      <div class="anchor-cta">
        <a class="pill" id="cta-primary" href="#approvals">Review signals</a>
        <a class="ghost" href="#positions-sec">Positions <span>&rsaquo;</span></a>
      </div>
    </div>
  </div>
</section>

<!-- Starting the bot used to mean a second command in a second
     terminal. The dashboard showed an idle account and never said that
     nothing was scanning. -->
<section class="panel" id="bot">
  <div class="bot-row">
    <div class="bot-who">
      <div class="label">The bot</div>
      <div class="bot-state"><span class="dot" id="bot-dot"></span><b id="bot-state">checking…</b></div>
      <div class="bot-sub" id="bot-sub">&nbsp;</div>
    </div>
    <div class="bot-actions">
      <button class="pill" id="bot-toggle" disabled>…</button>
      <button class="ghostbtn" id="bot-scan">Scan once now</button>
    </div>
  </div>
  <div class="bot-note" id="bot-note" hidden></div>
</section>

<section class="panel">
  <div class="label">Risk limits</div>
  <div class="kpis" id="kpis"></div>
</section>

<h2 id="approvals">Awaiting your approval</h2>
<div class="wrap"><table id="pending"></table></div>

<h2 id="positions-sec">Open positions</h2>
<div class="wrap"><table id="positions"></table></div>

<h2 id="venues-heading" hidden>Venues</h2>
<div class="wrap" id="venues-wrap" hidden><table id="venues"></table></div>

<h2 id="signals-sec">Recent signals</h2>
<div class="wrap"><table id="signals"></table></div>

<h2 id="performance">Performance</h2>
<div class="cards" id="metrics"></div>

<!-- Kept so anything (and any test) looking for the old container still
     finds one; the hero above replaced what it used to hold. -->
<div class="cards" id="cards" hidden></div>

</main>

<script>
// Set from /api/health on the first refresh. A GBP spread betting account
// rendered with a $ in front of every figure is just wrong.
let CUR = "$";
const money = n => (n<0?"-":"") + CUR + Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = n => n > 0 ? "pos" : n < 0 ? "neg" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

const pct = n => (n >= 0 ? "+" : "") + n.toFixed(2) + "%";

function card(label, value, klass="") {
  return `<div class="card"><div class="label">${label}</div>
          <div class="value ${klass}">${value}</div></div>`;
}

// A limit row: the number, and how close it is to the line that stops
// trading. Reading "-0.8% of -3%" is the point — a bare -0.8% tells you
// nothing about how much rope is left.
function kpi(label, value, klass="") {
  return `<div class="kpi"><span>${label}</span><b class="${klass}">${value}</b></div>`;
}

// Equity curve as inline SVG. No chart library on purpose: this page is a
// single self-contained file served from localhost, and a CDN script would
// make it depend on the internet to draw a line.
function sparkline(points) {
  const host = document.getElementById("equity-chart");
  // The curve IS the hero backdrop. Wiping it to nothing on a fresh install
  // left the top two thirds of the frame genuinely blank, and the whole hero
  // read as an empty box — indistinguishable from a page that had failed to
  // load, which is exactly how it was reported. A single reading is not a
  // curve, but it IS a baseline, so draw that: dashed and in the neutral
  // stroke colour, because a solid green or red line across a brand new
  // account would imply a trend that has not happened yet.
  const note = document.getElementById("hero-note");
  if (points.length < 2) {
    const MID = (40 + 340 * 0.62) / 2;
    host.innerHTML = `
    <svg class="chart" viewBox="0 0 1000 340" preserveAspectRatio="none" role="img"
         aria-label="No equity history recorded yet">
      <line x1="0" y1="${MID.toFixed(1)}" x2="1000" y2="${MID.toFixed(1)}"
            stroke="var(--accent-dim)" stroke-width="1.5" stroke-dasharray="2 9"
            vector-effect="non-scaling-stroke" opacity=".85"/>
    </svg>`;
    const m = document.getElementById("mark-right");
    if (m) {
      m.textContent += points.length
        ? " · no history yet"
        : " · no equity recorded yet";
    }
    if (note) {
      note.textContent = points.length
        ? "The equity curve draws itself here once there is more than one reading."
        : "The equity curve draws itself here once the bot records its first reading.";
      note.hidden = false;
    }
    return;
  }
  if (note) note.hidden = true;
  const W = 1000, H = 340, PAD = 0;
  const ys = points.map(p => p.equity);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const flat = hi === lo;
  // A flat account divides by zero. Centre the line rather than letting it
  // sit on the floor, which reads as "equity collapsed" at a glance.
  const span = flat ? 1 : hi - lo;
  // The curve lives behind the text, so it is drawn into the UPPER part of
  // the frame: the Anchor composition keeps the floor clear for the figures.
  const TOP = 40, BOT = H * 0.62;
  const x = i => PAD + i * (W - PAD * 2) / (points.length - 1);
  const y = v => flat ? (TOP + BOT) / 2 : BOT - ((v - lo) / span) * (BOT - TOP);

  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const area = `${line}L${x(points.length - 1).toFixed(1)},${H}L${x(0).toFixed(1)},${H}Z`;
  const up = ys[ys.length - 1] >= ys[0];
  const stroke = up ? "var(--pos)" : "var(--neg)";

  host.innerHTML = `
    <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="Equity over the last ${points.length} readings">
      <defs>
        <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${stroke}" stop-opacity=".28"/>
          <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path d="${area}" fill="url(#eqfill)"/>
      <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2.2"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>
    `;
  // The range caption belongs in the micro-label, not floating over the
  // curve. sparkline() runs after the labels are written, so it appends.
  const mark = document.getElementById("mark-right");
  if (mark) {
    mark.textContent += flat
      ? ` · flat at ${money(hi)}`
      : ` · ${money(lo)} – ${money(hi)}`;
  }
}
function table(el, headers, rows) {
  const head = headers.map(h =>
    typeof h === "string" ? `<th>${h}</th>` : `<th class="${h.c}">${h.t}</th>`
  ).join("");
  el.innerHTML = rows.length
    ? `<tr>${head}</tr>` + rows.join("")
    : `<tr><td class="empty">nothing yet</td></tr>`;
}

// --- The bot's on/off control -------------------------------------- //
// Three states worth distinguishing, because they need different things
// from you: working, paused, and running-but-waiting-for-the-open. The
// old dashboard showed all three as an idle account.
function renderBot(bot, health) {
  const dot = document.getElementById("bot-dot");
  const state = document.getElementById("bot-state");
  const sub = document.getElementById("bot-sub");
  const toggle = document.getElementById("bot-toggle");
  const scan = document.getElementById("bot-scan");
  const note = document.getElementById("bot-note");

  const waiting = bot.running && bot.market_hours_only && !bot.market_open;
  dot.className = "dot " + (waiting ? "wait" : bot.running ? "on" : "off");
  state.textContent = bot.busy ? "Scanning now…"
    : waiting ? "Waiting for the market to open"
    : bot.running ? "Working" : "Paused";

  const when = bot.next_run ? new Date(bot.next_run) : null;
  const bits = [];
  if (bot.running) {
    bits.push(`every ${bot.interval_minutes} min`);
    if (when) bits.push(`next ${when.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"})}`);
  } else {
    bits.push("not scanning — nothing is being analysed");
  }
  if (bot.last_run) {
    const at = new Date(bot.last_run.at).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
    const r = bot.last_run;
    // run_cycle().summary() is a sentence, not an object. Reading
    // .scanned off it silently produced "0 scanned, 0 signal(s)" for
    // every run, which is exactly the kind of quietly-wrong readout
    // this panel exists to replace.
    bits.push(r.error
      ? `last run ${at} failed: ${r.error}`
      : `last run ${at}: ${r.summary || "done"}`);
  }
  sub.textContent = bits.join(" · ");

  toggle.disabled = false;
  toggle.textContent = bot.running ? "Pause the bot" : "Start the bot";
  toggle.className = bot.running ? "pill stop" : "pill";
  scan.disabled = !!bot.busy;

  // Say what starting it will actually do. In auto mode the next cycle
  // can place orders without asking, which is not something to discover
  // after the fact.
  let text = "";
  if (!health.llm_ready) {
    text = "No Claude API key, so there is nothing to analyse with. " +
           "Add one with ./start.sh setkey and restart.";
  } else if (health.execution_mode === "auto") {
    text = health.mode === "live"
      ? "AUTO + LIVE MONEY: approved signals are sent to the broker as real " +
        "orders, with no confirmation step."
      : "Auto mode: signals are executed on the paper account without asking you.";
  } else {
    text = "Alert mode: every signal waits for your Approve above. " +
           "Nothing reaches the broker on its own.";
  }
  note.textContent = text;
  note.className = "bot-note" +
    ((!health.llm_ready || (health.execution_mode === "auto" && health.mode === "live"))
      ? " warn" : "");
  note.hidden = false;
}

async function botPost(path) {
  try {
    const r = await fetch(path, { method: "POST" });
    const body = await r.json();
    if (!body.ok && body.message) alert(body.message);
  } catch (e) {
    alert("Could not reach the bot: " + e.message);
  }
  refresh();
}

document.addEventListener("click", e => {
  if (e.target.id === "bot-toggle") {
    e.target.disabled = true;
    botPost(e.target.textContent.startsWith("Pause") ? "/api/bot/stop" : "/api/bot/start");
  }
  if (e.target.id === "bot-scan") {
    e.target.disabled = true;
    botPost("/api/bot/scan-now");
  }
});

async function refresh() {
  try {
    const [health, pf, pend, sigs, met, eq] = await Promise.all(
      ["/api/health","/api/portfolio","/api/pending","/api/signals?limit=15",
       "/api/metrics","/api/equity"]
        .map(u => fetch(u).then(r => r.json()))
    );

    CUR = health.currency_symbol || "$";

    // The build is here so "did my pull actually take effect?" is answerable
    // from the page itself rather than by guessing at ports and processes.
    document.getElementById("status").textContent =
      `${health.symbols.length} symbols · ${health.execution_mode} mode · ` +
      `${health.model} · build ${health.build || "unknown"}`;

    const badge = health.mode === "live"
      ? '<span class="badge live">LIVE MONEY</span>'
      : '<span class="badge paper">PAPER</span>';
    document.getElementById("chips").innerHTML =
      badge +
      `<span class="chip">Venue <b>${esc(health.broker)}</b></span>` +
      `<span class="chip">Size in <b>${esc(health.size_unit || "shares")}</b></span>`;

    let banners = "";
    if (!health.llm_ready) {
      banners +=
      `<div class="banner"><b>Viewing mode — no Claude API key.</b><br>
       Everything below is real and the page works, but the bot cannot analyse
       markets or produce new signals yet. Add a key with
       <code>./start.sh setkey</code> and restart. Backtesting works without one.</div>`;
    }
    if ((pf.unreachable || []).length) {
      banners +=
      `<div class="banner"><b>Trading halted — cannot reach ${esc(pf.unreachable.join(", "))}.</b><br>
       The figures below are missing ${pf.unreachable.length > 1 ? "those accounts" : "that account"},
       so equity and open risk are understated and no new trade can be sized
       safely. Existing positions and their broker-side stops are unaffected.
       Trading resumes by itself once
       ${pf.unreachable.length > 1 ? "they answer" : "the venue answers"} again.</div>`;
    }
    if (pf.error) {
      banners +=
      `<div class="banner"><b>Cannot reach the broker (${esc(health.broker)}).</b><br>
       ${esc(pf.error)}<br>
       The figures below are zeros, not your account. Run
       <code>./start.sh doctor --check-apis</code>.</div>`;
    }
    document.getElementById("banner").innerHTML = banners;

    // --- Hero ---
    // Anchor states facts plainly and prefers a specific number to an
    // adjective, which suits a risk tool exactly. Every line here is a
    // figure, not a claim.
    document.getElementById("hero-equity").textContent = money(pf.equity);

    const day = document.getElementById("hero-day");
    day.textContent = `${pct(pf.day_pnl_pct)}  ${money(pf.day_pnl)} today`;
    day.className = "figure sm " + cls(pf.day_pnl_pct);

    document.getElementById("mark-left").textContent =
      `${health.mode === "live" ? "Live" : "Paper"} · ${health.broker}`;
    document.getElementById("mark-right").textContent =
      `${health.symbols.length} symbols · ${(pf.venues || []).length || 1} venue(s)`;

    document.getElementById("hero-copy").innerHTML =
      `${money(pf.cash)} cash, ${money(pf.exposure)} exposed
       (${pf.exposure_pct.toFixed(1)}%). ${money(pf.open_risk)} still at risk across
       ${pf.positions.length} position(s), ${pf.open_risk_pct.toFixed(2)}% of equity
       against a ${(health.max_portfolio_risk_pct ?? 0).toFixed(1)}% cap.`;

    // The primary action is whatever is actually waiting for you, and it
    // goes quiet rather than inviting a click when nothing is.
    const waiting = (pend.pending || []).length;
    const primary = document.getElementById("cta-primary");
    primary.textContent = waiting
      ? `Review ${waiting} pending signal${waiting > 1 ? "s" : ""}`
      : "No signals waiting";
    primary.className = waiting ? "pill" : "pill quiet";

    sparkline(eq.points || []);

    renderBot(await fetch("/api/bot").then(r => r.json()), health);

    // --- Risk limits: the number AND how near the line it is ---
    const dll = health.daily_loss_limit_pct ?? 0;
    const mdd = health.max_drawdown_pct ?? 0;
    const mpr = health.max_portfolio_risk_pct ?? 0;
    const mop = health.max_open_positions ?? 0;
    document.getElementById("kpis").innerHTML =
      kpi("Day P&L", `${pct(pf.day_pnl_pct)} of -${dll.toFixed(1)}%`, cls(pf.day_pnl_pct)) +
      kpi("Drawdown", `-${pf.drawdown_pct.toFixed(2)}% of -${mdd.toFixed(1)}%`,
          pf.drawdown_pct > 0 ? "neg" : "") +
      kpi("Open risk", `${pf.open_risk_pct.toFixed(2)}% of ${mpr.toFixed(1)}%`) +
      kpi("Positions", `${pf.positions.length} of ${mop}`) +
      kpi("Peak equity", money(pf.peak_equity)) +
      kpi("Buying power", money(pf.buying_power)) +
      kpi("Risk per trade", `${(health.risk_per_trade_pct ?? 0).toFixed(2)}%`) +
      kpi("Venues", (pf.venues || []).length || 1);

    table(document.getElementById("pending"),
      ["Symbol",
       {t:"Dir", c:"hide-sm"}, "Entry", "Stop",
       {t:"Target", c:"hide-sm"}, {t:"R:R", c:"hide-sm"},
       {t:"Size", c:"hide-sm"}, "Risk", ""],
      pend.pending.map(p => {
        const s = p.signal;
        // data-l drives the phone card layout, where each cell shows its own
        // label because the header row is hidden.
        return `<tr>
          <td class="sym" data-l="Symbol">${esc(s.symbol)}</td>
          <td class="hide-sm ${s.direction === "long" ? "pos" : "neg"}" data-l="Direction">${esc(s.direction)}</td>
          <td data-l="Entry">${s.entry.toFixed(2)}</td>
          <td data-l="Stop">${s.stop.toFixed(2)}</td>
          <td class="hide-sm" data-l="Target">${s.target.toFixed(2)}</td>
          <td class="hide-sm" data-l="R:R">${(s.risk_reward ?? 0).toFixed(2)}</td>
          <td class="hide-sm" data-l="Size">${p.sizing.shares} ${esc(p.sizing.unit || "shares")}</td>
          <td data-l="Risk">${money(p.sizing.dollar_risk)} (${p.sizing.account_risk_pct.toFixed(2)}%)</td>
          <td><button class="ok" onclick="act('${esc(s.id)}','approve')">Approve</button>
              <button class="no" onclick="act('${esc(s.id)}','deny')">Deny</button></td>
        </tr>`;
      }));

    const multi = (pf.venues || []).length > 1;

    table(document.getElementById("positions"),
      multi
        ? ["Symbol", {t:"Venue", c:"hide-sm"}, "Qty", "Avg entry", "Price", "Unrealised"]
        : ["Symbol","Qty","Avg entry","Price","Unrealised"],
      pf.positions.map(p => `<tr>
          <td class="sym">${esc(p.symbol)}</td>
          ${multi ? `<td class="hide-sm"><span class="venue-tag">${esc(p.venue || "")}</span></td>` : ""}
          <td>${p.qty}</td>
          <td>${p.avg_entry.toFixed(2)}</td><td>${p.market_price.toFixed(2)}</td>
          <td class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</td></tr>`));

    // Only worth showing when there is more than one. With a single broker
    // the venue table just repeats the figures above it — and hiding only
    // the heading left an empty "nothing yet" box sitting under Positions.
    document.getElementById("venues-heading").hidden = !multi;
    document.getElementById("venues-wrap").hidden = !multi;
    table(document.getElementById("venues"),
      multi ? ["Venue","Equity","Cash",{t:"Exposure", c:"hide-sm"},"Positions","Status"] : [],
      multi ? (pf.venues || []).map(v => `<tr>
          <td>${esc(v.name)}</td>
          <td>${money(v.equity)}</td><td>${money(v.cash)}</td>
          <td class="hide-sm">${money(v.exposure)}</td>
          <td>${v.positions}</td>
          <td class="${v.error ? "neg" : ""}">${v.error ? esc(v.error.slice(0,80)) : "ok"}</td>
        </tr>`) : []);

    table(document.getElementById("signals"),
      [{t:"Time", c:"hide-sm"}, "Symbol", {t:"Dir", c:"hide-sm"},
       "Entry", "Stop", {t:"R:R", c:"hide-sm"}, "Status",
       {t:"Note", c:"hide-sm"}],
      sigs.signals.map(s => `<tr>
          <td class="muted hide-sm">${esc((s.created_at||"").slice(5,16).replace("T"," "))}</td>
          <td>${esc(s.symbol)}</td><td class="hide-sm">${esc(s.direction)}</td>
          <td>${(s.entry||0).toFixed(2)}</td><td>${(s.stop||0).toFixed(2)}</td>
          <td class="hide-sm">${(s.risk_reward||0).toFixed(2)}</td>
          <td>${esc(s.status)}</td>
          <td class="reason muted hide-sm">${esc(s.risk_approved ? (s.reasoning||"").slice(0,160) : s.risk_reason)}</td>
        </tr>`));

    document.getElementById("metrics").innerHTML =
      card("Trades", met.total_trades + (met.is_significant ? "" : " (thin)")) +
      card("Win rate", met.win_rate.toFixed(1)+"%") +
      card("Avg R", (met.avg_r>=0?"+":"")+met.avg_r.toFixed(2)) +
      card("Expectancy", (met.expectancy_r>=0?"+":"")+met.expectancy_r.toFixed(2)+"R",
           cls(met.expectancy_r)) +
      card("Profit factor", met.profit_factor.toFixed(2)) +
      card("Max DD", met.max_drawdown_pct.toFixed(1)+"%") +
      card("Total P&L", money(met.total_pnl), cls(met.total_pnl));
  } catch (e) {
    document.getElementById("status").textContent = "error talking to the bot: " + e;
  }
}

async function act(id, what) {
  const r = await fetch(`/api/signals/${id}/${what}`, {method:"POST"});
  const j = await r.json();
  alert(j.message);
  refresh();
}

refresh();
setInterval(refresh, 20000);
</script>
</body>
</html>
"""


def run_server() -> None:  # pragma: no cover - entry point
    import uvicorn

    from src.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "src.webhook_server:app",
        host=settings.webhook_host,
        port=settings.webhook_port,
        log_level=settings.log_level.lower(),
    )
