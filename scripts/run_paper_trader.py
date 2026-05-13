#!/usr/bin/env python
"""CLI entry point for the paper-trader engine.

Paper-trade only. Reads a YAML trade plan, polls live data (economic calendar,
spot FX, daily bars), evaluates entry/target/stop conditions, maintains a JSON
ledger of simulated positions. Does NOT place orders.

Examples:

    # One-shot evaluation of the council's EUR/USD plan
    python scripts/run_paper_trader.py --plan trade_plans/eurusd_2026_05_council.yaml

    # Watch loop, evaluate every 5 minutes
    python scripts/run_paper_trader.py --plan trade_plans/eurusd_2026_05_council.yaml --watch --interval 300

    # Mark a correlated position open (so sizing halves on next fill)
    python scripts/run_paper_trader.py --plan ... --mark-correlated-open XAUUSD
    python scripts/run_paper_trader.py --plan ... --mark-correlated-closed XAUUSD

    # Show the current ledger state and exit
    python scripts/run_paper_trader.py --plan ... --show-ledger
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# UTF-8 stdio (project convention — Windows cp1252 mangles em-dashes from .md briefs).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trade_council.paper_trader import Engine, Ledger, Notifier, load_plan  # noqa: E402
from trade_council.paper_trader.data.calendar import CalendarSource  # noqa: E402
from trade_council.paper_trader.data.prices import PriceSource  # noqa: E402


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper-trade a trade_council debate decision.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--plan", required=True, help="Path to YAML trade plan (under trade_plans/).")
    p.add_argument("--ledger", default=None,
                   help="Path to ledger JSON. Default: runtime/<plan_id>_ledger.json")
    p.add_argument("--log", default=None,
                   help="Path to append-only alerts log. Default: runtime/<plan_id>_alerts.log")
    p.add_argument("--cache-dir", default=None,
                   help="Path to data cache dir. Default: runtime/cache/")
    p.add_argument("--watch", action="store_true",
                   help="Loop instead of running one cycle and exiting.")
    p.add_argument("--interval", type=int, default=300,
                   help="Watch interval in seconds. Default: 300 (5 min).")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress console output; still writes the log.")
    p.add_argument("--show-ledger", action="store_true",
                   help="Print current ledger state and exit (no evaluation, no fetch).")
    p.add_argument("--mark-correlated-open", metavar="SYMBOL",
                   help="Mark a correlated symbol as having an open position, then exit. "
                        "Affects sizing on the next entry fill.")
    p.add_argument("--mark-correlated-closed", metavar="SYMBOL",
                   help="Mark a correlated symbol as flat, then exit.")
    return p


def main() -> int:
    args = _build_argparser().parse_args()

    plan_path = Path(args.plan).resolve()
    if not plan_path.exists():
        print(f"[error] plan not found: {plan_path}", file=sys.stderr)
        return 2

    plan = load_plan(plan_path)

    runtime_dir = PROJECT_ROOT / "runtime"
    ledger_path = Path(args.ledger) if args.ledger else runtime_dir / f"{plan.plan_id}_ledger.json"
    log_path = Path(args.log) if args.log else runtime_dir / f"{plan.plan_id}_alerts.log"
    cache_dir = Path(args.cache_dir) if args.cache_dir else runtime_dir / "cache"

    ledger = Ledger(ledger_path, plan_id=plan.plan_id)

    # ── one-shot administrative modes (do NOT touch the network) ───────────

    if args.mark_correlated_open:
        ledger.set_correlated_open(args.mark_correlated_open, True)
        print(f"Marked {args.mark_correlated_open.upper()} as OPEN (correlated). "
              f"Next fill will use correlated sizing.")
        return 0

    if args.mark_correlated_closed:
        ledger.set_correlated_open(args.mark_correlated_closed, False)
        print(f"Marked {args.mark_correlated_closed.upper()} as CLOSED.")
        return 0

    if args.show_ledger:
        _print_ledger(ledger, plan)
        return 0

    # ── normal eval ────────────────────────────────────────────────────────

    notifier = Notifier(log_path=log_path, quiet=args.quiet)
    calendar = CalendarSource(cache_dir=cache_dir)
    prices = PriceSource()
    engine = Engine(plan, ledger, calendar, prices, notifier)

    if args.watch:
        print(f"[paper_trader] watch mode: every {args.interval}s. ctrl-c to stop.")
        try:
            while True:
                _safe_evaluate(engine)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[paper_trader] interrupted.")
            return 0
    else:
        _safe_evaluate(engine)
        return 0


def _safe_evaluate(engine: Engine) -> None:
    try:
        engine.evaluate()
    except Exception as e:                                          # noqa: BLE001
        # Don't kill the loop on transient errors — log and continue.
        print(f"[paper_trader] cycle failed: {type(e).__name__}: {e}", file=sys.stderr)


def _print_ledger(ledger: Ledger, plan) -> None:
    s = ledger.state
    print(f"Plan: {plan.plan_id}")
    print(f"Started: {s.started}")
    print(f"Last updated: {s.last_updated}")
    print(f"Correlated open: {s.correlated_open or '(none)'}")
    print()
    if s.position is not None:
        p = s.position
        print(f"OPEN POSITION: {p.direction.upper()} {p.symbol} @ {p.entry_price:.5f}")
        print(f"  entry_id={p.entry_id}  initial_size={p.initial_size_units:,.0f} "
              f"open_frac={p.open_fraction:.0%}")
        print(f"  stop={p.stop_price:.5f}  initial_risk=${p.initial_risk_usd:.2f}  "
              f"realized=${p.realized_pnl_usd:.2f}")
        print("  fills:")
        for fill in p.fills:
            print(f"    [{fill.kind:>7}] @ {fill.price:.5f}  frac={fill.fraction:.2f}  "
                  f"{fill.when}  {fill.note}")
    else:
        print("No open position.")
    print()
    if s.closed_positions:
        print(f"Closed positions: {len(s.closed_positions)}")
        total = 0.0
        for p in s.closed_positions:
            total += p.realized_pnl_usd
            print(f"  {p.direction} @ {p.entry_price:.5f} → ${p.realized_pnl_usd:+.2f} "
                  f"(entry_id={p.entry_id})")
        print(f"  cumulative realized: ${total:+.2f}")
    if s.observed_events:
        print()
        print(f"Observed events ({len(s.observed_events)}):")
        for ev in s.observed_events[-5:]:
            print(f"  {ev['when']}  {ev['country']} {ev['title']}  "
                  f"forecast={ev.get('forecast')!r} actual={ev.get('actual')!r}")


if __name__ == "__main__":
    sys.exit(main())
