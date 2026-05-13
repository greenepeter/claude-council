"""Built-in decisions auto-prepended to every trade_council debate.

Three meta-decisions every trade debate must resolve, regardless of topic:

- should_act:        yes | no | paper_only
- next_review:       ISO datetime OR event spec (parsed downstream)
- confidence_level:  A | B | C

These drive the autonomous loop: should_act gates plan_generator; next_review
goes into schedule.sqlite for the scheduler; confidence_level halves/preserves
size and informs the overseer.
"""
from __future__ import annotations

# Decision IDs as constants — orchestrator code references these.
SHOULD_ACT = "should_act"
NEXT_REVIEW = "next_review"
CONFIDENCE = "confidence_level"

BUILTIN_TRADE_DECISIONS = [SHOULD_ACT, NEXT_REVIEW, CONFIDENCE]

# Valid values per built-in. Moderator must commit one of these for each.
VALID_SHOULD_ACT = {"yes", "no", "paper_only"}
VALID_CONFIDENCE = {"A", "B", "C"}

# Default cadence rules — used in fx_swing_chair guidance, exposed here so
# other moderators or formats can reference them too.
CADENCE_RULES = {
    "no_position_no_setup": "2-4 days",
    "no_position_setup_pending": "1-2 days",
    "position_open_no_event": "1 day",
    "position_open_near_event": "2-8 hours or event-based",
}


def prepend_builtins(user_decisions: list[str]) -> list[str]:
    """Return the user's decision list with built-ins appended at the END.

    Append-at-end (not prepend-at-start) so the moderator resolves the
    domain-specific decisions first, then the meta-decisions. The latter
    typically depend on what was decided about the trade.
    """
    out = list(user_decisions)
    for d in BUILTIN_TRADE_DECISIONS:
        if d not in out:
            out.append(d)
    return out


BUILTIN_DECISIONS_GUIDANCE = """
BUILT-IN DECISIONS — every trade_council debate must resolve these three meta-decisions in addition to the domain-specific ones. The moderator commits them at the end of the debate (after the domain decisions are resolved).

- should_act: one of {yes, no, paper_only}
    * yes        = act on the trade with full conviction (paper_trader will open a position when entry triggers)
    * paper_only = the conviction is borderline; act on paper but flag for closer review
    * no         = stand aside; no plan is opened
    Note: regardless of this value, a plan YAML is always emitted (the paper_trader engine respects 'no' by emitting direction=none and empty entries).

- next_review: when should the council reconvene on this instrument?
    * ISO datetime form:   "2026-05-12T13:00:00Z"
    * Event form:          "after USD CPI" or "after FOMC" (the orchestrator resolves to a real timestamp using the calendar)
    Default cadence guidance:
        - No position, no setup pending:   2-4 days
        - No position, setup pending:       1-2 days
        - Position open, no near-term event: 1 day
        - Position open, high-impact event ahead: 2-8 hours OR event-based
    Pick the cadence that matches the post-debate state. A debate that concludes "stand aside, no setup" should NOT reconvene every 4 hours.

- confidence_level: one of {A, B, C}
    * A = high — full Rhea-sized position
    * B = medium — paper_trader sizes at 0.75x (or her configured haircut)
    * C = low — paper_only is more appropriate than yes

These three are used by the orchestrator to: emit the plan, schedule the follow-up, and size the position. Commit them clearly, with rationale rooted in the debate, not the model's general priors.
"""
