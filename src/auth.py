"""
Who is allowed to reach the dashboard.

On a laptop this never mattered: the server listens on localhost and the
only client is you. Put the same server on a public URL and every one of
those endpoints is open to whoever finds it — `/api/signals/{id}/approve`
sends an order to your broker, `/api/bot/start` switches the bot on, and
`/api/portfolio` reads out your positions and equity. None of it asked
for a password.

The rule here is one that cannot be got wrong by forgetting to set
something:

* No DASHBOARD_PASSWORD set — only loopback clients are served. A local
  checkout behaves exactly as before, and a cloud deploy that forgot the
  password refuses everyone instead of exposing the account.
* DASHBOARD_PASSWORD set — HTTP Basic on everything, from anywhere.

The webhooks are exempt because they are machine endpoints that carry
their own shared secret and are called by TradingView and Telegram, which
cannot do Basic auth. `/api/live` is exempt so a platform health check
does not need credentials; it reveals nothing but the fact that a process
is up.
"""
from __future__ import annotations

import hmac
import ipaddress
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.logging_setup import get_logger

log = get_logger(__name__)

# Paths that authenticate themselves, or reveal nothing.
EXEMPT_PREFIXES = ("/webhook/",)
EXEMPT_PATHS = ("/api/live",)

_UNAUTHORISED = {"WWW-Authenticate": 'Basic realm="Trading desk", charset="UTF-8"'}


def is_loopback(host: Optional[str]) -> bool:
    """True only for a client on this machine."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A unix socket or a name we cannot parse is not provably local.
        return False


def _credentials(header: Optional[str]) -> Optional[tuple[str, str]]:
    if not header or not header.lower().startswith("basic "):
        return None
    import base64
    import binascii

    try:
        raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    user, _, password = raw.partition(":")
    return user, password


class DashboardAuth(BaseHTTPMiddleware):
    def __init__(self, app, settings) -> None:
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        password = (self.settings.dashboard_password or "").strip()
        client = request.client.host if request.client else None

        if not password:
            # Unconfigured: local only. Failing closed is the whole point
            # — a deploy that forgets the password must not serve the
            # account to the internet.
            if is_loopback(client):
                return await call_next(request)
            log.warning("auth.refused_remote_without_password", client=client)
            return JSONResponse(
                {
                    "error": "This dashboard is not reachable from outside this "
                    "machine until DASHBOARD_PASSWORD is set.",
                },
                status_code=403,
            )

        creds = _credentials(request.headers.get("authorization"))
        if creds is None:
            return self._challenge()

        user, supplied = creds
        expected_user = (self.settings.dashboard_user or "trader").strip()
        # compare_digest on both halves, and always both, so the time taken
        # does not reveal whether the username was the part that was wrong.
        ok_user = hmac.compare_digest(user, expected_user)
        ok_pass = hmac.compare_digest(supplied, password)
        if not (ok_user and ok_pass):
            log.warning("auth.bad_credentials", client=client, user=user[:40])
            return self._challenge()

        return await call_next(request)

    @staticmethod
    def _challenge() -> Response:
        return JSONResponse(
            {"error": "Authentication required."},
            status_code=401,
            headers=_UNAUTHORISED,
        )
