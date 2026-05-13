#!/usr/bin/env python
"""Seed the autonomous loop with the first debate row.

Usage:
    python scripts/seed_debate.py \
        --symbol EURUSD \
        --topic "EUR/USD weekly check-in" \
        --decisions "direction,entry,stop,target,size" \
        --schedule-for now

After running, the next invocation of run_autonomous.py will pick up this row
and fire the debate. Subsequent debates self-schedule based on each moderator's
next_review decision.
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime, timezone, timedelta
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

from trade_council.scheduler import Scheduler  # noqa: E402


def _parse_schedule_for(value: str) -> str:
    """Parse --schedule-for input into an ISO UTC timestamp."""
    v = value.strip().lower()
    if v == "now":
        return datetime.now(timezone.utc).isoformat()
    if v.startswith("+"):
        # "+1h", "+30m", "+2d"
        try:
            n = int(v[1:-1])
            unit = v[-1]
            now = datetime.now(timezone.utc)
            if unit == "m":
                return (now + timedelta(minutes=n)).isoformat()
            if unit == "h":
                return (now + timedelta(hours=n)).isoformat()
            if unit == "d":
                return (now + timedelta(days=n)).isoformat()
        except (ValueError, IndexError):
            pass
    # Assume ISO datetime
    try:
        dt = datetime.fromisoformat(v.replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise SystemExit(
            f"Could not parse --schedule-for {value!r}. "
            f"Accepted: 'now', '+1h', '+2d', '+30m', or ISO datetime."
        )


def main() -> int:
    p = argparse.ArgumentParser(description="Seed a debate into the autonomous schedule.")
    p.add_argument("--symbol", required=True, help="FX pair, e.g. EURUSD")
    p.add_argument("--topic", required=True, help="Debate topic")
    p.add_argument("--decisions", required=True,
                   help="Comma-separated decision IDs (built-ins auto-appended at run time)")
    p.add_argument("--specialists", default="all",
                   help="Comma-separated specialist names or 'all' (default: all)")
    p.add_argument("--moderator", default="fx_swing_chair",
                   help="Moderator profile (default: fx_swing_chair)")
    p.add_argument("--schedule-for", default="now",
                   help="When to run: 'now', '+1h', '+2d', or ISO datetime")
    p.add_argument("--db", default=None,
                   help="Path to schedule.sqlite (default: <project_root>/schedule.sqlite)")
    args = p.parse_args()

    decisions = [d.strip() for d in args.decisions.split(",") if d.strip()]
    specialists = [s.strip() for s in args.specialists.split(",") if s.strip()]
    if specialists == ["all"]:
        specialists = "all"

    next_review_at = _parse_schedule_for(args.schedule_for)
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "schedule.sqlite"

    s = Scheduler(db_path)
    rid = s.add_debate(
        symbol=args.symbol,
        topic=args.topic,
        decisions=decisions,
        specialists=specialists,
        moderator=args.moderator,
        next_review_at=next_review_at,
        reconvene_reason="manual seed",
    )
    s.close()

    print(f"[seed_debate] inserted schedule row id={rid}")
    print(f"  symbol:        {args.symbol}")
    print(f"  topic:         {args.topic}")
    print(f"  decisions:     {decisions}")
    print(f"  specialists:   {specialists}")
    print(f"  moderator:     {args.moderator}")
    print(f"  next_review:   {next_review_at}")
    print(f"  db:            {db_path}")
    print()
    print("Next: run scripts/run_autonomous.py to fire any due debates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
