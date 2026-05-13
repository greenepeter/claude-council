"""Debate formats — pluggable turn-taking strategies."""
from .base import BaseFormat
from .council import CouncilFormat

FORMATS = {
    "council": CouncilFormat,
}


def get_format(name: str, **kwargs) -> BaseFormat:
    if name not in FORMATS:
        raise ValueError(
            f"Unknown format '{name}'. Available: {sorted(FORMATS.keys())}\n"
            "To add a format: drop a new file in src/trade_council/formats/, "
            "subclass BaseFormat, and register it in this module's FORMATS dict."
        )
    return FORMATS[name](**kwargs)


__all__ = ["BaseFormat", "CouncilFormat", "FORMATS", "get_format"]
