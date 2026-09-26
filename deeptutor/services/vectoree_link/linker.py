"""PKCE loopback, project-key mint, and in-memory link progress.

The poll snapshot never includes the API key or the console JWT. Those values
are written only to ``.vectoree/config.json`` and the model catalog.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, MutableMapping
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from deeptutor.services.config.model_catalog import ModelCatalogService, get_model_catalog_service

from .catalog import apply_vectoree_catalog, catalog_model_entries, parse_model_list
from .credentials import (
    detect_link,
    extract_project_id,
    link_status_payload,
    public_poll,
    read_device_id,
    redact_text,
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


class _CallbackServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, expected_state: str, done: threading.Event) -> None:
        self.expected_state = expected_state
        self.done = done
        self.code: str | None = None
        self.error: str | None = None
        super().__init__(("127.0.0.1", 0), _CallbackHandler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        super().server_bind()
        host = self.server_address[0]
        if host != "127.0.0.1":
            raise VectoreeLinkError("OAuth callback must bind to 127.0.0.1")


class _CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        server: _CallbackServer = self.server  # type: ignore[assignment]
        parsed = urlsplit(self.path)
        if parsed.path != "/callback":
            self._send(404, "<p>Not found</p>")
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        code = _first(query.get("code"))
        state = _first(query.get("state"))
        if (
            not code
            or not state
            or len(state) != len(server.expected_state)
            or not secrets.compare_digest(state, server.expected_state)
        ):
            self._send(400, "<p>Invalid Vectoree login callback.</p>")
            server.error = "Invalid OAuth callback"
            server.done.set()
            return
        self._send(
            200,
            "<!doctype html><title>Vectoree</title>"
            '<body style="font-family:sans-serif"><h1>Vectoree connected</h1>'
            "<p>You can close this window and return to DeepTutor.</p></body>",
        )
        server.code = code
        server.done.set()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


class Loopback:
    """One-shot callback listener bound only to ``127.0.0.1``."""

    def __init__(self, expected_state: str) -> None:
        self._done = threading.Event()
        self._server = _CallbackServer(expected_state, self._done)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="vectoree-link-callback",
            daemon=True,
        )
        self._thread.start()

    def wait_for_code(self, timeout: float) -> str:
        if not self._done.wait(timeout):
            self._server.error = "Console login timed out. Try connecting again."
            self._done.set()
        self.shutdown()
        if self._server.error:
            raise VectoreeLinkError(self._server.error)
        if not self._server.code:
            raise VectoreeLinkError("Invalid OAuth callback")
        return self._server.code

    def fail(self, reason: str) -> None:
        if not self._done.is_set():
            self._server.error = reason
            self._done.set()
        self.shutdown()

    def shutdown(self) -> None:
        thread = threading.Thread(
            target=self._stop, name="vectoree-link-callback-stop", daemon=True
        )
        thread.start()

    def _stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except OSError:
            return


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
        self._loopback: Loopback | None = None
        self._state: dict[str, Any] = {"status": "idle"}

    def status(self) -> dict[str, Any]:
        info = detect_link(self.root, self.env, _safe_catalog(self.catalog))
        return link_status_payload(info)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state.get("status") == "idle":
            info = detect_link(self.root, self.env, _safe_catalog(self.catalog))
            if info.linked:
                state = {
                    "status": "linked",
                    "apiUrl": info.api_url,
                    "projectName": info.project_name,
                }
        return public_poll(state)

    def start(self, *, api_url: str | None, project_id: str) -> dict[str, Any]:
        origin = normalize_api_url(api_url)
        resolved_id = extract_project_id(project_id)
        if resolved_id is None:
            raise VectoreeLinkError("projectId must be a UUID from the Vectoree console")
        ensure_root_writable(self.root)
        verifier, challenge = pkce_pair()
        state_value = secrets.token_hex(16)
        loopback = Loopback(state_value)
        redirect_uri = f"http://127.0.0.1:{loopback.port}/callback"
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
        with self._lock:
            self._generation += 1
            attempt = self._generation
            previous = self._loopback
            self._loopback = loopback
            self._state = {
                "status": "pending",
                "authorizeUrl": authorize_url,
                "apiUrl": origin,
                "projectId": resolved_id,
            }
        if previous is not None:
            previous.fail("Login cancelled")
        thread = threading.Thread(
            target=self._finish,
            kwargs={
                "attempt": attempt,
                "api_url": origin,
                "project_id": resolved_id,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "loopback": loopback,
            },
            name="vectoree-link",
            daemon=True,
        )
        thread.start()
        try:
            self._open_url(authorize_url)
        except Exception:
            logger.info("Vectoree authorize URL was not opened by the system browser")
        return self.snapshot()

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

    def _finish(
        self,
        *,
        attempt: int,
        api_url: str,
        project_id: str,
        verifier: str,
        redirect_uri: str,
        loopback: Loopback,
    ) -> None:
        try:
            code = loopback.wait_for_code(self._timeout_s)
            if not self._current(attempt):
                return
            session = _exchange_code(
                self._request_json,
                api_url,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )
            if not self._current(attempt):
                return
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
            if not self._current(attempt):
                return
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
            with self._lock:
                if attempt != self._generation:
                    return
                self._state = {
                    "status": "linked",
                    "apiUrl": api_url,
                    "projectId": project_id,
                    "projectName": project_name,
                }
                self._loopback = None
        except Exception as exc:
            if not self._current(attempt):
                return
            message = redact_text(str(exc) if str(exc) else "Could not connect Vectoree")
            logger.info("Vectoree link failed: %s", message)
            with self._lock:
                if attempt != self._generation:
                    return
                self._state = {
                    "status": "error",
                    "apiUrl": api_url,
                    "projectId": project_id,
                    "message": message,
                }
                self._loopback = None

    def _current(self, attempt: int) -> bool:
        with self._lock:
            return attempt == self._generation


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


def _first(values: list[str] | None) -> str:
    if not values:
        return ""
    return values[0]
