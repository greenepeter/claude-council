"""Backend abstract base class."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class BackendResponse:
    text: str
    cost_usd: float = 0.0
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)


class Backend(ABC):
    @abstractmethod
    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "sonnet",
        tools_allowed: list[str] | None = None,
    ) -> BackendResponse:
        """Invoke Claude with the given prompts; return a BackendResponse.

        tools_allowed: list of Claude Code tool names (e.g. ['WebSearch','WebFetch'])
            that this call is permitted to use. None or [] means no tools — pure
            reasoning. Allowing tools also implicitly allows multi-turn tool-use
            round-trips inside the single backend call.
        """
        ...
