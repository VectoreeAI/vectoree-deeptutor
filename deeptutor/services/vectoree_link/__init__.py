"""Vectoree Console link: PKCE, project key, and model-catalog wiring."""

from .errors import VectoreeLinkError
from .linker import get_linker, reset_linker

__all__ = ["VectoreeLinkError", "get_linker", "reset_linker"]
