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
    _ctx = build_app()
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
        "broker": ctx.broker.name,
        "symbols": ctx.settings.all_symbols,
    }


@app.get("/api/portfolio")
async def portfolio() -> dict:
    ctx = get_context()
    state = ctx.portfolio.snapshot()
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
        "positions": [
            {
                "symbol": p.symbol,
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
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0d1117; color:#e6edf3;
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; padding:24px; }
  h1 { font-size:18px; margin:0 0 4px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
       color:#8b949e; margin:28px 0 10px; }
  .sub { color:#8b949e; font-size:12px; margin-bottom:24px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; }
  .paper { background:#1f6feb33; color:#79c0ff; }
  .live  { background:#da363333; color:#ff7b72; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; }
  .card .label { color:#8b949e; font-size:11px; text-transform:uppercase; }
  .card .value { font-size:20px; margin-top:6px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:#8b949e; font-weight:normal; font-size:11px;
       text-transform:uppercase; padding:6px 8px; border-bottom:1px solid #30363d; }
  td { padding:8px; border-bottom:1px solid #21262d; vertical-align:top; }
  .pos { color:#3fb950; } .neg { color:#f85149; } .muted { color:#8b949e; }
  button { font:inherit; border:0; border-radius:6px; padding:5px 12px;
           cursor:pointer; margin-right:6px; }
  .ok { background:#238636; color:#fff; } .no { background:#30363d; color:#e6edf3; }
  .wrap { overflow-x:auto; }
  .empty { color:#8b949e; padding:12px 0; }
  .reason { max-width:520px; white-space:pre-wrap; }
</style>
</head>
<body>
<h1>AI Trading Bot</h1>
<div class="sub" id="status">loading…</div>

<div class="cards" id="cards"></div>

<h2>Awaiting your approval</h2>
<div class="wrap"><table id="pending"></table></div>

<h2>Open positions</h2>
<div class="wrap"><table id="positions"></table></div>

<h2>Recent signals</h2>
<div class="wrap"><table id="signals"></table></div>

<h2>Performance</h2>
<div class="cards" id="metrics"></div>

<script>
const money = n => (n<0?"-":"") + "$" + Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = n => n > 0 ? "pos" : n < 0 ? "neg" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function card(label, value, klass="") {
  return `<div class="card"><div class="label">${label}</div>
          <div class="value ${klass}">${value}</div></div>`;
}
function table(el, headers, rows) {
  el.innerHTML = rows.length
    ? `<tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr>` + rows.join("")
    : `<tr><td class="empty">nothing yet</td></tr>`;
}

async function refresh() {
  try {
    const [health, pf, pend, sigs, met] = await Promise.all(
      ["/api/health","/api/portfolio","/api/pending","/api/signals?limit=15","/api/metrics"]
        .map(u => fetch(u).then(r => r.json()))
    );

    const badge = health.mode === "live"
      ? '<span class="badge live">LIVE MONEY</span>'
      : '<span class="badge paper">PAPER</span>';
    document.getElementById("status").innerHTML =
      `${badge} &nbsp; ${esc(health.execution_mode)} mode &nbsp;·&nbsp; ${esc(health.model)}
       &nbsp;·&nbsp; ${esc(health.broker)} &nbsp;·&nbsp; ${health.symbols.length} symbols`;

    document.getElementById("cards").innerHTML =
      card("Equity", money(pf.equity)) +
      card("Day P&L", pf.day_pnl_pct.toFixed(2)+"%", cls(pf.day_pnl_pct)) +
      card("Drawdown", "-"+pf.drawdown_pct.toFixed(2)+"%", pf.drawdown_pct>0?"neg":"") +
      card("Exposure", money(pf.exposure)+" ("+pf.exposure_pct.toFixed(1)+"%)") +
      card("Open risk", money(pf.open_risk)+" ("+pf.open_risk_pct.toFixed(2)+"%)") +
      card("Positions", pf.positions.length);

    table(document.getElementById("pending"),
      ["Symbol","Dir","Entry","Stop","Target","R:R","Size","Risk",""],
      pend.pending.map(p => {
        const s = p.signal;
        return `<tr>
          <td>${esc(s.symbol)}</td><td>${esc(s.direction)}</td>
          <td>${s.entry.toFixed(2)}</td><td>${s.stop.toFixed(2)}</td>
          <td>${s.target.toFixed(2)}</td><td>${(s.risk_reward ?? 0).toFixed(2)}</td>
          <td>${p.sizing.shares}</td>
          <td>${money(p.sizing.dollar_risk)} (${p.sizing.account_risk_pct.toFixed(2)}%)</td>
          <td><button class="ok" onclick="act('${esc(s.id)}','approve')">Approve</button>
              <button class="no" onclick="act('${esc(s.id)}','deny')">Deny</button></td>
        </tr>`;
      }));

    table(document.getElementById("positions"),
      ["Symbol","Qty","Avg entry","Price","Unrealised"],
      pf.positions.map(p => `<tr>
          <td>${esc(p.symbol)}</td><td>${p.qty}</td>
          <td>${p.avg_entry.toFixed(2)}</td><td>${p.market_price.toFixed(2)}</td>
          <td class="${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl)}</td></tr>`));

    table(document.getElementById("signals"),
      ["Time","Symbol","Dir","Entry","Stop","R:R","Status","Note"],
      sigs.signals.map(s => `<tr>
          <td class="muted">${esc((s.created_at||"").slice(5,16).replace("T"," "))}</td>
          <td>${esc(s.symbol)}</td><td>${esc(s.direction)}</td>
          <td>${(s.entry||0).toFixed(2)}</td><td>${(s.stop||0).toFixed(2)}</td>
          <td>${(s.risk_reward||0).toFixed(2)}</td>
          <td>${esc(s.status)}</td>
          <td class="reason muted">${esc(s.risk_approved ? (s.reasoning||"").slice(0,160) : s.risk_reason)}</td>
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
