"""Council format — moderator-driven, multi-decision debate."""
from __future__ import annotations
from .base import BaseFormat
from ..transcript import Transcript


class CouncilFormat(BaseFormat):
    """Moderator drives the loop. Multi-decision aware.

    Flow:
      1. OPENING ROUND — each specialist gives an opening position addressing the
         topic and the decision list.
      2. MODERATOR LOOP — each iteration:
         - Moderator picks ONE action: follow_up | decide | end.
         - follow_up routes to one specialist (or all). They respond in turn.
         - decide records the decision and continues.
         - end terminates (only valid when all decisions are decided).
      3. Safety: max_total_turns ceiling; structured-output parse retries on bad JSON.
    """

    name = "council"

    def __init__(self, max_total_turns: int = 60, max_parse_retries: int = 2,
                 console=None, overseer=None):
        self.max_total_turns = max_total_turns
        self.max_parse_retries = max_parse_retries
        self.console = console
        self.overseer = overseer  # optional PortfolioOverseer; consulted at each decide

    def run(self, transcript: Transcript, specialists, moderator):
        self._opening_round(transcript, specialists)
        self._moderator_loop(transcript, specialists, moderator)

    # ---------- Phases ----------

    def _opening_round(self, transcript: Transcript, specialists):
        for s in specialists:
            self._log(f"\n[turn] {s.name} (opening)...")
            prompt = self._build_specialist_prompt(
                transcript, s, specialists,
                instruction=(
                    "This is the OPENING round of the debate. Give your initial "
                    "position on the topic and briefly address each decision in "
                    "the list. Be substantive but not exhaustive — you'll have "
                    "more turns to elaborate. 2–4 paragraphs is ideal."
                ),
            )
            resp = s.respond(prompt)
            transcript.add_turn(
                speaker=s.name, role="specialist", content=resp.text,
                cost_usd=resp.cost_usd, duration_ms=resp.duration_ms,
            )
            self._log(f"\n=== {s.name} ===\n{resp.text}\n")

    def _moderator_loop(self, transcript: Transcript, specialists, moderator):
        parse_retries = 0
        while len(transcript.turns) < self.max_total_turns:
            self._log(f"\n[turn] MODERATOR ({moderator.name})...")
            mod_prompt = self._build_moderator_prompt(transcript, specialists)

            try:
                resp, action = moderator.respond(mod_prompt)
            except ValueError as e:
                if parse_retries < self.max_parse_retries:
                    parse_retries += 1
                    self._log(f"[warn] Moderator output didn't parse ({e}). Retry {parse_retries}/{self.max_parse_retries}.")
                    continue
                raise RuntimeError(
                    f"Moderator structured output failed to parse after "
                    f"{self.max_parse_retries} retries. Last error: {e}"
                )
            parse_retries = 0

            transcript.add_turn(
                speaker=f"MODERATOR ({moderator.name})", role="moderator",
                content=resp.text, cost_usd=resp.cost_usd, duration_ms=resp.duration_ms,
                metadata={"action": action.payload},
            )
            self._log(f"\n=== MODERATOR ({moderator.name}) ===\n{resp.text}\n")

            # Route on action
            if action.action == "end":
                if not transcript.all_decided():
                    pending = [d for d, s in transcript.decision_status().items() if s == "pending"]
                    self._log(f"[warn] Moderator tried to end with pending decisions: {pending}. Continuing.")
                    continue
                transcript.ended = True
                transcript.end_summary = action.payload.get("summary", "")
                self._log(f"\n[debate ended] {transcript.end_summary}")
                return

            if action.action == "decide":
                did = action.payload.get("decision_id")
                value = action.payload.get("value", "")
                rationale = action.payload.get("rationale", "")
                if did not in transcript.decision_ids:
                    self._log(f"[warn] Moderator tried to record unknown decision_id={did!r}. Skipping.")
                    continue

                # Portfolio overseer check (optional, only when configured).
                # Only consult on substantive trade decisions, not meta-builtins.
                if self.overseer is not None and did not in {"should_act", "next_review", "confidence_level"}:
                    self._run_overseer_check(transcript, action.payload)

                transcript.record_decision(did, value, rationale)
                self._log(f"[decided] {did} = {value}")
                continue

            if action.action == "follow_up":
                target = action.payload.get("to", "all")
                question = action.payload.get("question", "").strip()
                if not question:
                    self._log("[warn] Moderator follow_up with empty question. Skipping.")
                    continue
                if target == "all":
                    targets = specialists
                else:
                    targets = [s for s in specialists if s.name.lower() == target.lower()]
                    if not targets:
                        self._log(f"[warn] Moderator asked unknown specialist {target!r}. Treating as 'all'.")
                        targets = specialists
                for s in targets:
                    self._log(f"\n[turn] {s.name} (replying to moderator)...")
                    sp_prompt = self._build_specialist_prompt(
                        transcript, s, specialists,
                        instruction=(
                            f'The moderator has asked you a follow-up:\n\n"{question}"\n\n'
                            "Answer directly and concisely. If your answer depends on "
                            "assumptions, state them. If you've changed your view from "
                            "earlier in the debate, say so explicitly."
                        ),
                    )
                    sresp = s.respond(sp_prompt)
                    transcript.add_turn(
                        speaker=s.name, role="specialist", content=sresp.text,
                        cost_usd=sresp.cost_usd, duration_ms=sresp.duration_ms,
                    )
                    self._log(f"\n=== {s.name} ===\n{sresp.text}\n")
                continue

            self._log(f"[warn] Unknown moderator action {action.action!r}. Stopping.")
            return

        self._log(f"[stop] Max turns ({self.max_total_turns}) reached without explicit end.")
        transcript.ended = True
        transcript.end_summary = "(max_total_turns reached without explicit end)"


    def _run_overseer_check(self, transcript, proposed_payload):
        """Consult the portfolio overseer. If it speaks, append to transcript."""
        try:
            symbol = transcript.topic.upper()  # crude; refine if topic doesn't carry symbol
            for sym in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD",
                        "USDCHF", "EURGBP", "EURJPY", "AUDJPY", "GBPJPY", "XAUUSD"):
                if sym in symbol.replace("/", ""):
                    symbol = sym
                    break
            verdict = self.overseer.evaluate(
                proposed_decision=proposed_payload,
                proposed_symbol=symbol,
                proposed_direction=str(proposed_payload.get("value", "")).lower(),
                proposed_risk_usd=0.0,    # TODO: parse from value/rationale in v2.1
                account_size_usd=50_000.0,
                transcript_excerpt=transcript.transcript_for_prompt()[-2000:],
            )
            if not verdict.tripped:
                return
            self._log(f"\n=== OVERSEER ({self.overseer.name}) ===\n"
                      f"trip: {verdict.trip_reason}\n"
                      f"action: {verdict.action}\n{verdict.text}\n")
            transcript.add_turn(
                speaker=f"OVERSEER ({self.overseer.name})",
                role="overseer",
                content=(f"[deterministic trip: {verdict.trip_reason}]\n\n"
                         f"{verdict.text}\n\n"
                         f"action: {verdict.action}"
                         + (f" | modification: {verdict.modification}" if verdict.modification else "")
                         + (f" | veto_reason: {verdict.veto_reason}" if verdict.veto_reason else "")),
                cost_usd=verdict.cost_usd,
                duration_ms=verdict.duration_ms,
                metadata={"overseer_verdict": {
                    "action": verdict.action, "trip_reason": verdict.trip_reason,
                    "modification": verdict.modification, "veto_reason": verdict.veto_reason,
                }},
            )
        except Exception as e:
            self._log(f"[overseer] check failed: {type(e).__name__}: {e}")

    # ---------- Prompt builders ----------

    def _build_specialist_prompt(self, transcript: Transcript, specialist,
                                 specialists, instruction: str) -> str:
        participants = "\n".join(f"- {s.name}: {s.expertise}" for s in specialists)
        decision_lines = "\n".join(
            f"- {did}: [{status}]"
            for did, status in transcript.decision_status().items()
        )
        packet_block = (
            f"\nPRE-DEBATE MARKET-STATE PACKET (shared baseline for all specialists):\n"
            f"{transcript.research_packet}\n"
            if transcript.research_packet else ""
        )
        return f"""You are participating in a structured debate.

TOPIC: {transcript.topic}

DECISIONS THE MODERATOR WILL RESOLVE:
{decision_lines}

PARTICIPANTS:
{participants}
- MODERATOR ({transcript.moderator})
{packet_block}
DEBATE TRANSCRIPT SO FAR:
{transcript.transcript_for_prompt()}

YOUR INSTRUCTION FOR THIS TURN:
{instruction}

Respond as {specialist.name}. Address other specialists by name when relevant.
Stay in character as defined by your role brief (provided as your system prompt).
The market-state packet above is the shared factual baseline — treat it as given.
If your role brief permits, you may also use WebSearch/WebFetch this turn to chase
information specific to your lens that the packet doesn't cover (e.g., a specific
COT release, a particular yield differential, a chart screenshot). Stay focused —
your tool budget for this turn is small.
Do NOT include a JSON block — only the moderator emits structured output.
"""

    def _build_moderator_prompt(self, transcript: Transcript, specialists) -> str:
        participants = "\n".join(f"- {s.name}: {s.expertise}" for s in specialists)
        decision_lines = "\n".join(
            f"- {did}: [{status}]"
            for did, status in transcript.decision_status().items()
        )
        if transcript.decisions_recorded:
            recorded = "\n".join(
                f"- {d.decision_id}: {d.value}\n  rationale: {d.rationale}"
                for d in transcript.decisions_recorded
            )
        else:
            recorded = "(none yet)"
        packet_block = (
            f"\nPRE-DEBATE MARKET-STATE PACKET (shared baseline for all specialists):\n"
            f"{transcript.research_packet}\n"
            if transcript.research_packet else ""
        )
        return f"""You are moderating a structured debate.

TOPIC: {transcript.topic}

DECISIONS TO RESOLVE:
{decision_lines}

PARTICIPANTS:
{participants}
{packet_block}
DEBATE TRANSCRIPT SO FAR:
{transcript.transcript_for_prompt()}

DECISIONS ALREADY RECORDED:
{recorded}

GUIDANCE:
- If a decision feels unresolved or specialists disagree sharply, ask a focused
  follow-up to one specialist or to all.
- When you have enough to commit a decision, commit it via the 'decide' action.
- Only emit 'end' when every decision in the list is decided.
- If your role brief specifies a particular decision style (strict / synthesizer /
  gatekeeper / fx_swing_chair), follow it.
"""

    def _log(self, msg: str):
        if self.console is not None:
            self.console.print(msg)
        else:
            print(msg)
