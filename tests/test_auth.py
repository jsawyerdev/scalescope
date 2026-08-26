from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scalescope.auth import BasicAuthMiddleware


def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/protected")
    def protected() -> dict[str, str]:
        return {"data": "secret"}

    app.add_middleware(BasicAuthMiddleware, username="operator", password="s3cret")
    return app


def test_healthz_exempt_without_credentials() -> None:
    client = TestClient(_app())
    response = client.get("/healthz")
    assert response.status_code == 200


def test_protected_route_rejects_missing_credentials() -> None:
    client = TestClient(_app())
    response = client.get("/protected")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Basic realm="ScaleScope"'


def test_protected_route_rejects_wrong_credentials() -> None:
    client = TestClient(_app())
    response = client.get(
        "/protected", headers={"Authorization": _basic_header("operator", "wrong")}
    )
    assert response.status_code == 401


def test_protected_route_accepts_correct_credentials() -> None:
    client = TestClient(_app())
    response = client.get(
        "/protected", headers={"Authorization": _basic_header("operator", "s3cret")}
    )
    assert response.status_code == 200
    assert response.json() == {"data": "secret"}


def test_authorization_scheme_is_case_insensitive() -> None:
    client = TestClient(_app())
    response = client.get(
        "/protected",
        headers={
            "Authorization": _basic_header("operator", "s3cret").replace(
                "Basic", "basic"
            )
        },
    )
    assert response.status_code == 200


def test_malformed_authorization_header_rejected() -> None:
    client = TestClient(_app())
    response = client.get(
        "/protected", headers={"Authorization": "Basic not-valid-base64!!"}
    )
    assert response.status_code == 401


def test_authorization_header_without_colon_rejected() -> None:
    client = TestClient(_app())
    token = base64.b64encode(b"operator").decode()
    response = client.get("/protected", headers={"Authorization": f"Basic {token}"})
    assert response.status_code == 401
