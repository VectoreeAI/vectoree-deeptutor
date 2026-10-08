"""Proxy DeepTutor sign-in to the linked Vectoree Auth API.

The project API key stays on the server. Vectoree user tokens are used only
to decide whether a DeepTutor session may be issued; they are not returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Literal

import httpx

from deeptutor.services.auth import TokenPayload, hash_password

from .credentials import read_config, read_env_file, usable_api_key
from .root import resolve_project_root

_DEFAULT_API_URL = "https://vectoree.ai"
_OTP = re.compile(r"^\d{8}$")
AUTH_PATHS = {
    "register": "/api/auth/users",
    "login": "/api/auth/sessions",
    "verify": "/api/auth/email/verify",
    "resend": "/api/auth/email/send-verification",
    "methods": "/api/auth/methods",
}


@dataclass(frozen=True)
class VectoreeAppLink:
    api_url: str
    api_key: str


@dataclass(frozen=True)
class AuthProxyResult:
    kind: Literal["session", "verify", "ok", "error"]
    http_status: int
    email: str | None = None
    message: str = ""
    subject: str | None = None
    issuer: str | None = None
    email_verified: bool = True


def resolve_vectoree_app_link(
    env: Mapping[str, str] | None = None,
    root: os.PathLike[str] | str | None = None,
) -> VectoreeAppLink | None:
    """Return the project key from the data volume, never from the image root."""

    source = os.environ if env is None else env
    data_root = Path(root) if root is not None else resolve_project_root(env)
    config = read_config(data_root)
    file_env = read_env_file(data_root)
    key = _first(
        source.get("VECTOREE_API_KEY"),
        config.get("apiKey"),
        file_env.get("VECTOREE_API_KEY"),
    )
    if not usable_api_key(key):
        return None
    api_url = _first(
        source.get("VECTOREE_API_URL"),
        config.get("apiUrl"),
        file_env.get("VECTOREE_API_URL"),
        _DEFAULT_API_URL,
    )
    return VectoreeAppLink(api_url=api_url.rstrip("/"), api_key=key)


def sanitize_auth_body(action: str, body: Mapping[str, Any]) -> dict[str, str]:
    email = str(body.get("email") or body.get("username") or "").strip()
    if not email:
        raise ValueError("email is required")
    if action == "verify":
        otp = str(body.get("otp") or "").strip()
        if not _OTP.fullmatch(otp):
            raise ValueError("otp must be 8 digits")
        return {"email": email, "otp": otp}
    if action == "resend":
        return {"email": email}
    password = body.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("password is required")
    return {"email": email, "password": password}


async def proxy_vectoree_auth(
    link: VectoreeAppLink,
    action: str,
    body: Mapping[str, Any] | None = None,
) -> AuthProxyResult:
    """Call Vectoree auth and return a result that contains no tokens."""

    payload = sanitize_auth_body(action, body or {}) if action != "methods" else None
    url = f"{link.api_url}{AUTH_PATHS[action]}"
    if action != "methods":
        url = f"{url}?client_type=server"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                "GET" if action == "methods" else "POST",
                url,
                headers={
                    "Authorization": f"Bearer {link.api_key}",
                    "Accept": "application/json",
                    **({"Content-Type": "application/json"} if payload is not None else {}),
                },
                json=payload,
            )
    except httpx.HTTPError:
        return AuthProxyResult(kind="error", http_status=502, message="Could not reach Vectoree")
    data = _read_json(response)
    if action == "resend":
        if response.status_code >= 400:
            return AuthProxyResult(
                kind="error",
                http_status=response.status_code,
                message="Could not resend the code",
            )
        return AuthProxyResult(kind="ok", http_status=200, message="ok")
    result = _interpret(action, response.status_code, data)
    if result.kind == "session":
        return replace(result, issuer=link.api_url)
    return result


def provision_local_user(result: AuthProxyResult) -> TokenPayload:
    """Return the DeepTutor account bound to the Vectoree user in ``result``.

    The Vectoree password is not stored. A random hash keeps local password
    login from accepting that same password if the project is later unlinked.
    Raises ``VectoreeIdentityConflict`` when the account belongs to someone else.
    """

    from deeptutor.multi_user.identity import claim_vectoree_account

    username, record = claim_vectoree_account(
        str(result.email or ""),
        issuer=str(result.issuer or ""),
        subject=str(result.subject or ""),
        email_verified=result.email_verified,
        new_password_hash=hash_password(secrets.token_urlsafe(32)),
    )
    return TokenPayload(
        username=username,
        role=str(record.get("role") or "user"),
        user_id=str(record.get("id") or ""),
    )


def _interpret(action: str, status: int, data: Any) -> AuthProxyResult:
    if _needs_verification(status, data):
        return AuthProxyResult(
            kind="verify",
            http_status=200,
            message="Enter the 8-digit code sent to your email.",
        )
    email = _read_email(data)
    subject = _read_user_field(data, "id")
    if 200 <= status < 300 and email and subject and _has_access_token(data):
        return AuthProxyResult(
            kind="session",
            http_status=200,
            email=email,
            subject=subject,
            email_verified=_read_email_verified(data),
        )
    if action == "login":
        return AuthProxyResult(
            kind="error",
            http_status=401,
            message="Incorrect email or password",
        )
    if action == "verify":
        return AuthProxyResult(
            kind="error",
            http_status=400,
            message="Invalid verification code",
        )
    if status in {400, 409}:
        return AuthProxyResult(
            kind="error",
            http_status=400,
            message="Email already registered",
        )
    return AuthProxyResult(
        kind="error",
        http_status=400,
        message="Could not create the account",
    )


def _needs_verification(status: int, data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("requireEmailVerification") is True:
        return True
    return status == 403 and data.get("error") == "AUTH_NEED_VERIFICATION"


def _has_access_token(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("accessToken"), str)
        and bool(data.get("accessToken"))
    )


def _read_email(data: Any) -> str | None:
    return _read_user_field(data, "email")


def _read_user_field(data: Any, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    value = user.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _read_email_verified(data: Any) -> bool:
    user = data.get("user") if isinstance(data, dict) else None
    return not (isinstance(user, dict) and user.get("emailVerified") is False)


def _read_json(response: httpx.Response) -> Any:
    text = response.text
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _first(*values: str | None) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
