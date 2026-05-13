"""Format abstract base class."""
from __future__ import annotations
from abc import ABC, abstractmethod
from ..transcript import Transcript


class BaseFormat(ABC):
    """A debate format owns the turn-taking loop.

    Subclasses receive the specialists list, the moderator, and a Transcript,
    and mutate the transcript with turns and decisions until the debate ends.
    """

    name: str = "base"

    @abstractmethod
    def run(self, transcript: Transcript, specialists: list, moderator) -> None:
        ...
