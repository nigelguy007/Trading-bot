"""
The wiring: build_app, the HTTP API and the scheduler's gate.

These existed nowhere before, which is why a 100%-drawdown bug reached a
screenshot rather than a test. They are smoke tests on purpose — every
endpoint answers, with the right shape, against the simulator — but that is
enough to catch a broken wire-up, a renamed field the dashboard reads, or an
endpoint that 500s because a broker is unreachable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import build_app
from src.config import Settings


def settings_for(tmp_path, **kw) -> Settings:
    kw.setdefault("anthropic_api_key", "")
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("log_dir", str(tmp_path / "logs"))
    kw.setdefault("tradingview_webhook_secret", "s3cret")
    return Settings(_env_file=None, **kw)


@pytest.fixture
def ctx(tmp_path):
    context = build_app(settings_for(tmp_path), force_simulated=True, require_llm=False)
    yield context
    context.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import src.webhook_server as ws

    context = build_app(settings_for(tmp_path), force_simulated=True, require_llm=False)
    monkeypatch.setattr(ws, "_ctx", context, raising=False)
    monkeypatch.setattr(ws, "get_context", lambda: context)
    with TestClient(ws.app) as c:
        yield c
    context.close()


# ---------------------------------------------------------------------- #
# Wiring
# ---------------------------------------------------------------------- #
def test_build_app_without_an_llm_key_still_wires_everything(ctx):
    """The dashboard has to work before you have set up API billing."""
    assert ctx.broker.name == "simulated"
    assert ctx.llm.enabled is False
    for part in (ctx.journal, ctx.portfolio, ctx.risk, ctx.execution, ctx.pipeline):
        assert part is not None


def test_the_portfolio_reads_through_the_router_not_one_broker(ctx):
    """
    The whole point of the combined-book work: hand the portfolio the router
    so it sums every venue. If this regresses to a single broker, two
    accounts silently become two sets of limits again.
    """
    assert ctx.portfolio.brokers == ctx.brokers.all()


def test_the_risk_manager_can_resolve_a_venue(ctx):
    from src.models import Signal

    signal = Signal(
        symbol="AAPL", direction="long", entry=100, stop=98, target=106,
        confidence=0.9, reasoning="t",
    )
    assert ctx.risk.venue_for(signal) == "simulated"


def test_reconcile_is_safe_when_no_broker_implements_it(ctx):
    """The simulator has no reconcile(); the call must be a no-op, not a crash."""
    assert ctx.execution.reconcile() == {}


# ---------------------------------------------------------------------- #
# The HTTP API the dashboard reads
# ---------------------------------------------------------------------- #
def test_health_reports_the_fields_the_dashboard_uses(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
    assert body["llm_ready"] is False
    # The dashboard formats every figure with these two.
    assert body["currency_symbol"] == "$"
    assert body["size_unit"] == "shares"
    assert "simulated" in body["broker"]


def test_portfolio_returns_every_key_the_dashboard_renders(client):
    body = client.get("/api/portfolio").json()
    for key in (
        "equity", "cash", "day_pnl_pct", "drawdown_pct", "exposure",
        "exposure_pct", "open_risk", "open_risk_pct", "positions",
        "venues", "unreachable",
    ):
        assert key in body, f"/api/portfolio is missing {key}"
    assert body["unreachable"] == []
    assert [v["name"] for v in body["venues"]] == ["simulated"]


def test_a_healthy_portfolio_reports_no_drawdown(client):
    """The bug that reached a screenshot: 100% drawdown on a fresh account."""
    assert client.get("/api/portfolio").json()["drawdown_pct"] == 0.0


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/portfolio", "/api/pending", "/api/metrics", "/api/signals"]
)
def test_every_endpoint_answers(client, path):
    assert client.get(path).status_code == 200


def test_the_dashboard_page_serves(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # The elements refresh() writes into must exist, or the page silently
    # renders nothing.
    for element in ("banner", "cards", "pending", "positions", "venues", "metrics"):
        assert f'id="{element}"' in resp.text


def test_the_webhook_rejects_a_wrong_secret(client):
    resp = client.post(
        "/webhook/tradingview",
        json={"secret": "wrong", "symbol": "AAPL", "action": "buy"},
    )
    assert resp.status_code in (401, 403)


def test_the_webhook_rejects_a_missing_secret(client):
    resp = client.post("/webhook/tradingview", json={"symbol": "AAPL", "action": "buy"})
    assert resp.status_code in (400, 401, 403, 422)


# ---------------------------------------------------------------------- #
# Scheduler gate
# ---------------------------------------------------------------------- #
def test_market_hours_can_be_turned_off(tmp_path):
    from src.scheduler import market_is_open

    context = build_app(
        settings_for(tmp_path, market_hours_only=False),
        force_simulated=True,
        require_llm=False,
    )
    assert market_is_open(context) is True
    context.close()


def test_a_broker_that_raises_falls_back_to_the_clock(tmp_path):
    """A broker outage must not permanently convince the scheduler it is a
    weekend."""
    from src.scheduler import market_is_open

    context = build_app(settings_for(tmp_path), force_simulated=True, require_llm=False)

    def boom() -> bool:
        raise RuntimeError("broker down")

    context.broker.is_market_open = boom  # type: ignore[method-assign]
    assert market_is_open(context) in (True, False)  # falls back, does not raise
    context.close()


# ---------------------------------------------------------------------- #
# The redesigned dashboard
# ---------------------------------------------------------------------- #
def test_health_carries_the_limits_the_dashboard_shows_figures_against(client):
    """
    "-0.8%" means nothing on its own. The panel renders "-0.8% of -3%", so
    the limits have to come down with the rest of the health payload.
    """
    body = client.get("/api/health").json()
    for key in (
        "risk_per_trade_pct", "daily_loss_limit_pct", "max_drawdown_pct",
        "max_portfolio_risk_pct", "max_open_positions",
    ):
        assert key in body, f"/api/health is missing {key}"
    assert body["daily_loss_limit_pct"] == 3.0


def test_the_equity_endpoint_serves_the_curve(client):
    body = client.get("/api/equity").json()
    assert "points" in body
    assert isinstance(body["points"], list)


def test_the_equity_endpoint_shapes_each_point_for_the_chart(client, tmp_path):
    client.get("/api/portfolio")  # records one reading
    points = client.get("/api/equity").json()["points"]
    assert points, "a portfolio read should have recorded an equity point"
    for key in ("ts", "equity", "exposure"):
        assert key in points[0]


def test_the_dashboard_has_the_elements_the_redesign_writes_into(client):
    html = client.get("/").text
    for element in (
        "hero-equity", "hero-day", "hero-copy", "equity-chart", "kpis",
        "chips", "venues-wrap", "mark-left", "mark-right", "cta-primary",
    ):
        assert f'id="{element}"' in html, f"the page is missing #{element}"


def test_the_hero_is_bottom_anchored(client):
    """
    The Anchor composition: a tall frame with the top left empty and every
    element pinned to the floor. `margin-top:auto` on the floor row is what
    does it — without that the block floats and the whole point is lost.
    """
    html = client.get("/").text
    assert ".anchor-floor {" in html
    assert "margin-top:auto" in html
    assert "min-height:340px" in html


def test_the_equity_curve_is_the_hero_backdrop(client):
    """
    Not a boxed chart any more. It sits behind the figures, under a veil
    that darkens toward the floor so the numbers read without a scrim.
    """
    html = client.get("/").text
    assert 'class="anchor-bg" id="equity-chart"' in html
    assert "anchor-veil" in html


def test_the_primary_action_goes_quiet_when_nothing_is_waiting(client):
    """A CTA that invites a click when there is nothing to click is noise."""
    html = client.get("/").text
    assert "No signals waiting" in html
    assert "pill quiet" in html


def test_the_page_is_self_contained(client):
    """
    No external script or stylesheet. This runs on localhost and must draw
    its own charts; a CDN would make the page need the internet.
    """
    html = client.get("/").text
    assert "<script src=" not in html
    assert "cdn." not in html
    assert "<link rel=\"stylesheet\"" not in html


def test_the_venue_table_is_hidden_until_there_are_two(client):
    """Hiding only the heading used to leave an empty box under Positions."""
    html = client.get("/").text
    assert 'id="venues-wrap" hidden' in html
    # And an author display rule must not override the hidden attribute.
    assert "[hidden] { display:none !important; }" in html


def test_approve_and_deny_survive_on_a_phone(client):
    """
    Approving from a phone is the entire point of alert mode. The pending
    rows become cards under the phone breakpoint so the buttons are not
    stranded inside a sideways-scrolling table.
    """
    html = client.get("/").text
    assert "#pending tr { display:block;" in html
    assert "#pending button { display:block; width:100%;" in html
    # The "nothing yet" row must not be hidden along with the header row.
    assert "#pending tr:first-child:not(:last-child) { display:none; }" in html
