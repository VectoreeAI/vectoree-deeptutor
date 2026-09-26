"""Read and write Vectoree link files without ever returning secrets."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9._\-]+|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_DASHBOARD_PROJECT_RE = re.compile(
    r"Dashboard project:[^\n]*\(([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\)",
    re.IGNORECASE,
)
_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_CONFIG_KEYS = (
    "apiUrl",
    "accessToken",
    "refreshToken",
    "apiKey",
    "projectId",
    "projectName",
    "keyId",
)
_PUBLIC_POLL_KEYS = (
    "status",
    "authorizeUrl",
    "message",
    "apiUrl",
    "projectId",
    "projectName",
)


@dataclass(frozen=True)
class LinkInfo:
    linked: bool
    project_name: str = ""
    api_url: str = ""


def redact_text(text: str, *, fallback: str = "Could not connect Vectoree") -> str:
    """Strip key-shaped and JWT-shaped substrings from a browser-facing message."""

    cleaned = _SECRET_RE.sub("[redacted]", text or "")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return fallback
    return cleaned[:400]


def extract_project_id(text: str) -> str | None:
    """Pull a project UUID out of a raw id or a Console URL / dashboard paste."""

    dashboard = _DASHBOARD_PROJECT_RE.search(text or "")
    if dashboard:
        return dashboard.group(1)
    match = _UUID_RE.search(text or "")
    return match.group(0) if match else None


def usable_api_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or set(text) <= {"*"}:
        return False
    return True


def read_config(root: Path) -> dict[str, str]:
    path = root / ".vectoree" / "config.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): value for key, value in loaded.items() if isinstance(value, str) and value}


def read_env_file(root: Path) -> dict[str, str]:
    path = root / ".env"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_RE.match(stripped)
        if not match:
            continue
        key = match.group(1)
        raw = stripped.split("=", 1)[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        values[key] = raw
    return values


def upsert_env(content: str, updates: Mapping[str, str]) -> str:
    """Replace listed keys and keep every other line, including comments."""

    lines = content.split("\n")
    seen: set[str] = set()
    next_lines: list[str] = []
    for line in lines:
        match = _ENV_KEY_RE.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            seen.add(key)
            next_lines.append(f"{key}={updates[key]}")
            continue
        next_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            next_lines.append(f"{key}={value}")
    while next_lines and next_lines[-1] == "":
        next_lines.pop()
    return "\n".join(next_lines) + "\n"


def read_device_id(root: Path) -> str:
    path = root / ".vectoree" / "device-id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= 8:
        return existing[:128]
    device_id = str(uuid4())
    _write_private(path, f"{device_id}\n")
    return device_id


def write_linked_files(
    root: Path,
    credentials: Mapping[str, str],
    env: MutableMapping[str, str],
) -> None:
    """Persist ``.vectoree/config.json`` and upsert Vectoree keys in ``.env``."""

    directory = root / ".vectoree"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    existing = read_config(root)
    ordered: dict[str, str] = {}
    merged = {**existing, **{key: value for key, value in credentials.items() if value}}
    for key in _CONFIG_KEYS:
        value = merged.get(key, "")
        if value:
            ordered[key] = value
    for key, value in merged.items():
        if key not in ordered and value and key not in {"api_url", "api_key", "project_id"}:
            ordered[key] = value
    _write_private(directory / "config.json", json.dumps(ordered, indent=2) + "\n")

    api_url = credentials["apiUrl"].rstrip("/")
    updates = {
        "VECTOREE_API_URL": api_url,
        "VECTOREE_API_KEY": credentials["apiKey"],
        "VECTOREE_API_BASE": f"{api_url}/api/v1",
    }
    env_path = root / ".env"
    try:
        current = env_path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    _write_private(env_path, upsert_env(current, updates))
    env["VECTOREE_API_URL"] = updates["VECTOREE_API_URL"]
    env["VECTOREE_API_KEY"] = updates["VECTOREE_API_KEY"]
    env["VECTOREE_API_BASE"] = updates["VECTOREE_API_BASE"]


def detect_link(
    root: Path,
    env: Mapping[str, str],
    catalog: Mapping[str, Any] | None,
) -> LinkInfo:
    """Linked when a real project key exists in config, env, or the catalog."""

    config = read_config(root)
    if usable_api_key(config.get("apiKey")):
        return LinkInfo(
            linked=True,
            project_name=config.get("projectName", ""),
            api_url=config.get("apiUrl", ""),
        )

    disk_env = read_env_file(root)
    env_key = env.get("VECTOREE_API_KEY") or disk_env.get("VECTOREE_API_KEY")
    if usable_api_key(env_key):
        api_url = (
            disk_env.get("VECTOREE_API_URL")
            or env.get("VECTOREE_API_URL")
            or config.get("apiUrl")
            or ""
        )
        return LinkInfo(linked=True, project_name=config.get("projectName", ""), api_url=api_url)

    catalog_url, catalog_name = _catalog_vectoree(catalog)
    if catalog_url is not None:
        return LinkInfo(linked=True, project_name=catalog_name, api_url=catalog_url)
    return LinkInfo(linked=False)


def link_status_payload(info: LinkInfo) -> dict[str, Any]:
    """Public status. Never includes an API key or a token."""

    payload: dict[str, Any] = {"linked": info.linked}
    if not info.linked:
        return payload
    if info.project_name:
        payload["projectName"] = info.project_name
    if info.api_url:
        payload["apiUrl"] = info.api_url
    return payload


def public_poll(state: Mapping[str, Any]) -> dict[str, Any]:
    """Poll payload. Drops every field except the public link progress keys."""

    payload: dict[str, Any] = {"status": str(state.get("status") or "idle")}
    for key in _PUBLIC_POLL_KEYS:
        if key == "status":
            continue
        value = state.get(key)
        if isinstance(value, str) and value:
            payload[key] = redact_text(value) if key == "message" else value
    return payload


def _catalog_vectoree(catalog: Mapping[str, Any] | None) -> tuple[str | None, str]:
    if not isinstance(catalog, Mapping):
        return None, ""
    connections = catalog.get("connections")
    if isinstance(connections, list):
        for connection in connections:
            if not isinstance(connection, Mapping) or not _is_vectoree_record(connection):
                continue
            if usable_api_key(connection.get("api_key")):
                return _origin_from_base(connection.get("base_url")), "Vectoree"
    services = catalog.get("services")
    if isinstance(services, Mapping):
        for service_name in ("llm", "task"):
            service = services.get(service_name)
            if not isinstance(service, Mapping):
                continue
            profiles = service.get("profiles")
            if not isinstance(profiles, list):
                continue
            for profile in profiles:
                if not isinstance(profile, Mapping) or not _is_vectoree_record(profile):
                    continue
                if usable_api_key(profile.get("api_key")):
                    return _origin_from_base(profile.get("base_url")), "Vectoree"
    return None, ""


def _is_vectoree_record(record: Mapping[str, Any]) -> bool:
    if str(record.get("id") or "") == "vectoree":
        return True
    if str(record.get("source") or "") == "vectoree":
        return True
    if str(record.get("connection_id") or "") == "vectoree":
        return True
    if str(record.get("name") or "").strip().lower() == "vectoree":
        return True
    base = str(record.get("base_url") or "").lower()
    return "vectoree.ai" in base or "/vectoree" in base


def _origin_from_base(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        return
