"""Point the model catalog at a minted Vectoree project key."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from deeptutor.services.config.model_catalog import ModelCatalogService

CONNECTION_ID = "vectoree"
LLM_PROFILE_ID = "vectoree-llm"
TASK_PROFILE_ID = "vectoree-task"
FALLBACK_MODEL = "vectoree/auto"
_MODEL_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ListedModel:
    slug: str
    name: str
    vision: bool


def parse_model_list(payload: object) -> list[ListedModel]:
    """Read ``GET /api/v1/models``. Unknown shapes yield an empty list."""

    data: object
    if isinstance(payload, list):
        data = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("data"), list):
        data = payload["data"]
    else:
        return []
    models: list[ListedModel] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        slug = item.get("id")
        if not isinstance(slug, str) or not slug.strip():
            continue
        name = item.get("name")
        display = item.get("displayName")
        label = slug.strip()
        if isinstance(name, str) and name.strip():
            label = name.strip()
        elif isinstance(display, str) and display.strip():
            label = display.strip()
        models.append(ListedModel(slug=slug.strip(), name=label, vision=_is_vision(item)))
    return models


def pick_default_slug(models: list[ListedModel]) -> str:
    """Prefer ``vectoree/auto``, then a vision+text model, then the first id."""

    for model in models:
        if model.slug == FALLBACK_MODEL:
            return model.slug
    for model in models:
        if model.vision:
            return model.slug
    if models:
        return models[0].slug
    return FALLBACK_MODEL


def catalog_model_entries(models: list[ListedModel]) -> list[dict[str, str]]:
    listed = list(models)
    if not any(model.slug == FALLBACK_MODEL for model in listed):
        listed.insert(0, ListedModel(slug=FALLBACK_MODEL, name=FALLBACK_MODEL, vision=False))
    chosen = pick_default_slug(listed)
    models = listed
    ordered = sorted(models, key=lambda model: 0 if model.slug == chosen else 1)
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for model in ordered[:40]:
        entry_id = _model_id(model.slug)
        if entry_id in seen:
            continue
        seen.add(entry_id)
        entries.append({"id": entry_id, "name": model.name, "model": model.slug})
    return entries


def ensure_vectoree_placeholder(service: ModelCatalogService) -> None:
    """Seed an empty LLM catalog with Vectoree and ``vectoree/auto``.

    No API key is stored, so an install still counts as unlinked until the
    user finishes ``/link``.
    """

    try:
        loaded = service.load()
    except Exception:
        return
    services = loaded.get("services") if isinstance(loaded, dict) else None
    llm = services.get("llm") if isinstance(services, dict) else None
    if not _service_empty(llm):
        return

    def mutate(catalog: dict[str, Any]) -> None:
        if not _service_empty((catalog.get("services") or {}).get("llm")):
            return
        connections = catalog.get("connections")
        if not isinstance(connections, list):
            connections = []
            catalog["connections"] = connections
        if not any(
            isinstance(item, dict) and item.get("id") == CONNECTION_ID for item in connections
        ):
            connections.append(
                {
                    "id": CONNECTION_ID,
                    "name": "Vectoree",
                    "provider": "openai",
                    "api_key": "",
                    "base_url": "https://vectoree.ai/api/v1",
                    "api_version": "",
                    "extra_headers": {},
                    "source": "vectoree",
                }
            )
        _ensure_profile(
            catalog,
            "llm",
            LLM_PROFILE_ID,
            [
                {
                    "id": "vectoree-model-auto",
                    "name": FALLBACK_MODEL,
                    "model": FALLBACK_MODEL,
                }
            ],
        )

    service.update(mutate)


def apply_vectoree_catalog(
    service: ModelCatalogService,
    *,
    api_url: str,
    api_key: str,
    models: list[dict[str, str]],
) -> None:
    """Upsert the Vectoree connection and make it the active LLM."""

    base_url = f"{api_url.rstrip('/')}/api/v1"

    def mutate(catalog: dict[str, Any]) -> None:
        connections = catalog.get("connections")
        if not isinstance(connections, list):
            connections = []
            catalog["connections"] = connections
        connection = next(
            (
                item
                for item in connections
                if isinstance(item, dict) and item.get("id") == CONNECTION_ID
            ),
            None,
        )
        if connection is None:
            connection = {"id": CONNECTION_ID}
            connections.append(connection)
        connection.clear()
        connection.update(
            {
                "id": CONNECTION_ID,
                "name": "Vectoree",
                "provider": "openai",
                "api_key": api_key,
                "base_url": base_url,
                "api_version": "",
                "extra_headers": {},
                "source": "vectoree",
            }
        )
        _ensure_profile(catalog, "llm", LLM_PROFILE_ID, models)
        task = catalog.get("services", {}).get("task")
        if _service_empty(task):
            _ensure_profile(catalog, "task", TASK_PROFILE_ID, models)

    service.update(mutate)


def _ensure_profile(
    catalog: dict[str, Any],
    service_name: str,
    profile_id: str,
    models: list[dict[str, str]],
) -> None:
    services = catalog.setdefault("services", {})
    service = services.setdefault(service_name, {})
    if not isinstance(service, dict):
        service = {}
        services[service_name] = service
    profiles = service.setdefault("profiles", [])
    if not isinstance(profiles, list):
        profiles = []
        service["profiles"] = profiles
    profile = next(
        (
            item
            for item in profiles
            if isinstance(item, dict)
            and (item.get("id") == profile_id or item.get("connection_id") == CONNECTION_ID)
        ),
        None,
    )
    if profile is None:
        profile = {"id": profile_id}
        profiles.append(profile)
    profile_id = str(profile.get("id") or profile_id)
    profile.clear()
    profile.update(
        {
            "id": profile_id,
            "name": "Vectoree",
            "binding": "openai",
            "connection_id": CONNECTION_ID,
            "models": models,
        }
    )
    service["active_profile_id"] = profile_id
    service["active_model_id"] = models[0]["id"] if models else None


def _service_empty(service: object) -> bool:
    if not isinstance(service, Mapping):
        return True
    profiles = service.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        return True
    return not any(isinstance(profile, Mapping) and profile.get("models") for profile in profiles)


def _is_vision(item: Mapping[str, Any]) -> bool:
    inputs = _modalities(item.get("inputModality"), _architecture(item).get("input_modalities"))
    outputs = _modalities(item.get("outputModality"), _architecture(item).get("output_modalities"))
    reads_images = "image" in inputs
    writes_text = not outputs or "text" in outputs
    return reads_images and writes_text


def _architecture(item: Mapping[str, Any]) -> Mapping[str, Any]:
    architecture = item.get("architecture")
    return architecture if isinstance(architecture, Mapping) else {}


def _modalities(primary: object, fallback: object) -> list[str]:
    source = (
        primary if isinstance(primary, list) else fallback if isinstance(fallback, list) else []
    )
    return [str(entry).lower() for entry in source if isinstance(entry, str)]


def _model_id(slug: str) -> str:
    safe = _MODEL_ID_RE.sub("-", slug).strip("-")[:80] or "model"
    return f"vectoree-model-{safe}"
