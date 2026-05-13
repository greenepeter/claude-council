"""Paper-trading registry — first-class record of plans enrolled for paper trading.

Every plan YAML the debate orchestrator emits is enrolled here. The autonomous
runner iterates this list rather than globbing `trade_plans/*.yaml`, which lets
the registry mark plans as expired/invalidated/closed so they stop consuming
poll cycles.

Design constraints (avoiding mistakes from the prior MT5-coupled paper trader):
  * Strategy-agnostic. No fields tied to any one strategy or color.
  * No silent None states. `save()` always writes a complete document.
  * Idempotent. Re-enrolling the same plan_id updates the row in place.
  * Robust to partial writes (tmp-then-replace).
  * Never raises on a missing/corrupt file — degrades to an empty registry
    with a one-shot warning so the debate flow is never blocked.

Persisted shape (`runtime/paper_trading_registry.json`):
{
  "version": 1,
  "updated_at": "2026-05-11T18:22:00+00:00",
  "entries": {
    "<plan_id>": {
      "plan_id": "...",
      "plan_path": "trade_plans/eurgbp_2026_05_swing_short.yaml",
      "symbol": "EURGBP",
      "status": "active",        # active | expired | invalidated | closed | errored
      "enrolled_at": "...",
      "last_cycle_at": "...",
      "last_cycle_summary": "spot=0.86523 state=active gates=clear",
      "last_error": null,
      "debate_ref": "debates/2026-05-11_..."
    },
    ...
  }
}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_council.paper_trader.plan import load_plan


REGISTRY_VERSION = 1
ACTIVE_STATUSES = {"active"}
TERMINAL_STATUSES = {"expired", "invalidated", "closed"}


@dataclass
class RegistryEntry:
    plan_id: str
    plan_path: str                          # stored as POSIX-relative-to-project-root when possible
    symbol: str
    status: str                             # active | expired | invalidated | closed | errored
    enrolled_at: str
    debate_ref: str = ""
    last_cycle_at: str = ""
    last_cycle_summary: str = ""
    last_error: str | None = None
    horizon_days: int = 0
    direction: str = ""


class PaperTradingRegistry:
    """JSON-backed record of which plans are enrolled for paper trading.

    Methods that mutate state always write the full file atomically.
    """

    def __init__(self, registry_path: Path, project_root: Path | None = None):
        self.path = Path(registry_path)
        self.project_root = Path(project_root) if project_root else self.path.parent.parent
        self._entries: dict[str, RegistryEntry] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # Don't block the caller on a corrupt file; rename and start fresh.
            backup = self.path.with_suffix(self.path.suffix + f".corrupt-{int(datetime.now().timestamp())}")
            try:
                self.path.rename(backup)
            except OSError:
                pass
            print(f"[paper_trading_registry] could not parse {self.path} ({e}); "
                  f"backed up to {backup}; starting empty.")
            self._entries = {}
            return

        entries_raw = raw.get("entries") or {}
        out: dict[str, RegistryEntry] = {}
        for plan_id, row in entries_raw.items():
            # Tolerate missing optional fields rather than crashing on stale schemas.
            row.setdefault("debate_ref", "")
            row.setdefault("last_cycle_at", "")
            row.setdefault("last_cycle_summary", "")
            row.setdefault("last_error", None)
            row.setdefault("horizon_days", 0)
            row.setdefault("direction", "")
            try:
                out[plan_id] = RegistryEntry(**row)
            except TypeError:
                # Forward-incompatible row: keep going, log, skip the row.
                print(f"[paper_trading_registry] skipping unparseable row {plan_id!r}")
        self._entries = out

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": REGISTRY_VERSION,
            "updated_at": _utc_now(),
            "entries": {pid: asdict(e) for pid, e in self._entries.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ── public API ─────────────────────────────────────────────────────────

    def enroll(
        self,
        plan_path: Path,
        debate_ref: str = "",
    ) -> RegistryEntry:
        """Enroll a plan YAML for paper trading.

        Idempotent: re-enrolling the same plan_id updates the row in place and
        keeps its current status unless it was in a terminal state, in which
        case it is reactivated. Caller decides whether to immediately run a
        cycle on the returned entry.
        """
        plan_path = Path(plan_path).resolve()
        plan = load_plan(plan_path)

        rel = _try_relative(plan_path, self.project_root)
        existing = self._entries.get(plan.plan_id)
        now = _utc_now()

        if existing is None:
            entry = RegistryEntry(
                plan_id=plan.plan_id,
                plan_path=rel,
                symbol=plan.symbol,
                status="active",
                enrolled_at=now,
                debate_ref=debate_ref,
                horizon_days=plan.horizon_days,
                direction=plan.direction,
            )
        else:
            entry = existing
            # Re-enrollment: reactivate from terminal states, refresh metadata.
            entry.plan_path = rel
            entry.symbol = plan.symbol
            entry.horizon_days = plan.horizon_days
            entry.direction = plan.direction
            if debate_ref:
                entry.debate_ref = debate_ref
            if entry.status in TERMINAL_STATUSES or entry.status == "errored":
                entry.status = "active"
                entry.enrolled_at = now
                entry.last_error = None

        self._entries[entry.plan_id] = entry
        self._save()
        return entry

    def list_active(self) -> list[RegistryEntry]:
        return [e for e in self._entries.values() if e.status in ACTIVE_STATUSES]

    def list_all(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def get(self, plan_id: str) -> RegistryEntry | None:
        return self._entries.get(plan_id)

    def set_status(self, plan_id: str, status: str, *, error: str | None = None) -> None:
        entry = self._entries.get(plan_id)
        if entry is None:
            return
        entry.status = status
        if error is not None:
            entry.last_error = error
        elif status in ACTIVE_STATUSES:
            entry.last_error = None
        self._save()

    def record_cycle(self, plan_id: str, summary: str) -> None:
        """Stamp the most recent cycle's summary onto the entry."""
        entry = self._entries.get(plan_id)
        if entry is None:
            return
        entry.last_cycle_at = _utc_now()
        entry.last_cycle_summary = summary
        # Clear stale error so a successful cycle isn't shadowed by an old failure.
        if entry.status == "active":
            entry.last_error = None
        self._save()

    def record_error(self, plan_id: str, message: str) -> None:
        entry = self._entries.get(plan_id)
        if entry is None:
            return
        entry.last_error = message[:1000]
        # An errored cycle doesn't auto-terminate the plan — operator decides.
        # We DO leave status alone so the next poll retries.
        self._save()

    def resolve_plan_path(self, entry: RegistryEntry) -> Path:
        """Resolve the entry's plan_path against the project root if it's relative."""
        p = Path(entry.plan_path)
        return p if p.is_absolute() else (self.project_root / p)


# ── helpers ────────────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _try_relative(path: Path, root: Path) -> str:
    """Return the POSIX-style path relative to root if possible, else absolute."""
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def default_registry_path(project_root: Path) -> Path:
    return Path(project_root) / "runtime" / "paper_trading_registry.json"


def summarize_cycle(cycle: Any) -> str:
    """One-line summary of a paper_trader Cycle for the registry."""
    spot = f"{cycle.spot.price:.5f}" if cycle.spot is not None else "n/a"
    state = cycle.plan_state
    pos = "flat"
    if cycle.position_snapshot is not None:
        p = cycle.position_snapshot
        pos = f"{p.direction} {p.symbol}@{p.entry_price:.5f} frac={p.open_fraction:.2f}"
    actions = ",".join(a.kind for a in cycle.actions) or "noop"
    # Surface calendar status so a dead feed (event_gates blind) is visible
    # in `show_trades.py`'s last-summary column rather than buried in notes.
    cal = "ok" if getattr(cycle, "calendar_ok", True) else "DEAD"
    return f"spot={spot} state={state} pos={pos} actions={actions} cal={cal}"
