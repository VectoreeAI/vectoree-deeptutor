"""Vectoree Auth proxy: project key stays server-side and tokens are not returned."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.services import auth as auth_service
from deeptutor.services.auth import TokenPayload
from deeptutor.services.vectoree_link import app_auth
from deeptutor.services.vectoree_link.app_auth import (
    AuthProxyResult,
    VectoreeAppLink,
    proxy_vectoree_auth,
    resolve_vectoree_app_link,
    sanitize_auth_body,
)

API_KEY = "sk-ve-v1-test-secret"
ACCESS_TOKEN = "eyJhbGciOiJub25lIn0.payload.signature"


def test_resolve_link_reads_the_data_volume(tmp_path: Path) -> None:
    vectoree = tmp_path / ".vectoree"
    vectoree.mkdir()
    (vectoree / "config.json").write_text(
        json.dumps({"apiKey": API_KEY, "apiUrl": "https://vectoree.ai"}),
        encoding="utf-8",
    )
    (vectoree / "env").write_text("VECTOREE_API_KEY=\n", encoding="utf-8")

    link = resolve_vectoree_app_link({}, root=tmp_path)

    assert link == VectoreeAppLink(api_url="https://vectoree.ai", api_key=API_KEY)


def test_blank_key_is_not_linked(tmp_path: Path) -> None:
    assert resolve_vectoree_app_link({"VECTOREE_API_KEY": ""}, root=tmp_path) is None


def test_sanitize_rejects_a_short_code() -> None:
    assert sanitize_auth_body("login", {"username": "a@example.com", "password": "secret"}) == {
        "email": "a@example.com",
        "password": "secret",
    }
    with pytest.raises(ValueError, match="8 digits"):
        sanitize_auth_body("verify", {"email": "a@example.com", "otp": "1234"})


@pytest.mark.asyncio
async def test_proxy_login_hides_vectoree_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["client_type"] == "server"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(
            200,
            json={
                "user": {"id": "vt_a", "email": "a@example.com", "emailVerified": False},
                "accessToken": ACCESS_TOKEN,
                "refreshToken": "refresh-secret",
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _Client:
        def __init__(self, **kwargs):
            kwargs["transport"] = transport
            self._client = real_client(**kwargs)

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *args):
            await self._client.aclose()

    monkeypatch.setattr(app_auth.httpx, "AsyncClient", _Client)
    result = await proxy_vectoree_auth(
        VectoreeAppLink(api_url="https://vectoree.ai", api_key=API_KEY),
        "login",
        {"email": "a@example.com", "password": "secret"},
    )

    assert result == AuthProxyResult(
        kind="session",
        http_status=200,
        email="a@example.com",
        subject="vt_a",
        issuer="https://vectoree.ai",
        email_verified=False,
    )
    dumped = json.dumps(result.__dict__)
    assert ACCESS_TOKEN not in dumped
    assert API_KEY not in dumped


@pytest.mark.asyncio
async def test_proxy_resend_does_not_require_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/email/send-verification"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _Client:
        def __init__(self, **kwargs):
            kwargs["transport"] = transport
            self._client = real_client(**kwargs)

        async def __aenter__(self):
            return self._client

        async def __aexit__(self, *args):
            await self._client.aclose()

    monkeypatch.setattr(app_auth.httpx, "AsyncClient", _Client)
    result = await proxy_vectoree_auth(
        VectoreeAppLink(api_url="https://vectoree.ai", api_key=API_KEY),
        "resend",
        {"email": "a@example.com"},
    )
    assert result.kind == "ok"
    assert API_KEY not in json.dumps(result.__dict__)


def test_login_issues_a_deeptutor_cookie_without_the_vectoree_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def proxy(link, action, body):
        assert action == "login"
        assert body["password"] == "secret"
        return AuthProxyResult(kind="session", http_status=200, email="a@example.com")

    monkeypatch.setattr(
        app_auth,
        "resolve_vectoree_app_link",
        lambda: VectoreeAppLink(api_url="https://vectoree.ai", api_key=API_KEY),
    )
    monkeypatch.setattr(app_auth, "proxy_vectoree_auth", proxy)
    monkeypatch.setattr(
        app_auth,
        "provision_local_user",
        lambda result: TokenPayload(username=result.email, role="user", user_id="u_test"),
    )
    monkeypatch.setattr(auth_service, "AUTH_SECRET", "test-secret")
    monkeypatch.setattr(auth_service, "TOKEN_EXPIRE_HOURS", 24)
    monkeypatch.setattr(auth_router, "ensure_auth_secret", lambda: "test-secret")

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"username": "a@example.com", "password": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["username"] == "a@example.com"
    assert "dt_token" in response.headers["set-cookie"]
    body = response.text
    assert ACCESS_TOKEN not in body
    assert API_KEY not in body
    assert "refresh-secret" not in body
