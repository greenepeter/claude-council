"""Trade plan model + YAML loader.

A `Plan` is the machine-readable form of a moderator's committed decisions.
Hand-edited in `trade_plans/*.yaml`. The engine reads from this, never from
the prose `decisions.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class Entry:
    id: str
    description: str
    type: str                               # "limit_zone" | "breakout_retest"
    weight: float
    # limit_zone fields:
    zone: tuple[float, float] | None = None
    stop: float | None = None
    # breakout_retest fields:
    breakout_level: float | None = None
    retest_tolerance_pips: int | None = None
    stop_on_daily_close_below: float | None = None


@dataclass
class TargetOnFill:
    trail_stop_to: str | float | None = None  # "entry" sentinel or absolute level


@dataclass
class Target:
    id: str
    level: float
    fraction: float
    on_fill: TargetOnFill = field(default_factory=TargetOnFill)


@dataclass
class Sizing:
    standalone_risk_pct: float
    correlated_risk_pct: float
    correlated_symbols: list[str]
    no_pyramiding: bool = True


@dataclass
class EventGate:
    title_contains: str
    country: str
    impact: str
    block_before_hours: float
    block_after_hours: float
    behavior: str
    note: str = ""


@dataclass
class InvalidationRule:
    condition: str                          # "daily_close_below" | "plan_age_days"
    level: float
    note: str = ""


@dataclass
class Account:
    size_usd: float
    base_currency: str = "USD"


@dataclass
class Plan:
    plan_id: str
    created: date
    debate_ref: str
    symbol: str
    direction: str                          # "long" | "short"
    horizon_days: int
    confidence: str                         # "A" | "B" | "C"
    account: Account
    entries: list[Entry]
    targets: list[Target]
    sizing: Sizing
    event_gates: list[EventGate]
    invalidation: list[InvalidationRule]

    # ── Convenience ────────────────────────────────────────────────────────

    @property
    def yfinance_ticker(self) -> str:
        """Map our symbol to a yfinance ticker."""
        s = self.symbol.upper()
        if s.endswith("=X"):
            return s
        # FX majors → "{PAIR}=X"
        return f"{s}=X"

    @property
    def is_expired(self) -> bool:
        age = (datetime.now(timezone.utc).date() - self.created).days
        return age >= self.horizon_days


# ── Loader ──────────────────────────────────────────────────────────────────


def load_plan(path: str | Path) -> Plan:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    entries = [_parse_entry(e) for e in raw["entries"]]
    targets = [_parse_target(t) for t in raw["targets"]]
    sizing = Sizing(**raw["sizing"])
    event_gates = [EventGate(**g) for g in raw.get("event_gates", [])]
    invalidation = [InvalidationRule(**r) for r in raw.get("invalidation", [])]
    account = Account(**raw["account"])

    created = raw["created"]
    if isinstance(created, str):
        created = date.fromisoformat(created)

    return Plan(
        plan_id=raw["plan_id"],
        created=created,
        debate_ref=raw.get("debate_ref", ""),
        symbol=raw["symbol"],
        direction=raw["direction"],
        horizon_days=int(raw["horizon_days"]),
        confidence=raw.get("confidence", "B"),
        account=account,
        entries=entries,
        targets=targets,
        sizing=sizing,
        event_gates=event_gates,
        invalidation=invalidation,
    )


def _parse_entry(raw: dict[str, Any]) -> Entry:
    zone = raw.get("zone")
    if zone is not None:
        zone = (float(zone[0]), float(zone[1]))
    return Entry(
        id=raw["id"],
        description=raw.get("description", ""),
        type=raw["type"],
        weight=float(raw.get("weight", 1.0)),
        zone=zone,
        stop=raw.get("stop"),
        breakout_level=raw.get("breakout_level"),
        retest_tolerance_pips=raw.get("retest_tolerance_pips"),
        stop_on_daily_close_below=raw.get("stop_on_daily_close_below"),
    )


def _parse_target(raw: dict[str, Any]) -> Target:
    on_fill_raw = raw.get("on_fill", {}) or {}
    return Target(
        id=raw["id"],
        level=float(raw["level"]),
        fraction=float(raw["fraction"]),
        on_fill=TargetOnFill(trail_stop_to=on_fill_raw.get("trail_stop_to")),
    )
