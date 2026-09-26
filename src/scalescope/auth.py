"""Optional HTTP Basic Auth in front of the entire app (API and dashboard).

Applied as ASGI middleware, not a FastAPI route dependency, because a route
dependency would leave the StaticFiles-mounted dashboard (index.html/app.js/
style.css) unprotected while only guarding /api/*. `/healthz` is exempt so
the Docker HEALTHCHECK keeps working without credentials.
"""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

EXEMPT_PATHS = frozenset({"/healthz"})


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        super().__init__(app)
        # Compared as bytes: secrets.compare_digest rejects non-ASCII str.
        self._username = username.encode("utf-8")
        self._password = password.encode("utf-8")

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in EXEMPT_PATHS or self._authorized(request):
            return await call_next(request)
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="ScaleScope"'},
        )

    def _authorized(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "basic" or not credentials:
            return False
        try:
            decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
            username, separator, password = decoded.partition(":")
        except binascii.Error, UnicodeDecodeError:
            return False
        if not separator:
            return False
        # Both comparisons always run (no short-circuit) so response timing
        # does not reveal whether the username alone was correct.
        username_ok = secrets.compare_digest(username.encode("utf-8"), self._username)
        password_ok = secrets.compare_digest(password.encode("utf-8"), self._password)
        return username_ok and password_ok
