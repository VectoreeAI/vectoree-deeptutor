"""Vectoree link status, persistence, and a loopback mint that never leaks secrets."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import stat
from typing import Any
from urllib.parse import parse_qs, urlsplit
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers.vectoree_link import public_router, router
from deeptutor.services.config.model_catalog import ModelCatalogService
from deeptutor.services.vectoree_link.catalog import apply_vectoree_catalog
from deeptutor.services.vectoree_link.credentials import upsert_env
from deeptutor.services.vectoree_link.errors import VectoreeLinkError
from deeptutor.services.vectoree_link.linker import VectoreeLinker, reset_linker
from deeptutor.services.vectoree_link.root import resolve_project_root

PROJECT_ID = "11111111-2222-4333-8444-555555555555"
API_KEY = "sk-ve-v1-test-secret"
ACCESS_TOKEN = "eyJhbGciOiJub25lIn0.payload.signature"


def _catalog(tmp_path: Path) -> ModelCatalogService:
    return ModelCatalogService(path=tmp_path / "model_catalog.json")


def _deliver(
    linker: VectoreeLinker,
    authorize_url: str,
    *,
    code: str = "auth-code",
    state: str | None = None,
) -> None:
    query = parse_qs(urlsplit(authorize_url).query)
    redirect = query["redirect_uri"][0]
    assert redirect.endswith("/api/vectoree/link/callback")
    linker.accept_callback(
        code=code,
        state=state if state is not None else query["state"][0],
        error=None,
    )


def test_upsert_env_keeps_comments_and_other_keys() -> None:
    updated = upsert_env(
        "# keep me\nOTHER=1\nVECTOREE_API_KEY=old\n",
        {
            "VECTOREE_API_URL": "https://vectoree.ai",
            "VECTOREE_API_KEY": API_KEY,
            "VECTOREE_API_BASE": "https://vectoree.ai/api/v1",
        },
    )

    assert updated.startswith("# keep me\nOTHER=1\n")
    assert f"VECTOREE_API_KEY={API_KEY}\n" in updated
    assert "VECTOREE_API_BASE=https://vectoree.ai/api/v1\n" in updated


def test_unlinked_until_a_real_key_exists(tmp_path: Path) -> None:
    service = _catalog(tmp_path)
    linker = VectoreeLinker(root=tmp_path, env={}, catalog=service, open_url=lambda _url: None)

    assert linker.status() == {"linked": False}

    (tmp_path / ".vectoree").mkdir()
    (tmp_path / ".vectoree" / "config.json").write_text(
        json.dumps({"apiKey": "***", "apiUrl": "https://vectoree.ai"}),
        encoding="utf-8",
    )
    assert linker.status() == {"linked": False}

    env = {"VECTOREE_API_KEY": API_KEY, "VECTOREE_API_URL": "https://vectoree.ai"}
    linked = VectoreeLinker(
        root=tmp_path / "env-only", env=env, catalog=service, open_url=lambda _url: None
    )
    assert linked.status() == {"linked": True, "apiUrl": "https://vectoree.ai"}


def test_catalog_key_counts_as_linked_without_vectoree_dir(tmp_path: Path) -> None:
    service = _catalog(tmp_path)
    apply_vectoree_catalog(
        service,
        api_url="https://vectoree.ai",
        api_key=API_KEY,
        models=[{"id": "vectoree-model-auto", "name": "vectoree/auto", "model": "vectoree/auto"}],
    )
    linker = VectoreeLinker(
        root=tmp_path / "no-dot-vectoree",
        env={},
        catalog=service,
        open_url=lambda _url: None,
    )

    assert linker.status()["linked"] is True
    assert API_KEY not in json.dumps(linker.status())
    assert API_KEY not in json.dumps(linker.snapshot())


def test_existing_task_profile_is_left_in_place(tmp_path: Path) -> None:
    service = _catalog(tmp_path)
    catalog = service.load()
    catalog["services"]["task"]["profiles"] = [
        {
            "id": "task-existing",
            "name": "Existing",
            "binding": "openai",
            "models": [{"id": "task-model", "name": "Mini", "model": "gpt-4o-mini"}],
        }
    ]
    catalog["services"]["task"]["active_profile_id"] = "task-existing"
    catalog["services"]["task"]["active_model_id"] = "task-model"
    service.save(catalog)

    apply_vectoree_catalog(
        service,
        api_url="https://vectoree.ai",
        api_key=API_KEY,
        models=[{"id": "vectoree-model-auto", "name": "vectoree/auto", "model": "vectoree/auto"}],
    )
    saved = service.load()

    assert saved["services"]["task"]["active_profile_id"] == "task-existing"
    profile = saved["services"]["llm"]["profiles"][0]
    assert profile["connection_id"] == "vectoree"
    assert profile["binding"] == "openai"
    assert profile["api_key"] == API_KEY
    assert profile["base_url"] == "https://vectoree.ai/api/v1"
    assert saved["services"]["llm"]["active_model_id"] == "vectoree-model-auto"


def test_project_root_honors_deeptutor_home(tmp_path: Path) -> None:
    assert resolve_project_root({"DEEPTUTOR_HOME": str(tmp_path)}) == (tmp_path / "data").resolve()


def test_project_root_defaults_to_cwd_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPTUTOR_HOME", raising=False)
    assert resolve_project_root() == (tmp_path / "data").resolve()


def test_read_only_root_is_a_clear_error(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    frozen.chmod(0o555)
    linker = VectoreeLinker(
        root=frozen,
        env={},
        catalog=_catalog(tmp_path),
        open_url=lambda _url: None,
    )
    try:
        with pytest.raises(VectoreeLinkError, match="writable"):
            linker.start(api_url="https://vectoree.ai", project_id=PROJECT_ID)
    finally:
        frozen.chmod(0o755)


def test_start_mints_a_key_and_poll_hides_it(tmp_path: Path) -> None:
    env_path = tmp_path / ".vectoree" / "env"
    env_path.parent.mkdir()
    env_path.write_text("# keep me\nOTHER=1\n", encoding="utf-8")
    seen: list[tuple[str, str, Mapping[str, str], Mapping[str, Any] | None]] = []

    def request_json(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[int, Any]:
        seen.append((method, url, dict(headers), body))
        if url.endswith("/cli/token"):
            return 200, {"accessToken": ACCESS_TOKEN, "refreshToken": "refresh-secret"}
        if url.endswith("/api/projects"):
            return 200, [{"id": PROJECT_ID, "name": "Demo Project"}]
        if url.endswith("/api/gateway/keys"):
            return 200, {"apiKey": API_KEY, "id": "key-1"}
        if url.endswith("/api/v1/models"):
            return 200, {
                "data": [
                    {
                        "id": "vectoree/text",
                        "name": "Text",
                        "inputModality": ["text"],
                        "outputModality": ["text"],
                    },
                    {
                        "id": "vectoree/vision",
                        "name": "Vision",
                        "inputModality": ["text", "image"],
                        "outputModality": ["text"],
                    },
                ]
            }
        raise AssertionError(url)

    service = _catalog(tmp_path)
    process_env: dict[str, str] = {}
    linker = VectoreeLinker(
        root=tmp_path,
        env=process_env,
        catalog=service,
        request_json=request_json,
        open_url=lambda _url: None,
        timeout_s=5,
    )
    pending = linker.start(
        api_url="https://vectoree.ai/api/v1",
        project_id=f"https://vectoree.ai/dashboard/projects/{PROJECT_ID}",
        public_origin="http://127.0.0.1:3782",
    )
    assert pending["status"] == "pending"
    assert "code_challenge_method=S256" in pending["authorizeUrl"]
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A3782%2Fapi%2Fvectoree%2Flink%2Fcallback" in (
        pending["authorizeUrl"]
    )
    assert API_KEY not in json.dumps(pending)
    _deliver(linker, pending["authorizeUrl"])
    done = linker.snapshot()

    assert done == {
        "status": "linked",
        "apiUrl": "https://vectoree.ai",
        "projectId": PROJECT_ID,
        "projectName": "Demo Project",
    }
    public = json.dumps({"status": linker.status(), "poll": done})
    assert API_KEY not in public
    assert ACCESS_TOKEN not in public
    assert "refresh-secret" not in public

    config = json.loads((tmp_path / ".vectoree" / "config.json").read_text(encoding="utf-8"))
    assert config["apiKey"] == API_KEY
    assert config["accessToken"] == ACCESS_TOKEN
    assert config["projectName"] == "Demo Project"
    assert (tmp_path / ".vectoree" / "device-id").read_text(encoding="utf-8").strip()
    config_mode = stat.S_IMODE((tmp_path / ".vectoree" / "config.json").stat().st_mode)
    dir_mode = stat.S_IMODE((tmp_path / ".vectoree").stat().st_mode)
    assert config_mode == 0o600
    assert dir_mode == 0o700

    env_text = env_path.read_text(encoding="utf-8")
    assert env_text.startswith("# keep me\nOTHER=1\n")
    assert f"VECTOREE_API_KEY={API_KEY}" in env_text
    assert "VECTOREE_API_BASE=https://vectoree.ai/api/v1" in env_text
    assert not (tmp_path / ".env").exists()
    assert process_env["VECTOREE_API_KEY"] == API_KEY

    saved = service.load()
    assert saved["connections"][0]["id"] == "vectoree"
    assert saved["connections"][0]["provider"] == "openai"
    active_id = saved["services"]["llm"]["active_model_id"]
    models = saved["services"]["llm"]["profiles"][0]["models"]
    active = next(item for item in models if item["id"] == active_id)
    assert active["model"] == "vectoree/auto"
    assert any(item["model"] == "vectoree/vision" for item in models)
    assert saved["services"]["task"]["profiles"][0]["connection_id"] == "vectoree"

    key_call = next(item for item in seen if item[1].endswith("/api/gateway/keys"))
    assert key_call[2]["User-Agent"] == "VectoreeCLI"
    assert key_call[2]["X-Project-Id"] == PROJECT_ID
    assert key_call[3] is not None
    assert key_call[3]["scopes"] == [
        "gateway:chat",
        "gateway:models",
        "tools:*",
        "database:*",
        "storage:*",
        "auth:*",
    ]


def test_upstream_error_text_is_redacted(tmp_path: Path) -> None:
    def request_json(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[int, Any]:
        return 400, {"message": f"rejected {API_KEY}"}

    linker = VectoreeLinker(
        root=tmp_path,
        env={},
        catalog=_catalog(tmp_path),
        request_json=request_json,
        open_url=lambda _url: None,
        timeout_s=5,
    )
    pending = linker.start(
        api_url="https://vectoree.ai",
        project_id=PROJECT_ID,
        public_origin="http://localhost:3782",
    )
    with pytest.raises(VectoreeLinkError, match="redacted"):
        _deliver(linker, pending["authorizeUrl"])
    done = linker.snapshot()

    assert done["status"] == "error"
    assert API_KEY not in json.dumps(done)
    assert "[redacted]" in done["message"]


def test_http_status_and_poll_never_return_the_key(tmp_path: Path) -> None:
    service = _catalog(tmp_path)
    (tmp_path / ".vectoree").mkdir()
    (tmp_path / ".vectoree" / "config.json").write_text(
        json.dumps(
            {
                "apiUrl": "https://vectoree.ai",
                "apiKey": API_KEY,
                "accessToken": ACCESS_TOKEN,
                "projectName": "Demo Project",
            }
        ),
        encoding="utf-8",
    )
    linker = VectoreeLinker(root=tmp_path, env={}, catalog=service, open_url=lambda _url: None)
    reset_linker(linker)
    app = FastAPI()
    app.include_router(public_router, prefix="/api/vectoree")
    app.include_router(router, prefix="/api/vectoree")
    try:
        client = TestClient(app)
        status = client.get("/api/vectoree/link-status")
        poll = client.get("/api/vectoree/link")
        rejected = client.post("/api/vectoree/link/start", json={"projectId": "not-a-uuid"})
    finally:
        reset_linker(None)

    assert status.status_code == 200
    assert status.json() == {
        "linked": True,
        "projectName": "Demo Project",
        "apiUrl": "https://vectoree.ai",
    }
    assert poll.json()["status"] == "linked"
    body = status.text + poll.text + rejected.text
    assert API_KEY not in body
    assert ACCESS_TOKEN not in body
    assert rejected.status_code == 400


def test_empty_catalog_placeholder_stays_unlinked(tmp_path: Path) -> None:
    service = _catalog(tmp_path)
    linker = VectoreeLinker(root=tmp_path, env={}, catalog=service, open_url=lambda _url: None)

    assert linker.status() == {"linked": False}
    saved = service.load()
    assert saved["connections"][0]["base_url"] == "https://vectoree.ai/api/v1"
    assert saved["connections"][0]["api_key"] == ""
    assert saved["services"]["llm"]["profiles"][0]["models"][0]["model"] == "vectoree/auto"
    assert API_KEY not in json.dumps(linker.status())


def test_remote_callback_origin_is_rejected(tmp_path: Path) -> None:
    linker = VectoreeLinker(
        root=tmp_path,
        env={},
        catalog=_catalog(tmp_path),
        open_url=lambda _url: None,
    )
    with pytest.raises(VectoreeLinkError, match="localhost"):
        linker.start(
            api_url="https://vectoree.ai",
            project_id=PROJECT_ID,
            public_origin="https://evil.example",
        )


def test_published_callback_route_mints_on_the_host_port(tmp_path: Path) -> None:
    def request_json(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[int, Any]:
        if url.endswith("/cli/token"):
            return 200, {"accessToken": ACCESS_TOKEN, "refreshToken": "refresh-secret"}
        if url.endswith("/api/projects"):
            return 200, [{"id": PROJECT_ID, "name": "Demo Project"}]
        if url.endswith("/api/gateway/keys"):
            return 200, {"apiKey": API_KEY, "id": "key-1"}
        if url.endswith("/api/v1/models"):
            return 200, {"data": []}
        raise AssertionError(url)

    linker = VectoreeLinker(
        root=tmp_path,
        env={},
        catalog=_catalog(tmp_path),
        request_json=request_json,
        open_url=lambda _url: None,
    )
    reset_linker(linker)
    app = FastAPI()
    app.include_router(public_router, prefix="/api/vectoree")
    app.include_router(router, prefix="/api/vectoree")
    try:
        client = TestClient(app)
        started = client.post(
            "/api/vectoree/link/start",
            json={
                "apiUrl": "https://vectoree.ai",
                "projectId": PROJECT_ID,
                "publicOrigin": "http://127.0.0.1:3782",
            },
        )
        assert started.status_code == 200
        query = parse_qs(urlsplit(started.json()["authorizeUrl"]).query)
        assert query["redirect_uri"] == ["http://127.0.0.1:3782/api/vectoree/link/callback"]
        rejected = client.get(
            "/api/vectoree/link/callback",
            params={"code": "auth-code", "state": "0" * 32},
        )
        assert rejected.status_code == 400
        assert API_KEY not in rejected.text
        completed = client.get(
            "/api/vectoree/link/callback",
            params={"code": "auth-code", "state": query["state"][0]},
        )
    finally:
        reset_linker(None)

    assert completed.status_code == 200
    assert API_KEY not in completed.text
    assert ACCESS_TOKEN not in completed.text
    config = json.loads((tmp_path / ".vectoree" / "config.json").read_text(encoding="utf-8"))
    assert config["apiKey"] == API_KEY
    assert not (tmp_path / ".env").exists()
    assert API_KEY in (tmp_path / ".vectoree" / "env").read_text(encoding="utf-8")
    catalog = json.loads((tmp_path / "model_catalog.json").read_text(encoding="utf-8"))
    assert catalog["services"]["llm"]["profiles"][0]["models"][0]["model"] == "vectoree/auto"
    progress = json.loads(
        (tmp_path / ".vectoree" / "link-progress.json").read_text(encoding="utf-8")
    )
    assert API_KEY not in json.dumps(progress)
    assert "verifier" not in progress
    assert not (tmp_path / ".vectoree" / "link-attempt.json").exists()


def test_another_worker_can_finish_the_callback(tmp_path: Path) -> None:
    def request_json(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> tuple[int, Any]:
        if url.endswith("/cli/token"):
            return 200, {"accessToken": ACCESS_TOKEN, "refreshToken": "refresh-secret"}
        if url.endswith("/api/projects"):
            return 200, [{"id": PROJECT_ID, "name": "Demo Project"}]
        if url.endswith("/api/gateway/keys"):
            return 200, {"apiKey": API_KEY, "id": "key-1"}
        if url.endswith("/api/v1/models"):
            return 200, {"data": []}
        raise AssertionError(url)

    catalog = _catalog(tmp_path)
    env: dict[str, str] = {}
    starter = VectoreeLinker(
        root=tmp_path,
        env=env,
        catalog=catalog,
        request_json=request_json,
        open_url=lambda _url: None,
    )
    pending = starter.start(
        api_url="https://vectoree.ai",
        project_id=PROJECT_ID,
        public_origin="http://127.0.0.1:3782",
    )
    finisher = VectoreeLinker(
        root=tmp_path,
        env=env,
        catalog=catalog,
        request_json=request_json,
        open_url=lambda _url: None,
    )
    _deliver(finisher, pending["authorizeUrl"])

    assert finisher.snapshot()["status"] == "linked"
    assert starter.snapshot()["projectName"] == "Demo Project"
    assert "verifier" not in json.dumps(starter.snapshot())
