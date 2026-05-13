"""Top-level orchestrator — `run_debate()`. v2: auto-built-in decisions + auto-plan."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import re
from .config import load_specialists, load_moderator
from .specialist import Specialist
from .moderator import Moderator
from .transcript import Transcript
from .formats import get_format
from .research import build_research_packet
from .builtin_decisions import (
    prepend_builtins, BUILTIN_TRADE_DECISIONS, SHOULD_ACT,
)
from .plan_generator import generate_plan


def _slugify(text: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return s[:maxlen] or "debate"


def _default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def run_debate(
    topic: str,
    decisions: list[str],
    specialist_names: list[str] | None = None,
    format_name: str = "council",
    moderator_name: str = "fx_swing_chair",
    project_root: Path | None = None,
    output_dir: Path | None = None,
    console=None,
    format_kwargs: dict | None = None,
    enable_research: bool = True,
    research_model: str = "sonnet",
    auto_emit_plan: bool = True,
    include_builtins: bool = True,
    overseer=None,
) -> Transcript:
    """Run a single debate end-to-end.

    Args:
        topic: The question/topic being debated.
        decisions: List of decision IDs the moderator must resolve. Built-in
            meta-decisions (should_act, next_review, confidence_level) are
            auto-appended when include_builtins=True (default).
        specialist_names: List of specialist filenames (without .md). If None
            or ['all'], all specialists in specialists/ are used.
        format_name: Name of the debate format (currently 'council').
        moderator_name: Moderator profile filename. Defaults to fx_swing_chair.
        project_root: Project root path. Defaults to this file's grandparent.
        output_dir: Where to write transcript/decisions. Defaults to
            <project_root>/debates/<date>_<slug>/.
        console: Optional rich.Console for prettier output.
        format_kwargs: Additional kwargs passed to the format constructor.
        enable_research: If True, run pre-debate research packet step.
        research_model: Model for the research step (default 'sonnet').
        auto_emit_plan: If True (default), call plan_generator at end of debate
            to emit a paper_trader YAML. Always emits, even when should_act=no
            (the plan will have direction=none and empty entries).
        include_builtins: If True (default), auto-append should_act / next_review
            / confidence_level to the decision list.
        overseer: Optional PortfolioOverseer instance. If provided, the format
            consults it between moderator decide actions for portfolio risk
            checks. v2.0 Phase 2.

    Returns:
        The completed Transcript object.
    """
    project_root = project_root or _default_project_root()
    specialists_dir = project_root / "specialists"
    moderators_dir = project_root / "moderators"

    specialist_configs = load_specialists(specialists_dir, specialist_names)
    moderator_config = load_moderator(moderators_dir, moderator_name)

    specialists = [Specialist(c) for c in specialist_configs]
    moderator = Moderator(moderator_config)

    final_decisions = prepend_builtins(decisions) if include_builtins else list(decisions)

    transcript = Transcript(
        topic=topic,
        decisions=final_decisions,
        specialists=[s.name for s in specialists],
        moderator=moderator.name,
    )

    if enable_research:
        packet_text, packet_resp = build_research_packet(
            topic=topic,
            decisions=final_decisions,
            model=research_model,
            console=console,
        )
        transcript.research_packet = packet_text
        transcript.research_packet_cost_usd = packet_resp.cost_usd
        transcript.research_packet_duration_ms = packet_resp.duration_ms

    fmt_kwargs = format_kwargs or {}
    if console is not None and "console" not in fmt_kwargs:
        fmt_kwargs["console"] = console
    if overseer is not None and "overseer" not in fmt_kwargs:
        fmt_kwargs["overseer"] = overseer
    fmt = get_format(format_name, **fmt_kwargs)
    fmt.run(transcript, specialists, moderator)

    # Persist debate output
    if output_dir is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = _slugify(topic)
        output_dir = project_root / "debates" / f"{date}_{slug}"
    transcript.save(output_dir)

    # Auto-emit paper_trader plan YAML
    if auto_emit_plan and transcript.decisions_recorded:
        debate_ref = f"debates/{output_dir.name}"
        generated = None
        try:
            generated = generate_plan(
                transcript=transcript,
                project_root=project_root,
                debate_ref=debate_ref,
                console=console,
            )
        except Exception as e:
            import traceback as _tb
            err_msg = f"[plan_generator] failed: {type(e).__name__}: {e}"
            (console.print if console else print)(err_msg)
            # Persist the failure alongside the debate so it does not get
            # silently lost. Caller can also re-run scripts/regenerate_plan.py.
            try:
                (output_dir / "plan_generator_error.txt").write_text(
                    err_msg + "\n\n" + _tb.format_exc(), encoding="utf-8"
                )
            except Exception:
                pass

        # Enroll the freshly emitted plan for paper trading and run one
        # immediate cycle so the plan is acknowledged at debate-end time
        # rather than waiting for the next autonomous poll. Any failure
        # here is recorded but never blocks the debate.
        if generated is not None:
            _enroll_for_paper_trading(
                plan_path=generated.plan_path,
                project_root=project_root,
                debate_ref=debate_ref,
                output_dir=output_dir,
                console=console,
            )

    return transcript


def _enroll_for_paper_trading(
    plan_path,
    project_root: Path,
    debate_ref: str,
    output_dir: Path,
    console,
) -> None:
    """Best-effort post-debate hook: register the plan and tick one cycle."""
    _log = console.print if console else print
    try:
        from .paper_trader.enroll import enroll_plan
        from .paper_trader.registry import summarize_cycle
    except Exception as e:
        _log(f"[paper_trader] skipping enrollment — import failed: {type(e).__name__}: {e}")
        return

    try:
        entry, cycle = enroll_plan(
            plan_path=plan_path,
            project_root=project_root,
            debate_ref=debate_ref,
            run_initial_cycle=True,
            quiet=True,
            console=console,
        )
    except Exception as e:
        import traceback as _tb
        err_msg = f"[paper_trader] enrollment failed: {type(e).__name__}: {e}"
        _log(err_msg)
        try:
            (output_dir / "paper_trader_enroll_error.txt").write_text(
                err_msg + "\n\n" + _tb.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        return

    if cycle is None:
        _log(f"[paper_trader] enrolled {entry.plan_id} (status={entry.status}); "
             f"initial cycle skipped (non-tradeable or eval error — see registry)")
    else:
        _log(f"[paper_trader] enrolled {entry.plan_id} (status={entry.status}); "
             f"cycle: {summarize_cycle(cycle)}")
