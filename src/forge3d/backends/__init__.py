"""Pluggable geometry engines. See registry.py for routing."""

from .base import Backend, BackendResult
from .registry import ALL, get_backend, list_backends, resolve_auto, run

__all__ = ["ALL", "Backend", "BackendResult", "get_backend", "list_backends", "resolve_auto", "run"]
