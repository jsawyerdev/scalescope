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
        self._username = username
        self._password = password

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
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[len("Basic ") :]).decode("utf-8")
            username, _, password = decoded.partition(":")
        except binascii.Error, UnicodeDecodeError:
            return False
        # Constant-time comparison: a timing difference between a wrong
        # username and a wrong password would leak which one was correct.
        return secrets.compare_digest(
            username, self._username
        ) and secrets.compare_digest(password, self._password)
