"""High-level helpers that bind the registry, ledger, and engine.

`enroll_plan(plan_path, project_root, ...)` is called by the debate orchestrator
right after a plan YAML is written: it registers the plan, initializes its
ledger, and runs ONE evaluation cycle so the plan is acknowledged immediately
rather than waiting for the next autonomous-loop tick.

`run_cycle_for_entry(entry, project_root, ...)` is what the autonomous loop
calls per active entry on every tick — same engine call, but only updates the
registry (status + last_cycle_summary). It doesn't re-enroll.

Both functions are tolerant of upstream failures (network, broken plan, etc.):
they record the error onto the registry entry and return rather than raising,
so a single bad plan never halts the debate flow or the polling loop.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trade_council.paper_trader.plan import Plan, load_plan
from trade_council.paper_trader.ledger import Ledger
from trade_council.paper_trader.engine import Engine, Cycle
from trade_council.paper_trader.notify import Notifier
from trade_council.paper_trader.data.calendar import CalendarSource
from trade_council.paper_trader.data.prices import PriceSource
from trade_council.paper_trader.registry import (
    PaperTradingRegistry,
    RegistryEntry,
    default_registry_path,
    summarize_cycle,
)


def _runtime_paths(project_root: Path, plan_id: str) -> tuple[Path, Path, Path]:
    """Return (ledger_path, alerts_log_path, cache_dir) for a given plan_id."""
    runtime = project_root / "runtime"
    return (
        runtime / f"{plan_id}_ledger.json",
        runtime / f"{plan_id}_alerts.log",
        runtime / "cache",
    )


def _is_tradeable(plan: Plan) -> bool:
    """A plan with no entries (e.g. should_act=no) doesn't need cycles."""
    return bool(plan.entries) and plan.direction in ("long", "short")


def enroll_all_plans_in_dir(
    plans_dir: Path,
    project_root: Path,
    *,
    debate_ref: str = "",
) -> list[RegistryEntry]:
    """Register every *.yaml in plans_dir not already in the registry.

    Does NOT run an initial cycle — that's the autonomous loop's next tick.
    Useful for picking up plans created outside the debate flow (manual edits,
    plans that pre-date this registry, etc.). Idempotent.
    """
    project_root = Path(project_root).resolve()
    plans_dir = Path(plans_dir)
    if not plans_dir.exists():
        return []

    registry = PaperTradingRegistry(default_registry_path(project_root), project_root)
    known = {e.plan_id for e in registry.list_all()}
    added: list[RegistryEntry] = []
    for yaml_path in sorted(plans_dir.glob("*.yaml")):
        try:
            plan = load_plan(yaml_path)
        except Exception:
            continue
        if plan.plan_id in known:
            continue
        try:
            entry = registry.enroll(yaml_path, debate_ref=debate_ref)
            added.append(entry)
        except Exception:
            # Bad plan — skip rather than blocking the whole poll.
            continue
    return added


def enroll_plan(
    plan_path: Path,
    project_root: Path,
    *,
    debate_ref: str = "",
    run_initial_cycle: bool = True,
    quiet: bool = True,
    console=None,
) -> tuple[RegistryEntry, Cycle | None]:
    """Register a plan YAML for paper trading and optionally run one cycle.

    Returns (registry_entry, cycle_or_None). `cycle` is None when:
      - run_initial_cycle is False, OR
      - the plan is not tradeable (no entries / direction='none'), OR
      - the engine raised (in which case the error is recorded on the entry).
    """
    plan_path = Path(plan_path).resolve()
    project_root = Path(project_root).resolve()
    registry = PaperTradingRegistry(default_registry_path(project_root), project_root)

    entry = registry.enroll(plan_path, debate_ref=debate_ref)

    if not run_initial_cycle:
        return entry, None

    plan = load_plan(plan_path)

    # Skip engine work on non-tradeable plans, but mark them so the operator
    # sees what happened. They stay 'active' in case they're later edited;
    # the autonomous loop will just no-op on them too.
    if not _is_tradeable(plan):
        msg = (
            f"not tradeable (direction={plan.direction!r}, entries={len(plan.entries)}); "
            "skipping initial cycle"
        )
        registry.record_cycle(plan.plan_id, msg)
        return entry, None

    cycle = _safe_evaluate(plan, project_root, registry, quiet=quiet, console=console)
    if cycle is None:
        return entry, None

    registry.record_cycle(plan.plan_id, summarize_cycle(cycle))
    _sync_status_from_cycle(registry, plan.plan_id, cycle)
    return entry, cycle


def run_cycle_for_entry(
    entry: RegistryEntry,
    project_root: Path,
    *,
    quiet: bool = True,
    console=None,
) -> Cycle | None:
    """Evaluate one cycle for a registered, active plan.

    Called by the autonomous loop. Updates the registry's last_cycle and may
    transition the plan to expired/invalidated/closed based on the engine's
    output. Returns the Cycle, or None on a hard load/eval failure.
    """
    project_root = Path(project_root).resolve()
    registry = PaperTradingRegistry(default_registry_path(project_root), project_root)
    plan_path = registry.resolve_plan_path(entry)

    if not plan_path.exists():
        registry.set_status(
            entry.plan_id, "invalidated",
            error=f"plan YAML missing on disk: {plan_path}",
        )
        return None

    try:
        plan = load_plan(plan_path)
    except Exception as e:
        registry.record_error(entry.plan_id, f"load_plan failed: {type(e).__name__}: {e}")
        return None

    if not _is_tradeable(plan):
        # Idempotent no-op: still stamp a cycle so the operator sees the loop ran.
        registry.record_cycle(
            plan.plan_id,
            f"not tradeable (direction={plan.direction!r}, entries={len(plan.entries)})",
        )
        return None

    cycle = _safe_evaluate(plan, project_root, registry, quiet=quiet, console=console)
    if cycle is None:
        return None

    registry.record_cycle(plan.plan_id, summarize_cycle(cycle))
    _sync_status_from_cycle(registry, plan.plan_id, cycle)
    return cycle


# ── internals ──────────────────────────────────────────────────────────────


def _safe_evaluate(
    plan: Plan,
    project_root: Path,
    registry: PaperTradingRegistry,
    *,
    quiet: bool,
    console,
) -> Cycle | None:
    """Build engine deps, run one cycle, capture exceptions onto the registry."""
    ledger_path, alerts_log, cache_dir = _runtime_paths(project_root, plan.plan_id)

    try:
        ledger = Ledger(ledger_path, plan_id=plan.plan_id)
    except Exception as e:
        registry.record_error(
            plan.plan_id,
            f"ledger init failed: {type(e).__name__}: {e}",
        )
        return None

    try:
        notifier = Notifier(log_path=alerts_log, console=console, quiet=quiet)
        calendar = CalendarSource(cache_dir=cache_dir)
        prices = PriceSource()
        engine = Engine(plan, ledger, calendar, prices, notifier)
        return engine.evaluate()
    except Exception as e:
        # Engine internals are designed to catch most network errors and record
        # them as cycle.notes — anything that reaches us here is a hard bug.
        registry.record_error(
            plan.plan_id,
            f"engine.evaluate failed: {type(e).__name__}: {e}",
        )
        return None


def _sync_status_from_cycle(
    registry: PaperTradingRegistry,
    plan_id: str,
    cycle: Cycle,
) -> None:
    """Map the engine's plan_state onto the registry status."""
    mapping = {
        "expired": "expired",
        "invalidated": "invalidated",
    }
    target = mapping.get(cycle.plan_state)
    if target is not None:
        registry.set_status(plan_id, target)
        return

    # If a position was opened and then fully closed in the same series of cycles,
    # we don't auto-close the registry entry — the operator may want to re-open on
    # a second signal. Closing the registry row is currently a manual operation.
