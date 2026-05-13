#!/usr/bin/env python
"""CLI entry point for running a trade_council debate.

Examples:

    # Arg-driven
    python scripts/run_debate.py \
        --topic "GBP/JPY this week - long, short, or stand aside?" \
        --decisions "direction,entry,stop,target,size" \
        --specialists all \
        --format council \
        --moderator fx_swing_chair

    # Interactive (no args — prompts step by step)
    python scripts/run_debate.py

    # Use all specialists in specialists/
    python scripts/run_debate.py --topic "..." --decisions "d1" --specialists all
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr so em-dashes and other Unicode from the .md
# briefs render on Windows consoles (default cp1252 mangles them).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# Make `trade_council` importable when running this file directly.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Load .env so ANTHROPIC_API_KEY is picked up without manual shell exports.
# Safe to call even if .env is absent — load_dotenv just returns False.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv listed in requirements.txt; this is just defensive.

from trade_council import run_debate  # noqa: E402
from trade_council.config import (  # noqa: E402
    load_specialists, load_moderator, parse_markdown_with_frontmatter,
)


def _list_available(label: str, directory: Path) -> list[str]:
    return sorted(
        p.stem for p in directory.glob("*.md") if not p.name.startswith("_")
    )


def _interactive_args() -> argparse.Namespace:
    """Walk the user through the prompts step by step."""
    print("=" * 70)
    print("  trade_council — interactive debate setup")
    print("=" * 70)

    available_specialists = _list_available("specialists", PROJECT_ROOT / "specialists")
    available_moderators = _list_available("moderators", PROJECT_ROOT / "moderators")

    topic = input("\nTopic to debate: ").strip()
    while not topic:
        topic = input("Topic cannot be empty. Topic to debate: ").strip()

    print("\nList the decisions the moderator must resolve (comma-separated IDs).")
    print("Example: calibration_method, sample_size_minimum")
    decisions_raw = input("Decisions: ").strip()
    decisions = [d.strip() for d in decisions_raw.split(",") if d.strip()]
    while not decisions:
        decisions_raw = input("Need at least one decision. Decisions: ").strip()
        decisions = [d.strip() for d in decisions_raw.split(",") if d.strip()]

    print(f"\nAvailable specialists: {', '.join(available_specialists) or '(none — add files to specialists/)'}")
    print("Enter comma-separated names, or 'all' to use every specialist.")
    spec_raw = input("Specialists: ").strip() or "all"
    specialists = [s.strip() for s in spec_raw.split(",") if s.strip()]

    print(f"\nAvailable moderators: {', '.join(available_moderators)}")
    moderator = input("Moderator (default: fx_swing_chair): ").strip() or "fx_swing_chair"

    fmt = input("\nFormat (default: council): ").strip() or "council"

    research_raw = input("\nRun pre-debate research packet (WebSearch/WebFetch)? [Y/n]: ").strip().lower()
    no_research = research_raw in ("n", "no")

    args = argparse.Namespace(
        topic=topic, decisions=",".join(decisions),
        specialists=",".join(specialists),
        moderator=moderator, format=fmt, output=None,
        no_research=no_research,
    )
    return args


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run a trade_council debate.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--topic", help="The topic/question to debate.")
    p.add_argument("--symbol", default=None,
                   help="Instrument symbol (e.g. EURUSD, GBPJPY). Required for auto-scheduling "
                        "the follow-up debate; if omitted, follow-up scheduling is skipped and "
                        "must be done manually via scripts/seed_debate.py.")
    p.add_argument("--decisions", help="Comma-separated list of decision IDs the moderator must resolve.")
    p.add_argument("--specialists", default="all",
                   help="Comma-separated specialist names (filenames without .md), or 'all'. Default: all.")
    p.add_argument("--moderator", default="fx_swing_chair",
                   help="Moderator profile (filename in moderators/). Default: fx_swing_chair.")
    p.add_argument("--format", default="council",
                   help="Debate format. Default: council.")
    p.add_argument("--output", default=None,
                   help="Output directory. Default: debates/<date>_<topic_slug>/.")
    p.add_argument("--list", action="store_true",
                   help="List available specialists, moderators, formats, then exit.")
    p.add_argument("--no-research", action="store_true",
                   help="Skip the pre-debate market-state research packet. "
                        "By default, the orchestrator runs a research step (WebSearch/WebFetch) "
                        "before the debate to build a shared market-state briefing for all specialists.")
    return p


def _print_listing():
    print("Available specialists (in specialists/):")
    for s in _list_available("specialists", PROJECT_ROOT / "specialists"):
        try:
            fm, _ = parse_markdown_with_frontmatter(PROJECT_ROOT / "specialists" / f"{s}.md")
            print(f"  - {s:30s}  {fm.get('expertise', '')}")
        except Exception as e:
            print(f"  - {s:30s}  (failed to parse: {e})")
    print("\nAvailable moderators (in moderators/):")
    for m in _list_available("moderators", PROJECT_ROOT / "moderators"):
        try:
            fm, _ = parse_markdown_with_frontmatter(PROJECT_ROOT / "moderators" / f"{m}.md")
            print(f"  - {m:30s}  {fm.get('role', '')}")
        except Exception as e:
            print(f"  - {m:30s}  (failed to parse: {e})")
    print("\nAvailable formats:")
    from trade_council.formats import FORMATS
    for f in sorted(FORMATS.keys()):
        print(f"  - {f}")


def main():
    parser = _build_argparser()
    args = parser.parse_args()

    if args.list:
        _print_listing()
        return 0

    # Interactive mode if topic/decisions not provided
    if not args.topic or not args.decisions:
        if args.topic or args.decisions:
            print("Both --topic and --decisions are required for non-interactive mode.\n"
                  "Either provide both, or run with no arguments for interactive mode.")
            return 2
        args = _interactive_args()

    decisions = [d.strip() for d in args.decisions.split(",") if d.strip()]
    specialists = [s.strip() for s in args.specialists.split(",") if s.strip()]
    if specialists == ["all"]:
        specialists = None

    output_dir = Path(args.output) if args.output else None

    enable_research = not getattr(args, "no_research", False)

    print()
    print("=" * 70)
    print(f"  Topic:        {args.topic}")
    print(f"  Decisions:    {decisions}")
    print(f"  Specialists:  {specialists or '(all available)'}")
    print(f"  Moderator:    {args.moderator}")
    print(f"  Format:       {args.format}")
    print(f"  Research:     {'ON (pre-debate packet)' if enable_research else 'OFF'}")
    print("=" * 70)

    try:
        transcript = run_debate(
            topic=args.topic,
            decisions=decisions,
            specialist_names=specialists,
            format_name=args.format,
            moderator_name=args.moderator,
            project_root=PROJECT_ROOT,
            output_dir=output_dir,
            enable_research=enable_research,
        )
    except Exception as e:
        print(f"\n[error] {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("\n" + "=" * 70)
    print(f"  Debate complete.")
    print(f"  Turns:         {len(transcript.turns)}")
    print(f"  Decisions:     {len(transcript.decisions_recorded)} / {len(transcript.decision_ids)}")
    print(f"  Total cost:    ${transcript.total_cost_usd():.4f}")
    print(f"  Total time:    {transcript.total_duration_ms() / 1000:.1f}s")
    print("=" * 70)
    print()

    # Final decision summary
    if transcript.decisions_recorded:
        print("Decisions:")
        for d in transcript.decisions_recorded:
            print(f"  • {d.decision_id}: {d.value}")
            print(f"    {d.rationale}")
    if transcript.end_summary:
        print(f"\nModerator summary: {transcript.end_summary}")

    # Auto-schedule the follow-up debate so the autonomous loop picks it up.
    # Mirrors what run_autonomous.py::process_one() does after each debate it
    # processes. Without this, a manual `run_debate.py` invocation creates a
    # dangling plan with no scheduled reconvene.
    if not args.symbol:
        print()
        print("[scheduler] --symbol not provided; follow-up debate NOT auto-scheduled.")
        print("[scheduler] Seed it manually via scripts/seed_debate.py if you want autonomy.")
    else:
        _schedule_follow_up(
            symbol=args.symbol,
            transcript=transcript,
            decisions=decisions,
            specialists=specialists,
            moderator=args.moderator,
        )

    return 0


def _schedule_follow_up(symbol, transcript, decisions, specialists, moderator):
    """Pull next_review from the transcript, resolve it, and add to schedule.

    Best-effort: prints diagnostics and continues on any failure rather than
    raising — a debate that completed successfully shouldn't fail at the end
    just because the follow-up couldn't be scheduled.
    """
    try:
        from trade_council.scheduler import Scheduler, resolve_next_review
        from trade_council.builtin_decisions import NEXT_REVIEW
    except Exception as e:
        print(f"\n[scheduler] could not import scheduler: {type(e).__name__}: {e}")
        return

    next_review_value = ""
    rationale = ""
    for d in transcript.decisions_recorded:
        if d.decision_id == NEXT_REVIEW:
            next_review_value = d.value or ""
            rationale = d.rationale or ""
            break

    if not next_review_value:
        print("\n[scheduler] no next_review decision found in transcript; defaulting to 'in 2 days'")
        next_review_value = "in 2 days"

    # Best-effort calendar source for event-based resolution.
    cal = None
    try:
        from trade_council.paper_trader.data.calendar import CalendarSource
        cal = CalendarSource()
    except Exception as e:
        print(f"[scheduler] calendar source unavailable ({e}); event phrases will fall back to +2 days")

    next_iso = resolve_next_review(next_review_value, calendar_source=cal)

    try:
        db_path = PROJECT_ROOT / "schedule.sqlite"
        sched = Scheduler(db_path)
        follow_id = sched.add_debate(
            symbol=symbol,
            topic=f"{symbol.upper()} follow-up: {next_review_value}",
            decisions=decisions,
            specialists=specialists if specialists else "all",
            moderator=moderator,
            next_review_at=next_iso,
            reconvene_reason=rationale or next_review_value,
        )
        sched.close()
        print(f"\nNext debate auto-scheduled: id={follow_id} symbol={symbol.upper()} "
              f"at {next_iso} (reason: {next_review_value!r})")
    except Exception as e:
        print(f"\n[scheduler] add_debate failed: {type(e).__name__}: {e}")
        print("[scheduler] Seed manually via scripts/seed_debate.py to recover.")


if __name__ == "__main__":
    sys.exit(main())
