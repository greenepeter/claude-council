"""Plan generator — converts a finished debate's decisions into a paper_trader plan YAML.

The moderator commits free-text decision values during a debate. The paper_trader
engine needs structured YAML matching the schema demonstrated by
`trade_plans/eurusd_2026_05_council.yaml`. This module bridges the two by invoking
a "plan-writer" agent: a single Claude turn that reads the transcript +
decisions + schema reference and emits a YAML document that paper_trader can
load directly.

In v2.0 (Phase 1), this is invoked manually after a debate. In Phase 2 the
debate orchestrator calls it automatically whenever should_act ∈ {yes, paper_only}.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import yaml

from .backends import get_backend
from .backends.base import BackendResponse
from .transcript import Transcript


PLAN_WRITER_SYSTEM_PROMPT = """You are a plan-writer. You consume the output of a structured FX swing-trading
debate (transcript + committed decisions + research packet) and emit a single
YAML document that conforms to the paper_trader plan schema.

You do NOT introduce new trading views. You do NOT second-guess the moderator's
decisions. You translate them faithfully into the YAML schema, applying the
schema-compliance rules below.

If a decision is ambiguous, prefer the most conservative interpretation
(tighter stops, smaller size, more event gates).

If the moderator chose NOT to trade (should_act=no), the entries and targets
lists MUST both be []. Direction can be:
  - the moderator's residual directional bias ('short' even if not acting), OR
  - 'none' if the moderator had no clear directional view.
Either is safe because the engine never touches a no-entries plan. Put the
reason in meta.note.

== HARD COMPATIBILITY RULES (the engine will silently drop violations) ==

1. invalidation[].condition MUST be one of: daily_close_below, daily_close_above,
   plan_age_days. The engine does not evaluate anything else. If the moderator
   committed semantic invalidation conditions (e.g., COT percentile above 80,
   hike probability above 90, portfolio exposure above 1%), put them under
   meta.review_triggers as a list of free-text strings -- they will become
   reasons to reconvene early, not engine-evaluated invalidations.

2. targets[].on_fill.trail_stop_to: use the literal string 'entry' to mean
   break-even. Do NOT use 'breakeven' -- the engine only recognizes 'entry'
   as a sentinel and absolute prices as floats. Anything else is dead.

3. Correlated risk haircut: if the moderator's size decision does not
   explicitly set both standalone_risk_pct AND correlated_risk_pct, default
   correlated_risk_pct = standalone_risk_pct / 2. This implements Rhea's
   cross-position rule (a correlated open position cuts available risk in half).

4. event_gates: scan the debate rationale and research packet for high-impact
   scheduled events inside horizon_days. For each, emit an event_gate even if
   the moderator did not commit one as an explicit decision. Conservative
   defaults: block_before_hours=24, block_after_hours=2, behavior=no_new_entries.

5. Top-level YAML must contain ONLY the fields enumerated in the schema below.
   Place should_act, next_review, confidence_level (the meta-decisions), plus
   any prose note or semantic review_triggers, under a top-level 'meta:' block.
   The engine ignores 'meta'; it exists for downstream tooling and audit.

6. entries[].type must be limit_zone or breakout_retest. There is no 'market'
   type in the engine.

7. direction may be 'long', 'short', or 'none'. The engine recognizes only
   'long' and 'short' -- any other value (including 'none') causes the engine
   to silently no-op on stop/target/P&L checks. Therefore: if direction is
   NOT 'long' or 'short', entries MUST be [] and targets MUST be []. The
   plan-writer is responsible for enforcing this invariant.

Your output is ONLY the YAML document. No prose before it, no explanation
after. The orchestrator parses with yaml.safe_load -- if it fails, you'll be
re-prompted with the error.
"""


# Reference schema — paper_trader.plan expects this shape.
# Update if the dataclasses in paper_trader/plan.py change.
PLAN_SCHEMA_REFERENCE = """# Reference schema (this is the format your output MUST match)

plan_id: <snake_case_identifier_unique_per_plan>       # e.g., eurgbp_2026_05_swing_short
created: <YYYY-MM-DD>                                  # today's date UTC
debate_ref: <debates/relative_path_to_this_debate>     # provided in the user prompt
symbol: <UPPERCASE_FX_PAIR>                            # e.g., EURGBP (no slash)
direction: <long | short | none>                       # 'none' = no directional view (requires entries: [])
horizon_days: <int>                                    # plan auto-expires after this many days
confidence: <A | B | C>                                # A=high, B=medium, C=low

account:
  size_usd: 50000                                       # default; moderator can override
  base_currency: USD

entries:
  # Each entry is one independent way into the trade. Most plans have 1-2.
  # If should_act = no, leave entries: []
  - id: <snake_case>
    description: <one-line plain English; multiline OK using YAML > or |>
    type: <limit_zone | breakout_retest>               # only these two are evaluated
    # Type-specific fields:
    #   limit_zone:      zone: [low, high]    stop: <price>
    #   breakout_retest: breakout_level: <price>  retest_tolerance_pips: <int>  stop_on_daily_close_below: <price>
    weight: 1.0                                         # fraction of total risk (1.0 = full)

targets:
  # In order, hit by intraday touch. fraction is what to close at each.
  # If should_act = no, leave targets: []
  - id: <snake_case>
    level: <price>
    fraction: <0.0..1.0>                                # e.g., 0.35 for "take 35%"
    on_fill:
      trail_stop_to: <price as float | 'entry'>        # 'entry' = move stop to entry (break-even). Do NOT use 'breakeven'.

sizing:
  standalone_risk_pct: <pct_of_account>                 # e.g., 0.75 (= 0.75% of account)
  correlated_risk_pct: <pct_of_account>                 # ALWAYS half of standalone unless moderator explicitly overrode
  correlated_symbols: [LIST, OF, PAIRS]                 # e.g., [XAUUSD, AUDJPY]
  no_pyramiding: true

event_gates:
  # Block new entries around scheduled high-impact events inside the holding window.
  # Required if the debate referenced any such event (CPI, NFP, FOMC, CB meeting, etc.)
  - title_contains: "CPI"                               # case-insensitive substring
    country: USD                                        # currency code on the feed
    impact: High                                        # High | Medium | Low
    block_before_hours: 24
    block_after_hours: 2
    behavior: no_new_entries                            # only behavior the engine supports
    note: <one-line rationale>

invalidation:
  # Conditions the ENGINE evaluates every cycle. Only these conditions:
  #   - daily_close_below (level: <price>)
  #   - daily_close_above (level: <price>)
  #   - plan_age_days     (level: <int days>)
  # Anything else here is dead code -- the engine drops it silently.
  - condition: daily_close_above
    level: <price>
    note: <one-line rationale>

meta:
  # Engine ignores this entire block; it is for the orchestrator + audit trail.
  should_act: <yes | no | paper_only>                  # from moderator's built-in decision
  next_review: <ISO datetime | event form>             # from moderator's built-in decision
  confidence_level: <A | B | C>                        # mirror of top-level 'confidence' for completeness
  note: |
    <multi-line rationale and important caveats — anything that doesn't fit elsewhere>
  review_triggers:
    # Semantic conditions to reconvene EARLY (not engine invalidations).
    # Free-text -- the scheduler does not parse these in v2.0; they are audit signals.
    - "<e.g., EUR COT percentile >= 80 on May 16 release>"
    - "<e.g., ECB June hike probability >= 90%>"
"""


@dataclass
class GeneratedPlan:
    plan_path: Path
    plan_dict: dict
    response: BackendResponse


def _slug_from_decisions(transcript: Transcript) -> str:
    """Derive a plan_id slug from the symbol if we can find it in decisions, else from the topic."""
    for d in transcript.decisions_recorded:
        v = d.value.upper() if isinstance(d.value, str) else ""
        for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
                    "USDCHF", "EURGBP", "EURJPY", "AUDJPY", "GBPJPY", "XAUUSD"):
            if sym in v:
                return sym.lower()
    # Fall back to topic
    s = re.sub(r"[^a-zA-Z0-9]+", "_", transcript.topic.lower()).strip("_")
    return s[:30] or "plan"


def _build_user_prompt(transcript: Transcript, debate_ref: str, today: str) -> str:
    decisions_block = "\n".join(
        f"- {d.decision_id}: {d.value}\n  rationale: {d.rationale}"
        for d in transcript.decisions_recorded
    )
    packet_block = (
        f"\n## PRE-DEBATE RESEARCH PACKET\n\n{transcript.research_packet}\n"
        if getattr(transcript, "research_packet", None) else ""
    )
    return f"""## TOPIC OF THE DEBATE
{transcript.topic}

## DECISIONS COMMITTED BY THE MODERATOR
{decisions_block}

## MODERATOR'S END-OF-DEBATE SUMMARY
{transcript.end_summary or "(none recorded)"}
{packet_block}
## FULL DEBATE TRANSCRIPT
{transcript.transcript_for_prompt()}

## METADATA TO USE IN YOUR OUTPUT
- created: {today}
- debate_ref: {debate_ref}

{PLAN_SCHEMA_REFERENCE}

Now produce the YAML plan. Output the YAML document and nothing else.
"""


def _extract_yaml(text: str) -> str:
    """Strip code fences if the model wrapped output in ```yaml ... ```."""
    text = text.strip()
    m = re.match(r"^```(?:yaml|yml)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def generate_plan(
    transcript: Transcript,
    project_root: Path,
    debate_ref: str,
    output_path: Path | None = None,
    model: str = "opus",
    backend_mode: str = "claude_code",
    max_retries: int = 2,
    console=None,
) -> GeneratedPlan:
    """Generate a paper_trader plan YAML from a finished debate.

    Args:
        transcript: The completed Transcript (must have decisions_recorded).
        project_root: D:/trade_council/ (used to resolve trade_plans/).
        debate_ref: Relative path to the debate folder (recorded in plan metadata).
        output_path: Where to write the YAML. Defaults to trade_plans/<slug>.yaml.
        model, backend_mode: Backend selection for the plan-writer agent.
        max_retries: Number of re-prompts if YAML doesn't parse.
        console: Optional rich.Console.

    Returns:
        GeneratedPlan with the path, parsed dict, and backend response.
    """
    def _log(msg: str):
        (console.print if console else print)(msg)

    if not transcript.decisions_recorded:
        raise ValueError(
            "Cannot generate plan: transcript has no recorded decisions. "
            "Run the debate to completion first."
        )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base_user_prompt = _build_user_prompt(transcript, debate_ref, today)
    backend = get_backend(backend_mode)

    last_error = None
    user_prompt = base_user_prompt
    for attempt in range(max_retries + 1):
        _log(f"[plan_generator] attempt {attempt + 1}/{max_retries + 1}")
        resp = backend.call(
            system_prompt=PLAN_WRITER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=model,
        )
        raw_yaml = _extract_yaml(resp.text)
        try:
            plan_dict = yaml.safe_load(raw_yaml)
            if not isinstance(plan_dict, dict):
                raise ValueError(
                    f"Top-level YAML must be a mapping/dict, got {type(plan_dict).__name__}"
                )
            # Minimal schema sanity (paper_trader.plan.load will do full validation)
            for required in ("plan_id", "symbol", "direction", "horizon_days"):
                if required not in plan_dict:
                    raise ValueError(f"Required field missing from generated plan: {required!r}")
            break
        except (yaml.YAMLError, ValueError) as e:
            last_error = e
            _log(f"[plan_generator] YAML parse failed: {e}")
            if attempt >= max_retries:
                raise RuntimeError(
                    f"plan-writer failed to produce valid YAML after "
                    f"{max_retries + 1} attempts. Last error: {e}\n"
                    f"Last raw output (first 2000 chars):\n{raw_yaml[:2000]}"
                )
            # Re-prompt with the error to help the model self-correct
            user_prompt = (
                base_user_prompt
                + "\n\n## RETRY CONTEXT\n"
                + f"Your previous output failed to parse with this error:\n  {e}\n"
                + f"Previous output (first 2000 chars):\n{raw_yaml[:2000]}\n\n"
                + "Emit the YAML again, fixing the issue. Output only the YAML, no prose."
            )

    # Compute output path
    if output_path is None:
        slug = plan_dict.get("plan_id") or _slug_from_decisions(transcript)
        # Make sure slug is safe for filenames
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(slug)).strip("_")[:60] or "plan"
        output_path = project_root / "trade_plans" / f"{slug}.yaml"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(raw_yaml + "\n", encoding="utf-8")
    _log(f"[plan_generator] wrote plan: {output_path}")

    return GeneratedPlan(
        plan_path=output_path,
        plan_dict=plan_dict,
        response=resp,
    )
