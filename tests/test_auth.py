"""
Who can reach the dashboard.

These matter more than most tests here. Every endpoint behind this
middleware can move money or reveal the account: approve sends an order
to the broker, /api/bot/start switches the bot on, /api/portfolio reads
out positions and equity. Before this, all of it was open — harmless on
localhost, a gift to the internet on a public URL.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from src.app import build_app
from src.auth import is_loopback
from src.config import Settings

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 51000)   # TEST-NET-3, never a real client


def settings_for(tmp_path, **kw) -> Settings:
    kw.setdefault("anthropic_api_key", "")
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("log_dir", str(tmp_path / "logs"))
    kw.setdefault("tradingview_webhook_secret", "s3cret")
    return Settings(_env_file=None, **kw)


def make_client(tmp_path, monkeypatch, *, client_addr, **setting_kw) -> TestClient:
    import src.webhook_server as ws

    settings = settings_for(tmp_path, **setting_kw)
    context = build_app(settings, force_simulated=True, require_llm=False)
    monkeypatch.setattr(ws, "_ctx", context, raising=False)
    monkeypatch.setattr(ws, "get_context", lambda: context)
    # The middleware was constructed at import with the ambient settings,
    # so point it at this test's settings.
    for mw in ws.app.user_middleware:
        if getattr(mw.cls, "__name__", "") == "DashboardAuth":
            monkeypatch.setitem(mw.kwargs, "settings", settings)
    ws.app.middleware_stack = ws.app.build_middleware_stack()
    return TestClient(ws.app, client=client_addr)


def basic(user: str, password: str) -> dict:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


# ---------------------------------------------------------------------- #
# No password set: local only
# ---------------------------------------------------------------------- #
def test_a_local_browser_needs_no_password(tmp_path, monkeypatch):
    """A laptop checkout must keep working with no setup at all."""
    with make_client(tmp_path, monkeypatch, client_addr=LOCAL) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/portfolio").status_code == 200


def test_a_remote_client_is_refused_when_no_password_is_set(tmp_path, monkeypatch):
    """
    The one that matters. A deploy that forgets DASHBOARD_PASSWORD must
    refuse everyone rather than hand the account to the internet.
    """
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE) as c:
        for path in ("/", "/desk", "/api/portfolio", "/api/bot"):
            assert c.get(path).status_code == 403, path
        assert "DASHBOARD_PASSWORD" in c.get("/").json()["error"]


@pytest.mark.parametrize("path", [
    "/api/bot/start",
    "/api/bot/stop",
    "/api/bot/scan-now",
    "/api/scan",
    "/api/signals/abc/approve",
    "/api/signals/abc/deny",
])
def test_no_write_endpoint_is_reachable_remotely_without_a_password(
    path, tmp_path, monkeypatch
):
    """Approve places a real order. None of these may be open."""
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE) as c:
        assert c.post(path).status_code == 403, path


# ---------------------------------------------------------------------- #
# Password set: Basic auth from anywhere
# ---------------------------------------------------------------------- #
def test_a_remote_client_with_the_password_gets_in(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        assert c.get("/", headers=basic("trader", "hunter2")).status_code == 200


def test_a_remote_client_without_credentials_is_challenged(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        resp = c.get("/")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Basic")


@pytest.mark.parametrize("user,password", [
    ("trader", "wrong"),
    ("wrong", "hunter2"),
    ("", ""),
    ("trader", "hunter2 "),      # trailing space must not pass
    ("trader", "HUNTER2"),       # case matters
])
def test_wrong_credentials_are_refused(user, password, tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        assert c.get("/", headers=basic(user, password)).status_code == 401


def test_a_password_protects_the_local_browser_too(tmp_path, monkeypatch):
    """
    Once a password is set it applies everywhere. Exempting loopback
    would leave anything that can make a request from the host — another
    container, a stray script — unauthenticated.
    """
    with make_client(tmp_path, monkeypatch, client_addr=LOCAL,
                     dashboard_password="hunter2") as c:
        assert c.get("/").status_code == 401
        assert c.get("/", headers=basic("trader", "hunter2")).status_code == 200


def test_the_username_can_be_changed(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_user="nigel", dashboard_password="hunter2") as c:
        assert c.get("/", headers=basic("nigel", "hunter2")).status_code == 200
        assert c.get("/", headers=basic("trader", "hunter2")).status_code == 401


def test_a_malformed_header_does_not_crash_the_server(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        for header in ("Basic !!!not-base64!!!", "Basic", "Bearer abc", "", "Basic " + "A" * 9):
            resp = c.get("/", headers={"Authorization": header})
            assert resp.status_code == 401, header


# ---------------------------------------------------------------------- #
# The exemptions
# ---------------------------------------------------------------------- #
def test_liveness_needs_no_credentials_and_leaks_nothing(tmp_path, monkeypatch):
    """A platform health check cannot send a password."""
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        resp = c.get("/api/live")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_the_tradingview_webhook_still_works_behind_its_own_secret(tmp_path, monkeypatch):
    """
    TradingView cannot do Basic auth. The endpoint carries its own shared
    secret, and a wrong one must still be rejected.
    """
    with make_client(tmp_path, monkeypatch, client_addr=REMOTE,
                     dashboard_password="hunter2") as c:
        bad = c.post("/webhook/tradingview",
                     json={"secret": "nope", "symbols": ["AAPL"]})
        assert bad.status_code in (401, 403)
        assert "www-authenticate" not in {k.lower() for k in bad.headers}


# ---------------------------------------------------------------------- #
# is_loopback
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("127.1.2.3", True),
    ("::1", True),
    ("0.0.0.0", False),
    ("10.0.0.5", False),
    ("192.168.1.10", False),      # the LAN is not this machine
    ("203.0.113.9", False),
    ("testclient", False),        # a name we cannot parse is not provably local
    ("", False),
    (None, False),
])
def test_only_this_machine_counts_as_local(host, expected):
    assert is_loopback(host) is expected


# ---------------------------------------------------------------------- #
# Deployment configuration
# ---------------------------------------------------------------------- #
# A trading bot has two deployment properties that are easy to get wrong
# and silent when they are: a process that sleeps stops trading, and an
# ephemeral filesystem wipes the journal the day-loss breaker measures
# against.
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_secrets_never_enter_the_image():
    ignore = (REPO / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in ignore
    lines = [l.strip() for l in ignore.splitlines()]
    assert ".env" in lines, "a stray .env must not be copied into a layer"


def test_the_fly_machine_is_not_allowed_to_sleep():
    """
    The scan loop is an in-process scheduler. A machine that stops when
    no HTTP request arrives stops trading, silently.
    """
    fly = (REPO / "fly.toml").read_text(encoding="utf-8")
    assert "auto_stop_machines = false" in fly
    assert "min_machines_running = 1" in fly


def test_the_journal_is_on_a_mounted_volume():
    """Otherwise every deploy restarts the journal from empty."""
    fly = (REPO / "fly.toml").read_text(encoding="utf-8")
    assert "[mounts]" in fly
    assert 'destination = "/data"' in fly
    assert 'DATA_DIR = "/data"' in fly

    compose = (REPO / "docker-compose.trader.yml").read_text(encoding="utf-8")
    assert "trader_data:/data" in compose
    assert "DATA_DIR: /data" in compose


def test_the_container_does_not_run_as_root():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "USER trader" in dockerfile


def test_the_healthcheck_uses_the_unauthenticated_endpoint():
    """A health check cannot send a password."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "/api/live" in dockerfile
    assert "/api/live" in (REPO / "fly.toml").read_text(encoding="utf-8")


def test_compose_does_not_publish_basic_auth_to_the_world():
    """
    Basic auth over plain HTTP sends the password in base64 on every
    request. Bind to loopback and let a reverse proxy terminate TLS.
    """
    compose = (REPO / "docker-compose.trader.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8080:8080"' in compose


def test_an_absolute_data_dir_is_honoured(tmp_path):
    """
    A relative DATA_DIR stays under the repo, which a laptop wants. A
    volume mount is absolute, and resolving it under the repo would put
    the journal back on the ephemeral filesystem.
    """
    from src.config import _resolve_dir

    target = tmp_path / "vol" / "data"
    assert _resolve_dir(str(target)) == target
    assert _resolve_dir("data") == REPO / "data"


def test_the_deploy_script_runs_on_macos_bash():
    """Every Mac ships bash 3.2, and this script is run on the user's Mac."""
    import subprocess
    script = REPO / "scripts" / "deploy-fly.sh"
    assert script.exists()
    out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    body = script.read_text(encoding="utf-8")
    for pattern in ("${VOLUME^^}", "declare -A", "mapfile", "readarray"):
        assert pattern not in body, f"bash 4+ only: {pattern}"


def test_the_deploy_script_never_prints_a_secret():
    """
    It reads .env and pushes it to Fly. The one value it may print is a
    dashboard password it generated itself, which the user has no other
    way to learn.
    """
    body = (REPO / "scripts" / "deploy-fly.sh").read_text(encoding="utf-8")
    assert 'cat "$STAGED"' not in body
    assert "flyctl secrets list" not in body
    # The staged file must be locked down and removed even on failure.
    assert 'chmod 600 "$STAGED"' in body
    assert "trap cleanup EXIT" in body


def test_the_deploy_script_keeps_it_to_one_machine():
    """Two schedulers means double scanning and duplicate orders."""
    body = (REPO / "scripts" / "deploy-fly.sh").read_text(encoding="utf-8")
    assert "--ha=false" in body


def test_the_deploy_script_does_not_ship_deployment_keys_as_secrets():
    """fly.toml sets these; a stale .env copy would silently override it."""
    body = (REPO / "scripts" / "deploy-fly.sh").read_text(encoding="utf-8")
    assert "DATA_DIR|LOG_DIR|WEBHOOK_HOST|WEBHOOK_PORT|AUTOSTART_SCANNER" in body


def test_the_host_platform_chooses_the_port():
    """
    Railway and Render pass the port as $PORT. A container listening
    anywhere else is never routed to, which looks like a broken build
    rather than a misread setting.
    """
    src = (REPO / "src" / "webhook_server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PORT"' in src
    assert "injected.isdigit()" in src


def test_the_dockerfile_is_where_build_platforms_look():
    """
    Railway, Render and most PaaS builders only look for a file literally
    named `Dockerfile`. It was Dockerfile.trader, so Railway fell back to
    auto-detection, found a Python project with no entry point, and
    failed the build before it ever read a setting.
    """
    assert (REPO / "Dockerfile").exists()
    assert not (REPO / "Dockerfile.trader").exists(), "two names would drift apart"


def test_the_container_healthcheck_follows_the_real_port():
    """
    The platform picks the port via $PORT and the app binds it. A
    healthcheck pinned to WEBHOOK_PORT would call a container unhealthy
    while it was serving perfectly.
    """
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "${PORT:-$WEBHOOK_PORT}" in dockerfile
