#!/usr/bin/env python
"""Autonomous runner — fires due debates, parses next_review, schedules follow-ups.

Designed to be invoked periodically (e.g., once per hour) by:
  - Windows Task Scheduler
  - Linux cron / systemd timer
  - GitHub Actions workflow `schedule:` trigger
  - Or just `watch -n 3600 python scripts/run_autonomous.py`

On each invocation:
  1. Check KILL file -> exit if present.
  2. Check daily cost cap -> exit if exceeded.
  3. Get any debate rows whose next_review_at has passed and status='pending'.
  4. For each: run the debate, parse the moderator's next_review decision,
     insert a follow-up schedule row, record cost.
  5. (Optional) Poll paper_trader for each plan with an open ledger.

The runner is stateless between invocations. All state lives in schedule.sqlite
and the per-plan ledger JSON files. Re-running is safe; no debate fires twice.
"""
from __future__ import annotations
import argparse
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Load .env so ANTHROPIC_API_KEY is picked up without manual shell exports.
# Safe to call even if .env is absent — load_dotenv just returns False.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv listed in requirements.txt; this is just defensive.

from trade_council.scheduler import (  # noqa: E402
    Scheduler, ScheduleRow, resolve_next_review, DEFAULT_DAILY_COST_CAP_USD,
)
from trade_council.debate import run_debate  # noqa: E402
from trade_council.builtin_decisions import NEXT_REVIEW, SHOULD_ACT  # noqa: E402


KILL_FILE_NAME = "KILL"
DEFAULT_DB = "schedule.sqlite"


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def _cost_cap() -> float:
    env = os.environ.get("COST_CAP_USD_PER_DAY")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_DAILY_COST_CAP_USD


def _extract_next_review(transcript) -> tuple[str, str]:
    """Return (raw_value, rationale) for the next_review decision, or ('','')."""
    for d in transcript.decisions_recorded:
        if d.decision_id == NEXT_REVIEW:
            return d.value, d.rationale
    return "", ""


def _extract_should_act(transcript) -> str:
    for d in transcript.decisions_recorded:
        if d.decision_id == SHOULD_ACT:
            return d.value
    return ""


def _try_calendar_source():
    """Best-effort load of paper_trader's CalendarSource for event resolution."""
    try:
        from trade_council.paper_trader.data.calendar import CalendarSource
        return CalendarSource()
    except Exception as e:
        _log(f"calendar source not available ({e}); event-based next_review won't resolve")
        return None


def process_one(scheduler: Scheduler, row: ScheduleRow, project_root: Path) -> float:
    """Run one debate row end-to-end. Returns the cost incurred."""
    _log(f"firing debate id={row.id} symbol={row.symbol} topic={row.topic!r}")
    scheduler.mark_running(row.id)

    try:
        specialist_names = row.specialists if isinstance(row.specialists, list) else None
        transcript = run_debate(
            topic=row.topic,
            decisions=row.decisions,
            specialist_names=specialist_names,
            moderator_name=row.moderator,
            project_root=project_root,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}"
        _log(f"debate {row.id} failed: {err.splitlines()[0]}")
        scheduler.mark_error(row.id, err)
        return 0.0

    cost = transcript.total_cost_usd()
    debate_dir_str = ""
    # Best-effort: find the most recent debate folder under debates/ matching this topic.
    debates_root = project_root / "debates"
    if debates_root.exists():
        candidates = sorted(debates_root.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            debate_dir_str = f"debates/{candidates[0].name}"

    scheduler.mark_completed(row.id, cost_usd=cost, debate_dir=debate_dir_str)
    scheduler.record_cost(row.id, cost, "debate", note=f"sym={row.symbol}")

    # Schedule the follow-up
    next_review_value, rationale = _extract_next_review(transcript)
    if not next_review_value:
        _log(f"debate {row.id} did not commit a next_review decision; defaulting to +2 days")
        next_review_value = "in 2 days"

    cal = _try_calendar_source()
    next_iso = resolve_next_review(next_review_value, calendar_source=cal)

    followup_id = scheduler.add_debate(
        symbol=row.symbol,
        topic=f"{row.symbol} follow-up: {next_review_value}",
        decisions=row.decisions,
        specialists=row.specialists if isinstance(row.specialists, list) else "all",
        moderator=row.moderator,
        next_review_at=next_iso,
        reconvene_reason=rationale or next_review_value,
        parent_debate_id=row.id,
    )
    should_act = _extract_should_act(transcript)
    _log(
        f"debate {row.id} done: cost=${cost:.4f} should_act={should_act!r} "
        f"-> follow-up id={followup_id} at {next_iso}"
    )
    return cost




def poll_paper_traders(project_root: Path) -> int:
    """Run one paper_trader cycle per active plan in the registry.

    Reads `runtime/paper_trading_registry.json` (managed by debate enrollment)
    and ticks one Engine.evaluate() cycle per active entry. Plans that the
    engine marks expired/invalidated are auto-skipped on subsequent polls
    via registry status. Errors on a single plan are recorded but never halt
    other plans. Returns the count of plans evaluated this poll.
    """
    try:
        from trade_council.paper_trader.registry import (
            PaperTradingRegistry, default_registry_path,
        )
        from trade_council.paper_trader.enroll import (
            run_cycle_for_entry, enroll_all_plans_in_dir,
        )
    except Exception as e:
        _log(f"paper_trader: imports failed ({type(e).__name__}: {e}); skipping")
        return 0

    # Pick up any YAMLs in trade_plans/ that aren't yet registered.
    # Debate-emitted plans are auto-enrolled by run_debate(); this catches
    # manually authored or pre-existing files.
    backfilled = enroll_all_plans_in_dir(
        project_root / "trade_plans", project_root, debate_ref="(backfill)",
    )
    if backfilled:
        _log(f"paper_trader: backfilled {len(backfilled)} pre-existing plan(s) into registry: "
             + ", ".join(e.plan_id for e in backfilled))

    registry_path = default_registry_path(project_root)
    registry = PaperTradingRegistry(registry_path, project_root)
    active = registry.list_active()
    if not active:
        _log("paper_trader: 0 active plans in registry")
        return 0

    evaluated = 0
    for entry in active:
        try:
            _log(f"paper_trader: evaluating {entry.plan_id} ({entry.symbol})")
            cycle = run_cycle_for_entry(entry, project_root, quiet=True)
            if cycle is None:
                _log(f"paper_trader [{entry.plan_id}] no cycle "
                     f"(see registry last_error / status)")
            else:
                evaluated += 1
        except Exception as e:
            _log(f"paper_trader [{entry.plan_id}] error: {type(e).__name__}: {e}")
    return evaluated


def poll_once(project_root: Path, db_path: Path) -> dict:
    """One scheduler pass. Returns a summary dict."""
    summary = {"started_at": datetime.now(timezone.utc).isoformat(),
               "fired": 0, "errors": 0, "cost_capped": 0,
               "total_cost_usd": 0.0, "killed": False}

    # KILL check
    kill_path = project_root / KILL_FILE_NAME
    if kill_path.exists():
        _log(f"KILL file present at {kill_path} - exiting without firing")
        summary["killed"] = True
        return summary

    scheduler = Scheduler(db_path)
    try:
        today_cost = scheduler.get_today_cost_usd()
        cap = _cost_cap()
        _log(f"today's API spend so far: ${today_cost:.2f} / cap ${cap:.2f}")

        if today_cost >= cap:
            _log(f"daily cost cap reached - skipping all debates")
            return summary

        due = scheduler.get_due()
        _log(f"{len(due)} debate(s) due")

        for row in due:
            # Re-check KILL and cap before each debate
            if kill_path.exists():
                _log(f"KILL file appeared mid-run - stopping after current state")
                summary["killed"] = True
                break
            if today_cost >= cap:
                _log(f"daily cap exceeded mid-run; marking remaining as cost_capped")
                for remaining in due[due.index(row):]:
                    scheduler.mark_cost_capped(remaining.id)
                    summary["cost_capped"] += 1
                break

            cost = process_one(scheduler, row, project_root)
            summary["fired"] += 1
            summary["total_cost_usd"] += cost
            today_cost += cost
            if cost == 0.0 and row.status != "completed":
                summary["errors"] += 1

    finally:
        scheduler.close()

    # Tick paper_trader cycles on every poll, even when no debates fired.
    # These are free (no LLM) and need to run regularly to manage open positions.
    if not summary.get("killed"):
        try:
            n_plans = poll_paper_traders(project_root)
            summary["paper_trader_plans_evaluated"] = n_plans
        except Exception as e:
            _log(f"paper_trader polling failed: {type(e).__name__}: {e}")
            summary["paper_trader_plans_evaluated"] = 0

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    _log(
        f"poll done: fired={summary['fired']} errors={summary['errors']} "
        f"cost_capped={summary['cost_capped']} "
        f"paper_plans={summary.get('paper_trader_plans_evaluated', 0)} "
        f"total_cost=${summary['total_cost_usd']:.4f}"
    )
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="Run the trade_council autonomous loop once.")
    p.add_argument("--db", default=None, help=f"Path to schedule DB (default: {DEFAULT_DB})")
    p.add_argument("--project-root", default=None,
                   help="Project root (default: this script's parent)")
    p.add_argument("--dry-run", action="store_true",
                   help="List due debates and exit, don't fire them")
    args = p.parse_args()

    project_root = Path(args.project_root) if args.project_root else PROJECT_ROOT
    db_path = Path(args.db) if args.db else project_root / DEFAULT_DB

    if args.dry_run:
        s = Scheduler(db_path)
        due = s.get_due()
        cap = _cost_cap()
        cost = s.get_today_cost_usd()
        s.close()
        _log(f"DRY RUN: {len(due)} due, today's cost=${cost:.2f} / cap=${cap:.2f}")
        for r in due:
            print(f"  - id={r.id} symbol={r.symbol} due={r.next_review_at} topic={r.topic!r}")
        return 0

    summary = poll_once(project_root, db_path)
    return 0 if not summary.get("killed") else 130


if __name__ == "__main__":
    sys.exit(main())
