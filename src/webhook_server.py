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
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.app import AppContext, build_app
from src.broker.base import BrokerError
from src.logging_setup import get_logger
from src.metrics import compute_metrics

log = get_logger(__name__)

_ctx: Optional[AppContext] = None


def get_context() -> AppContext:
    if _ctx is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("application context is not initialised")
    return _ctx


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ctx
    # The dashboard is useful without a key — it shows the portfolio, the
    # journal and past signals. Only live analysis needs Claude.
    _ctx = build_app(require_llm=False)
    log.info("server.started", symbols=len(_ctx.settings.all_symbols))
    try:
        yield
    finally:
        _ctx.close()
        _ctx = None


app = FastAPI(title="AI Trading Bot", version="1.0.0", lifespan=lifespan)


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
#  Dashboard
# ====================================================================== #
@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


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
    --accent:    #af60ff;
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
      radial-gradient(60% 100% at 22% 0%, #af60ff1f 0%, transparent 70%),
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
    background:linear-gradient(145deg,var(--accent),#6d3bb0);
    display:grid; place-items:center; font-size:15px; color:#fff;
  }
  .rail a {
    width:40px; height:40px; border-radius:11px; display:grid; place-items:center;
    color:var(--muted); text-decoration:none; font-size:16px;
    border:1px solid transparent; transition:background .15s, color .15s;
  }
  .rail a:hover, .rail a.on { background:var(--surface-3); color:var(--text);
                              border-color:var(--stroke-2); }
  .rail .spacer { flex:1; }

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
  .paper { background:#af60ff1f; color:var(--accent); border:1px solid #af60ff44; }
  .live  { background:#ff6b6b1f; color:var(--neg); border:1px solid #ff6b6b55; }

  .banner { background:#e3b3410f; border:1px solid #e3b34155; color:var(--warn);
            border-radius:var(--radius); padding:14px 16px; margin-bottom:14px;
            font-size:13.5px; font-weight:400; }
  .banner b { font-weight:500; }
  .banner code { background:#00000066; padding:2px 6px; border-radius:5px;
                 font-family:ui-monospace,Menlo,monospace; font-size:12.5px; }

  /* --- Hero figures (Alpha Ledger's big light numerals) -------------- */
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
  <div class="mark">&#9650;</div>
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
<section class="hero">
  <div class="panel keyed">
    <div class="label">Equity</div>
    <div class="figure" id="hero-equity">—</div>
    <div class="delta" id="hero-delta">&nbsp;</div>
  </div>
  <div class="panel">
    <div class="label">Day P&amp;L</div>
    <div class="figure sm" id="hero-day">—</div>
    <div class="delta" id="hero-day-sub">&nbsp;</div>
  </div>
  <div class="panel">
    <div class="label">Open risk</div>
    <div class="figure sm" id="hero-risk">—</div>
    <div class="delta" id="hero-risk-sub">&nbsp;</div>
  </div>
</section>

<section class="panel" style="margin-bottom:14px">
  <div class="label">Equity curve</div>
  <div id="equity-chart"></div>
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
  if (points.length < 2) {
    host.innerHTML = `<div class="chart-empty">
      Not enough history yet — the curve appears once the bot has run a few cycles.</div>`;
    return;
  }
  const W = 1000, H = 150, PAD = 6;
  const ys = points.map(p => p.equity);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const flat = hi === lo;
  // A flat account divides by zero. Centre the line rather than letting it
  // sit on the floor, which reads as "equity collapsed" at a glance.
  const span = flat ? 1 : hi - lo;
  const x = i => PAD + i * (W - PAD * 2) / (points.length - 1);
  const y = v => flat ? H / 2 : H - PAD - ((v - lo) / span) * (H - PAD * 2);

  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const area = `${line}L${x(points.length - 1).toFixed(1)},${H}L${x(0).toFixed(1)},${H}Z`;
  const up = ys[ys.length - 1] >= ys[0];
  const stroke = up ? "var(--pos)" : "var(--neg)";

  host.innerHTML = `
    <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="Equity over the last ${points.length} readings">
      <defs>
        <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${stroke}" stop-opacity=".22"/>
          <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path d="${area}" fill="url(#eqfill)"/>
      <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>
    <div class="delta">${points.length} readings &nbsp;·&nbsp; ${
      flat ? "flat at " + money(hi)
           : "low " + money(lo) + " &nbsp;·&nbsp; high " + money(hi)}</div>`;
}
function table(el, headers, rows) {
  const head = headers.map(h =>
    typeof h === "string" ? `<th>${h}</th>` : `<th class="${h.c}">${h.t}</th>`
  ).join("");
  el.innerHTML = rows.length
    ? `<tr>${head}</tr>` + rows.join("")
    : `<tr><td class="empty">nothing yet</td></tr>`;
}

async function refresh() {
  try {
    const [health, pf, pend, sigs, met, eq] = await Promise.all(
      ["/api/health","/api/portfolio","/api/pending","/api/signals?limit=15",
       "/api/metrics","/api/equity"]
        .map(u => fetch(u).then(r => r.json()))
    );

    CUR = health.currency_symbol || "$";

    document.getElementById("status").textContent =
      `${health.symbols.length} symbols · ${health.execution_mode} mode · ${health.model}`;

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

    // --- Hero figures ---
    document.getElementById("hero-equity").textContent = money(pf.equity);
    document.getElementById("hero-delta").innerHTML =
      `${money(pf.cash)} cash &nbsp;·&nbsp; ${money(pf.exposure)} exposed
       (${pf.exposure_pct.toFixed(1)}%)`;

    const day = document.getElementById("hero-day");
    day.textContent = pct(pf.day_pnl_pct);
    day.className = "figure sm " + cls(pf.day_pnl_pct);
    document.getElementById("hero-day-sub").textContent =
      `${money(pf.day_pnl)} today · limit ${(-(health.daily_loss_limit_pct ?? 0)).toFixed(1)}%`;

    document.getElementById("hero-risk").textContent = money(pf.open_risk);
    document.getElementById("hero-risk-sub").textContent =
      `${pf.open_risk_pct.toFixed(2)}% of equity across ${pf.positions.length} position(s)`;

    sparkline(eq.points || []);

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
