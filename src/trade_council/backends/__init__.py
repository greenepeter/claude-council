"""Backends — abstractions over how a Claude turn is invoked (subprocess vs API)."""
from .base import Backend, BackendResponse
from .claude_code import ClaudeCodeBackend
from .api import APIBackend


def get_backend(mode: str) -> Backend:
    if mode == "claude_code":
        return ClaudeCodeBackend()
    if mode == "api":
        return APIBackend()
    raise ValueError(
        f"Unknown backend mode: '{mode}'. Valid modes: claude_code, api."
    )


__all__ = ["Backend", "BackendResponse", "ClaudeCodeBackend", "APIBackend", "get_backend"]
