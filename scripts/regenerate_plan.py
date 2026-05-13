#!/usr/bin/env python
"""Regenerate the paper_trader plan YAML for an existing debate folder.

Use this when:
  - run_autonomous.py failed silently at the plan_generator step
  - You updated the plan-writer prompt and want to re-emit a cleaner plan
  - You hand-edited decisions.md and want a fresh YAML

Usage:
    python scripts/regenerate_plan.py debates/2026-05-11_aud_cad_this_week_...

Prints the full traceback on failure (unlike debate.py which swallows it).
"""
from __future__ import annotations
import argparse
import json
import sys
import traceback
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

from trade_council.transcript import Transcript
from trade_council.plan_generator import generate_plan


def main() -> int:
    p = argparse.ArgumentParser(description="Regenerate plan YAML for a saved debate.")
    p.add_argument("debate_dir", help="Path to debates/<date>_<slug>/")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--backend", default="claude_code", choices=["claude_code", "api"])
    args = p.parse_args()

    debate_dir = Path(args.debate_dir).resolve()
    transcript_json = debate_dir / "transcript.json"
    if not transcript_json.exists():
        print(f"[error] transcript.json not found at {transcript_json}", file=sys.stderr)
        return 1

    with transcript_json.open(encoding="utf-8") as f:
        raw = json.load(f)
    t = Transcript.from_dict(raw)

    print(f"[regenerate_plan] loaded {transcript_json}")
    print(f"  topic:             {t.topic}")
    print(f"  decisions recorded: {len(t.decisions_recorded)}")
    print(f"  ended:             {t.ended}")
    print()

    # debate_ref is relative to project root, matches debate.py convention
    debate_ref = f"debates/{debate_dir.name}"

    # Look for an existing plan that already references this debate.
    # If found, overwrite it (so regenerate-replaces rather than regenerate-duplicates).
    plans_dir = PROJECT_ROOT / "trade_plans"
    existing_output = None
    if plans_dir.exists():
        import yaml as _y
        for yaml_path in plans_dir.glob("*.yaml"):
            try:
                with yaml_path.open(encoding="utf-8") as f:
                    raw = _y.safe_load(f)
            except Exception:
                continue
            if isinstance(raw, dict) and raw.get("debate_ref") == debate_ref:
                if existing_output is None or yaml_path.stat().st_mtime > existing_output.stat().st_mtime:
                    existing_output = yaml_path
        if existing_output:
            print(f"[regenerate_plan] existing plan found, will overwrite: {existing_output}")

    try:
        result = generate_plan(
            transcript=t,
            project_root=PROJECT_ROOT,
            debate_ref=debate_ref,
            output_path=existing_output,
            model=args.model,
            backend_mode=args.backend,
        )
        print(f"\n[regenerate_plan] SUCCESS")
        print(f"  plan written to: {result.plan_path}")
        print(f"  cost:            ${result.response.cost_usd:.4f}")
        print(f"  duration:        {result.response.duration_ms/1000:.1f}s")
        return 0
    except Exception as e:
        print(f"\n[regenerate_plan] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        print(file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
