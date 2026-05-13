"""Specialist — a single Claude with a custom role, invoked through a Backend."""
from __future__ import annotations
from .config import SpecialistConfig
from .backends import get_backend, BackendResponse


class Specialist:
    def __init__(self, config: SpecialistConfig):
        self.config = config
        self.backend = get_backend(config.mode)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def expertise(self) -> str:
        return self.config.expertise

    def respond(self, user_prompt: str) -> BackendResponse:
        return self.backend.call(
            system_prompt=self.config.system_prompt,
            user_prompt=user_prompt,
            model=self.config.model,
            tools_allowed=self.config.tools_allowed,
        )
