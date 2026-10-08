"""Vectoree sign-in keeps one DeepTutor identity per Vectoree account.

A linked project turns on per-account auth even with ``AUTH_ENABLED=false``,
local accounts are bound to the Vectoree user id rather than the email, and only
an administrator may relink the project once it is linked.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.multi_user.identity import (
    VectoreeIdentityConflict,
    claim_vectoree_account,
    get_user,
    save_user,
)
from deeptutor.services.auth import TokenPayload

ISSUER = "https://vectoree.ai"


def _claim(email: str, subject: str, *, verified: bool = True, issuer: str = ISSUER):
    return claim_vectoree_account(
        email,
        issuer=issuer,
        subject=subject,
        email_verified=verified,
        new_password_hash="$2b$12$placeholder",
    )


def test_new_vectoree_user_gets_a_bound_account(mu_isolated_root) -> None:
    _claim("owner@example.com", "vt_owner")
    username, record = _claim("alice@example.com", "vt_alice")

    assert username == "alice@example.com"
    assert record["role"] == "user"
    assert record["vectoree"] == {"issuer": ISSUER, "subject": "vt_alice"}
    assert get_user("alice@example.com")["vectoree"] == record["vectoree"]


def test_same_identity_returns_the_same_account(mu_isolated_root) -> None:
    _, first = _claim("alice@example.com", "vt_alice")
    _, second = _claim("alice@example.com", "vt_alice")

    assert first["id"] == second["id"]


def test_binding_survives_a_changed_vectoree_email(mu_isolated_root) -> None:
    _, first = _claim("alice@example.com", "vt_alice")
    username, second = _claim("alice@new.example.com", "vt_alice")

    assert username == "alice@example.com"
    assert second["id"] == first["id"]


def test_other_vectoree_account_cannot_take_a_bound_email(mu_isolated_root) -> None:
    _claim("alice@example.com", "vt_alice")

    with pytest.raises(VectoreeIdentityConflict):
        _claim("alice@example.com", "vt_attacker")
    with pytest.raises(VectoreeIdentityConflict):
        _claim("alice@example.com", "vt_alice", issuer="https://evil.example")


def test_unverified_email_cannot_claim_an_existing_local_account(mu_isolated_root) -> None:
    save_user("admin@example.com", "$2b$12$local", role="admin")

    with pytest.raises(VectoreeIdentityConflict):
        _claim("admin@example.com", "vt_someone", verified=False)
    assert "vectoree" not in get_user("admin@example.com")


def test_verified_email_binds_an_existing_local_account(mu_isolated_root) -> None:
    existing = save_user("admin@example.com", "$2b$12$local", role="admin")

    _, record = _claim("admin@example.com", "vt_admin")

    assert record["id"] == existing["id"]
    assert record["role"] == "admin"
    assert get_user("admin@example.com")["vectoree"]["subject"] == "vt_admin"


def test_disabled_account_cannot_sign_in(mu_isolated_root) -> None:
    from deeptutor.multi_user import identity

    _claim("alice@example.com", "vt_alice")
    users = identity.load_users()
    users["alice@example.com"]["disabled"] = True
    identity._write_users(users)

    with pytest.raises(VectoreeIdentityConflict):
        _claim("alice@example.com", "vt_alice")


def test_missing_vectoree_user_id_is_rejected(mu_isolated_root) -> None:
    with pytest.raises(VectoreeIdentityConflict):
        _claim("alice@example.com", "")


def test_binding_survives_a_password_reset(mu_isolated_root) -> None:
    _claim("alice@example.com", "vt_alice")
    save_user("alice@example.com", "$2b$12$reset")

    assert get_user("alice@example.com")["vectoree"]["subject"] == "vt_alice"


@pytest.fixture
def linked_app(mu_isolated_root, monkeypatch):
    """Auth router plus a scoped probe route with Vectoree linked, AUTH_ENABLED off."""

    import deeptutor.api.routers.auth as auth_router
    from deeptutor.multi_user.context import get_current_user
    from deeptutor.services import auth as auth_service

    alice = save_user("alice@example.com", "$2b$12$placeholder", role="user")
    bob = save_user("bob@example.com", "$2b$12$placeholder", role="user")
    tokens = {
        "alice": TokenPayload(username="alice@example.com", role="user", user_id=alice["id"]),
        "bob": TokenPayload(username="bob@example.com", role="user", user_id=bob["id"]),
    }
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_service, "AUTH_ENABLED", False)
    monkeypatch.setattr(auth_service, "vectoree_login_active", lambda: True)
    monkeypatch.setattr(auth_router, "decode_token", lambda token: tokens.get(token))

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")

    @app.get("/probe", dependencies=[Depends(auth_router.require_auth)])
    async def probe() -> dict:
        user = get_current_user()
        return {"id": user.id, "admin": user.is_admin, "root": str(user.scope.root)}

    return TestClient(app), alice, bob


def test_linked_install_rejects_anonymous_requests(linked_app) -> None:
    client, _, _ = linked_app

    assert client.get("/probe").status_code == 401
    status = client.get("/api/auth/status").json()
    assert status["enabled"] is True
    assert status["authenticated"] is False


def test_linked_install_scopes_each_vectoree_user(linked_app) -> None:
    client, alice, bob = linked_app

    as_alice = client.get("/probe", headers={"Authorization": "Bearer alice"}).json()
    as_bob = client.get("/probe", headers={"Authorization": "Bearer bob"}).json()

    assert as_alice["id"] == alice["id"]
    assert as_bob["id"] == bob["id"]
    assert as_alice["admin"] is False
    assert as_alice["root"] != as_bob["root"]
    assert as_alice["root"].endswith(alice["id"])


def test_linked_install_denies_admin_routes_to_users(linked_app) -> None:
    client, _, _ = linked_app

    response = client.get("/api/auth/users", headers={"Authorization": "Bearer alice"})
    assert response.status_code == 403


def test_vectoree_conflict_is_a_403_not_a_session(mu_isolated_root, monkeypatch) -> None:
    import deeptutor.api.routers.auth as auth_router
    from deeptutor.services.vectoree_link import app_auth
    from deeptutor.services.vectoree_link.app_auth import AuthProxyResult, VectoreeAppLink

    _claim("alice@example.com", "vt_alice")

    async def proxy(link, action, body):
        return AuthProxyResult(
            kind="session",
            http_status=200,
            email="alice@example.com",
            subject="vt_attacker",
            issuer=ISSUER,
        )

    monkeypatch.setattr(
        app_auth,
        "resolve_vectoree_app_link",
        lambda: VectoreeAppLink(api_url=ISSUER, api_key="sk-ve-v1-test"),
    )
    monkeypatch.setattr(app_auth, "proxy_vectoree_auth", proxy)
    monkeypatch.setattr(auth_router, "ensure_auth_secret", lambda: "test-secret")

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    response = TestClient(app).post(
        "/api/auth/login",
        json={"username": "alice@example.com", "password": "secret"},
    )

    assert response.status_code == 403
    assert "dt_token" not in response.headers.get("set-cookie", "")


@pytest.fixture
def link_router_app(mu_isolated_root, monkeypatch):
    from deeptutor.api.routers import vectoree_link as link_router
    from deeptutor.multi_user.context import reset_current_user, set_current_user
    from deeptutor.services.vectoree_link import app_auth
    from deeptutor.services.vectoree_link.app_auth import VectoreeAppLink

    started = []

    class _Linker:
        def start(self, **kwargs):
            started.append(kwargs)
            return {"status": "pending"}

        def status(self):
            return {"linked": True}

        def snapshot(self):
            return {"status": "linked"}

    monkeypatch.setattr(link_router, "get_linker", lambda: _Linker())
    monkeypatch.setattr(
        app_auth,
        "resolve_vectoree_app_link",
        lambda: VectoreeAppLink(api_url=ISSUER, api_key="sk-ve-v1-test"),
    )

    def client_as(user):
        app = FastAPI()

        async def install_user():
            token = set_current_user(user)
            try:
                yield
            finally:
                reset_current_user(token)

        app.include_router(link_router.public_router, prefix="/api/vectoree")
        app.include_router(
            link_router.router,
            prefix="/api/vectoree",
            dependencies=[Depends(install_user)],
        )
        return TestClient(app)

    return client_as, started


def test_relink_requires_an_admin(link_router_app, make_user) -> None:
    client_as, started = link_router_app
    body = {"projectId": "00000000-0000-0000-0000-000000000000"}

    denied = client_as(make_user("u_alice")).post("/api/vectoree/link/start", json=body)
    allowed = client_as(make_user("u_admin", role="admin")).post(
        "/api/vectoree/link/start", json=body
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert len(started) == 1


def test_link_status_and_poll_are_public(link_router_app, make_user) -> None:
    client_as, _ = link_router_app
    client = client_as(make_user("u_alice"))

    assert client.get("/api/vectoree/link-status").json() == {"linked": True}
    assert client.get("/api/vectoree/link").json() == {"status": "linked"}
