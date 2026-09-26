"""Project root used for ``.vectoree/`` and the optional ``.env`` upsert."""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

from deeptutor.runtime.home import DEEPTUTOR_HOME_ENV, PACKAGE_ROOT

from .errors import VectoreeLinkError

_READ_ONLY_MESSAGE = (
    "Vectoree Link needs a writable project root so it can save .vectoree/. "
    "A read-only Docker filesystem cannot store that directory; link from a "
    "writable checkout instead."
)


def resolve_project_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the directory that should own ``.vectoree/``.

    ``DEEPTUTOR_HOME`` wins when it is set, matching the runtime home DeepTutor
    already uses for a launch. Otherwise walk upward from the working directory
    for a checkout that contains both ``pyproject.toml`` and ``deeptutor/``.
    """

    source = os.environ if env is None else env
    raw_home = str(source.get(DEEPTUTOR_HOME_ENV) or "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve()

    start = Path.cwd().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "deeptutor").is_dir():
            return candidate
    if (PACKAGE_ROOT / "pyproject.toml").is_file() and (PACKAGE_ROOT / "deeptutor").is_dir():
        return PACKAGE_ROOT
    return start


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
