"""Data directory used for ``.vectoree/`` on the writable DeepTutor volume."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from deeptutor.runtime.home import DEEPTUTOR_HOME_ENV, get_runtime_data_root

from .errors import VectoreeLinkError

_READ_ONLY_MESSAGE = (
    "Vectoree Link needs a writable data directory so it can save .vectoree/. "
    "Mount ./data at /app/data and link again; the image root is not a place "
    "to store the project key."
)


def resolve_project_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the data directory that should own ``.vectoree/``.

    Docker bind-mounts this at ``/app/data``. It is ``<DEEPTUTOR_HOME>/data``
    (or ``<cwd>/data`` when that variable is unset), not the package root.
    The package root is often read-only in a container, and files written
    there disappear on rebuild.
    """

    if env is None:
        return get_runtime_data_root()
    raw_home = str(env.get(DEEPTUTOR_HOME_ENV) or "").strip()
    home = Path(raw_home).expanduser().resolve() if raw_home else Path.cwd().resolve()
    return (home / "data").resolve()


def ensure_root_writable(root: Path) -> None:
    """Fail clearly when ``.vectoree/`` cannot be created on this filesystem."""

    directory = root / ".vectoree"
    probe = directory / ".write-probe"
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise VectoreeLinkError(_READ_ONLY_MESSAGE) from exc
