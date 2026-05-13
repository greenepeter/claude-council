"""Paper-trade execution engine for plans produced by trade_council debates.

This subpackage is downstream of the debate orchestrator: it reads a YAML
trade plan, polls live data (economic calendar, spot FX), evaluates the plan's
conditions, and maintains a paper-position ledger. It does NOT route orders.

Public surface used by `scripts/run_paper_trader.py`:
    load_plan(path) -> Plan
    Engine(plan, ledger, calendar_source, price_source, notifier).evaluate() -> Cycle
"""
from __future__ import annotations

from trade_council.paper_trader.plan import Plan, load_plan
from trade_council.paper_trader.ledger import Ledger, Position
from trade_council.paper_trader.engine import Engine, Cycle
from trade_council.paper_trader.notify import Notifier
from trade_council.paper_trader.registry import (
    PaperTradingRegistry,
    RegistryEntry,
    default_registry_path,
    summarize_cycle,
)
from trade_council.paper_trader.enroll import (
    enroll_plan,
    enroll_all_plans_in_dir,
    run_cycle_for_entry,
)

__all__ = [
    "Plan", "load_plan",
    "Ledger", "Position",
    "Engine", "Cycle",
    "Notifier",
    "PaperTradingRegistry", "RegistryEntry",
    "default_registry_path", "summarize_cycle",
    "enroll_plan", "enroll_all_plans_in_dir", "run_cycle_for_entry",
]
