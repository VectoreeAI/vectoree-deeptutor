"""PKCE on the published app port, project-key mint, and in-memory link progress.

The poll snapshot never includes the API key or the console JWT. Those values
are written only to ``<data>/.vectoree/`` and the model catalog. The browser
returns to ``/api/vectoree/link/callback`` on the host-visible origin, so a
login started from outside the container can finish.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, MutableMapping
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from deeptutor.services.config.model_catalog import ModelCatalogService, get_model_catalog_service

from .catalog import (
    apply_vectoree_catalog,
    catalog_model_entries,
    ensure_vectoree_placeholder,
    parse_model_list,
)
from .credentials import (
    clear_link_progress,
    consume_link_attempt,
    detect_link,
    extract_project_id,
    link_status_payload,
    public_poll,
    read_device_id,
    read_link_attempt,
    read_link_progress,
    redact_text,
    write_link_attempt,
    write_link_progress,
    write_linked_files,
)
from .errors import VectoreeLinkError
from .root import ensure_root_writable, resolve_project_root

logger = logging.getLogger(__name__)

KEY_SCOPES = (
    "gateway:chat",
    "gateway:models",
    "tools:*",
    "database:*",
    "storage:*",
    "auth:*",
)
_DEFAULT_ORIGIN = "https://vectoree.ai"
_CALLBACK_PATH = "/api/vectoree/link/callback"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TIMEOUT_SECONDS = 180.0
RequestJson = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None], tuple[int, Any]]


def normalize_api_url(raw: str | None) -> str:
    text = (raw or "").strip() or _DEFAULT_ORIGIN
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise VectoreeLinkError("API origin must be an http(s) URL")
    if parsed.username or parsed.password:
        raise VectoreeLinkError("API origin must not include credentials")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    origin = f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
    return origin


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def callback_redirect_uri(public_origin: str | None) -> str:
    """Build the host-visible callback on the origin the browser already uses.

    Only loopback hosts are accepted. The verifier stays on the server; the
    browser is sent back to the published DeepTutor port instead of a random
    port bound inside the container.
    """

    text = (public_origin or "").strip()
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or path not in {"", "/"}
        or host not in _LOOPBACK_HOSTS
    ):
        raise VectoreeLinkError(
            "Open Link in the browser on this machine. "
            "Vectoree must call back to localhost on the published DeepTutor port."
        )
    return f"{parsed.scheme}://{parsed.netloc}{_CALLBACK_PATH}"


class VectoreeLinker:
    def __init__(
        self,
        *,
        root: Path | None = None,
        env: MutableMapping[str, str] | None = None,
        catalog: ModelCatalogService | None = None,
        request_json: RequestJson | None = None,
        open_url: Callable[[str], None] | None = None,
        timeout_s: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._root = root
        self._env = env
        self._catalog = catalog
        self._request_json = request_json or _request_json
        self._open_url = open_url or open_system_browser
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._generation = 0
        self._pending: dict[str, Any] | None = None
        self._state: dict[str, Any] = {"status": "idle"}

    def status(self) -> dict[str, Any]:
        try:
            ensure_vectoree_placeholder(self.catalog)
        except Exception:
            logger.info("Vectoree placeholder catalog was not seeded")
        info = detect_link(self.root, self.env, _safe_catalog(self.catalog))
        return link_status_payload(info)

    def snapshot(self) -> dict[str, Any]:
        disk = self._disk_state()
        if disk and disk.get("status") == "pending":
            return public_poll(disk)
        info = detect_link(self.root, self.env, _safe_catalog(self.catalog))
        if info.linked:
            progress = disk if disk and disk.get("status") == "linked" else {}
            return public_poll(
                {
                    "status": "linked",
                    "apiUrl": info.api_url or str(progress.get("apiUrl") or ""),
                    "projectName": info.project_name or str(progress.get("projectName") or ""),
                    "projectId": str(progress.get("projectId") or ""),
                }
            )
        if disk and disk.get("status") == "error":
            return public_poll(disk)
        with self._lock:
            state = dict(self._state)
        if state.get("status") == "error":
            return public_poll(state)
        return public_poll({"status": "idle"})

    def start(
        self,
        *,
        api_url: str | None,
        project_id: str,
        public_origin: str | None = None,
    ) -> dict[str, Any]:
        origin = normalize_api_url(api_url)
        resolved_id = extract_project_id(project_id)
        if resolved_id is None:
            raise VectoreeLinkError("projectId must be a UUID from the Vectoree console")
        ensure_root_writable(self.root)
        redirect_uri = callback_redirect_uri(public_origin)
        verifier, challenge = pkce_pair()
        state_value = secrets.token_hex(16)
        query = urlencode(
            {
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": redirect_uri,
                "state": state_value,
            }
        )
        authorize_url = f"{origin}/api/system/auth/cli/authorize?{query}"
        pending = {
            "attempt": 0,
            "state": state_value,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "api_url": origin,
            "project_id": resolved_id,
            "authorizeUrl": authorize_url,
            "expiresAt": time.time() + self._timeout_s,
        }
        with self._lock:
            self._generation += 1
            pending["attempt"] = self._generation
            self._pending = pending
            self._state = {
                "status": "pending",
                "authorizeUrl": authorize_url,
                "apiUrl": origin,
                "projectId": resolved_id,
            }
        clear_link_progress(self.root)
        write_link_attempt(self.root, pending)
        self._arm_timeout(int(pending["attempt"]))
        try:
            self._open_url(authorize_url)
        except Exception:
            logger.info("Vectoree authorize URL was not opened by the system browser")
        return self.snapshot()

    def accept_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
    ) -> None:
        """Finish a login when the host browser hits the published callback."""

        pending = consume_link_attempt(self.root, state or "")
        if pending is None:
            raise VectoreeLinkError("Invalid OAuth callback")
        if float(pending.get("expiresAt") or 0) < time.time():
            self._mark_error(
                int(pending.get("attempt") or 0),
                str(pending.get("api_url") or ""),
                str(pending.get("project_id") or ""),
                "Console login timed out. Try connecting again.",
            )
            raise VectoreeLinkError("Console login timed out. Try connecting again.")
        with self._lock:
            attempt = int(pending.get("attempt") or self._generation)
            self._pending = None
            verifier = str(pending["verifier"])
            redirect_uri = str(pending["redirect_uri"])
            api_url = str(pending["api_url"])
            project_id = str(pending["project_id"])
        if error or not code:
            message = redact_text(error or "", fallback="Invalid OAuth callback")
            self._mark_error(attempt, api_url, project_id, message)
            raise VectoreeLinkError(message)
        try:
            session = _exchange_code(
                self._request_json,
                api_url,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )
            project_name = _lookup_project_name(
                self._request_json, api_url, session["accessToken"], project_id
            )
            minted = _mint_project_key(
                self._request_json,
                api_url,
                session["accessToken"],
                project_id=project_id,
                root=self.root,
            )
            if self._replaced(state or ""):
                raise VectoreeLinkError("Login cancelled")
            models = _list_models(self._request_json, api_url, minted["apiKey"])
            apply_vectoree_catalog(
                self.catalog,
                api_url=api_url,
                api_key=minted["apiKey"],
                models=catalog_model_entries(models),
            )
            write_linked_files(
                self.root,
                {
                    "apiUrl": api_url,
                    "accessToken": session["accessToken"],
                    "refreshToken": session.get("refreshToken", ""),
                    "apiKey": minted["apiKey"],
                    "projectId": project_id,
                    "projectName": project_name,
                    "keyId": minted.get("keyId", ""),
                },
                self.env,
            )
            linked = {
                "status": "linked",
                "apiUrl": api_url,
                "projectId": project_id,
                "projectName": project_name,
            }
            write_link_progress(self.root, linked)
            with self._lock:
                self._pending = None
                self._state = linked
        except VectoreeLinkError as exc:
            if str(exc) == "Login cancelled":
                raise
            message = redact_text(str(exc))
            self._mark_error(attempt, api_url, project_id, message)
            raise VectoreeLinkError(message) from exc
        except Exception as exc:
            if self._replaced(state or ""):
                raise VectoreeLinkError("Login cancelled") from exc
            message = redact_text(str(exc) if str(exc) else "Could not connect Vectoree")
            logger.info("Vectoree link failed: %s", message)
            self._mark_error(attempt, api_url, project_id, message)
            raise VectoreeLinkError(message) from exc

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        return resolve_project_root()

    @property
    def env(self) -> MutableMapping[str, str]:
        if self._env is not None:
            return self._env
        return os.environ

    @property
    def catalog(self) -> ModelCatalogService:
        if self._catalog is not None:
            return self._catalog
        return get_model_catalog_service()

    def _disk_state(self) -> dict[str, Any] | None:
        attempt = read_link_attempt(self.root)
        if attempt:
            expires = float(attempt.get("expiresAt") or 0)
            api_url = str(attempt.get("api_url") or "")
            project_id = str(attempt.get("project_id") or "")
            if expires and expires < time.time():
                clear_link_attempt_state = str(attempt.get("state") or "")
                if consume_link_attempt(self.root, clear_link_attempt_state) is not None:
                    self._mark_error(
                        int(attempt.get("attempt") or 0),
                        api_url,
                        project_id,
                        "Console login timed out. Try connecting again.",
                    )
                progress = read_link_progress(self.root)
                return progress or None
            return {
                "status": "pending",
                "authorizeUrl": str(attempt.get("authorizeUrl") or ""),
                "apiUrl": api_url,
                "projectId": project_id,
            }
        progress = read_link_progress(self.root)
        if progress.get("status") in {"linked", "error"}:
            return progress
        return None

    def _arm_timeout(self, attempt: int) -> None:
        def fire() -> None:
            with self._lock:
                pending = self._pending
                if attempt != self._generation or pending is None:
                    return
                state_value = str(pending.get("state") or "")
                api_url = str(pending.get("api_url") or "")
                project_id = str(pending.get("project_id") or "")
            if consume_link_attempt(self.root, state_value) is None:
                return
            self._mark_error(
                attempt,
                api_url,
                project_id,
                "Console login timed out. Try connecting again.",
            )

        timer = threading.Timer(self._timeout_s, fire)
        timer.daemon = True
        timer.start()

    def _mark_error(self, attempt: int, api_url: str, project_id: str, message: str) -> None:
        progress = {
            "status": "error",
            "apiUrl": api_url,
            "projectId": project_id,
            "message": message,
        }
        write_link_progress(self.root, progress)
        with self._lock:
            self._pending = None
            if attempt == self._generation or attempt == 0:
                self._state = progress

    def _replaced(self, state: str) -> bool:
        """True when a newer login has been stored since this callback was claimed."""

        newer = read_link_attempt(self.root)
        newer_state = str(newer.get("state") or "")
        return bool(newer_state) and newer_state != state


_linker: VectoreeLinker | None = None
_linker_lock = threading.Lock()


def get_linker() -> VectoreeLinker:
    global _linker
    with _linker_lock:
        if _linker is None:
            _linker = VectoreeLinker()
        return _linker


def reset_linker(linker: VectoreeLinker | None = None) -> None:
    """Test hook. Production code uses :func:`get_linker`."""

    global _linker
    with _linker_lock:
        _linker = linker


def open_system_browser(url: str) -> None:
    if sys.platform == "darwin":
        command = ["open", url]
    elif sys.platform == "win32":
        command = ["cmd", "/c", "start", "", url]
    else:
        command = ["xdg-open", url]
    subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _request_json(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any] | None,
) -> tuple[int, Any]:
    with httpx.Client(timeout=30.0) as client:
        response = client.request(method, url, headers=dict(headers), json=body)
    text = response.text
    if not text:
        return response.status_code, None
    try:
        return response.status_code, json.loads(text)
    except json.JSONDecodeError:
        return response.status_code, {"message": text[:500]}


def _exchange_code(
    request_json: RequestJson,
    api_url: str,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, str]:
    status, data = request_json(
        "POST",
        f"{api_url}/api/system/auth/cli/token",
        {"Accept": "application/json", "Content-Type": "application/json"},
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )
    if status < 200 or status >= 300:
        raise VectoreeLinkError(_error_message(data, f"Console login failed ({status})"))
    record = data if isinstance(data, Mapping) else {}
    access = record.get("accessToken")
    if not isinstance(access, str) or not access:
        raise VectoreeLinkError("Console login did not return a session")
    refresh = record.get("refreshToken")
    return {
        "accessToken": access,
        "refreshToken": refresh if isinstance(refresh, str) else "",
    }


def _lookup_project_name(
    request_json: RequestJson,
    api_url: str,
    access_token: str,
    project_id: str,
) -> str:
    status, data = request_json(
        "GET",
        f"{api_url}/api/projects",
        {"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        None,
    )
    if status < 200 or status >= 300:
        raise VectoreeLinkError(_error_message(data, f"Could not list projects ({status})"))
    projects = data if isinstance(data, list) else []
    for item in projects:
        if isinstance(item, Mapping) and item.get("id") == project_id:
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
            return project_id
    raise VectoreeLinkError("That project was not found on this Vectoree account")


def _mint_project_key(
    request_json: RequestJson,
    api_url: str,
    access_token: str,
    *,
    project_id: str,
    root: Path,
) -> dict[str, str]:
    status, data = request_json(
        "POST",
        f"{api_url}/api/gateway/keys",
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-Project-Id": project_id,
            "User-Agent": "VectoreeCLI",
        },
        {
            "name": f"CLI — {socket.gethostname()}"[:128],
            "projectId": project_id,
            "deviceId": read_device_id(root),
            "localFolderPath": str(root),
            "scopes": list(KEY_SCOPES),
        },
    )
    if status < 200 or status >= 300:
        raise VectoreeLinkError(_error_message(data, f"Could not create a project key ({status})"))
    record = data if isinstance(data, Mapping) else {}
    api_key = record.get("apiKey")
    if not isinstance(api_key, str) or not api_key.startswith("sk-ve-"):
        raise VectoreeLinkError("Vectoree did not return a project API key")
    key_id = record.get("id")
    return {"apiKey": api_key, "keyId": key_id if isinstance(key_id, str) else ""}


def _list_models(request_json: RequestJson, api_url: str, api_key: str) -> list[Any]:
    try:
        status, data = request_json(
            "GET",
            f"{api_url}/api/v1/models",
            {"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            None,
        )
    except Exception:
        logger.info("Vectoree model list was unavailable; using vectoree/auto")
        return []
    if status < 200 or status >= 300:
        logger.info("Vectoree model list returned %s; using vectoree/auto", status)
        return []
    return parse_model_list(data)


def _error_message(data: Any, fallback: str) -> str:
    message = fallback
    if isinstance(data, Mapping):
        raw = data.get("message")
        if isinstance(raw, str) and raw.strip():
            message = raw.strip()
        else:
            error = data.get("error")
            if isinstance(error, str) and error.strip():
                message = error.strip()
            elif isinstance(error, Mapping):
                nested = error.get("message")
                if isinstance(nested, str) and nested.strip():
                    message = nested.strip()
    return redact_text(message, fallback=fallback)


def _safe_catalog(service: ModelCatalogService) -> dict[str, Any] | None:
    try:
        loaded = service.load()
    except Exception:
        logger.info("Vectoree link status could not read the model catalog")
        return None
    return loaded if isinstance(loaded, dict) else None
