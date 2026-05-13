"""Portfolio Overseer - cross-debate risk discipline. v2.0 Phase 2.

Architecture:
  1. Deterministic pre-check (free, runs every moderator decide):
     - Reads the live paper_trader ledger
     - Computes prospective correlated-symbol exposure + account-level risk
     - Compares to thresholds in overseer frontmatter
  2. LLM intervention (only when pre-check trips):
     - Single Claude turn with the ledger summary + proposed decision
     - Returns one of: comment / modify / veto
  3. Soft-override authority: the chair can override a veto by explicitly
     acknowledging which limit they're breaching. The format enforces this
     by treating the overseer turn as transcript-visible to the chair.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .backends import get_backend, BackendResponse
from .config import parse_markdown_with_frontmatter


@dataclass
class OverseerConfig:
    name: str
    role: str
    system_prompt: str
    mode: str = "claude_code"
    model: str = "sonnet"
    tools_allowed: list[str] = field(default_factory=list)
    # Thresholds (from frontmatter)
    correlated_exposure_warn: float = 1.0
    correlated_exposure_veto: float = 1.5
    account_risk_warn_pct: float = 4.0
    account_risk_veto_pct: float = 6.0
    correlation_groups: dict = field(default_factory=dict)
    path: Path | None = None


@dataclass
class OverseerVerdict:
    """What the overseer decided about a proposed decision.

    Tripped only when the deterministic check fires. If tripped, the LLM
    is invoked and its response populates action/text/modification.
    """
    tripped: bool                      # True if deterministic check flagged
    trip_reason: str = ""              # human-readable trip description
    action: str = ""                   # 'comment' | 'modify' | 'veto' | '' (silent)
    text: str = ""                     # paragraph from the overseer
    modification: str = ""             # if action == 'modify'
    veto_reason: str = ""              # if action == 'veto'
    cost_usd: float = 0.0
    duration_ms: int = 0


def load_overseer(overseers_dir: Path, name: str = "portfolio_overseer") -> OverseerConfig:
    """Load an overseer profile from overseers/<name>.md."""
    if not overseers_dir.exists():
        raise FileNotFoundError(f"Overseers directory not found: {overseers_dir}")
    path = overseers_dir / f"{name}.md"
    if not path.exists():
        available = sorted(p.stem for p in overseers_dir.glob("*.md"))
        raise ValueError(
            f"Overseer '{name}' not found in {overseers_dir}. Available: {available}"
        )
    fm, body = parse_markdown_with_frontmatter(path)
    return OverseerConfig(
        name=fm.get("name", name),
        role=fm.get("role", ""),
        system_prompt=body,
        mode=fm.get("mode", "claude_code"),
        model=fm.get("model", "sonnet"),
        tools_allowed=fm.get("tools_allowed", []) or [],
        correlated_exposure_warn=float(fm.get("correlated_exposure_warn", 1.0)),
        correlated_exposure_veto=float(fm.get("correlated_exposure_veto", 1.5)),
        account_risk_warn_pct=float(fm.get("account_risk_warn_pct", 4.0)),
        account_risk_veto_pct=float(fm.get("account_risk_veto_pct", 6.0)),
        correlation_groups=fm.get("correlation_groups", {}) or {},
        path=path,
    )


JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class PortfolioOverseer:
    """Cross-debate portfolio risk overseer.

    Construction is cheap; LLM is only invoked when evaluate() trips.
    """

    def __init__(self, config: OverseerConfig, ledger_dir: Path | None = None):
        self.config = config
        self.ledger_dir = ledger_dir
        self._backend = None  # lazy init

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def backend(self):
        if self._backend is None:
            self._backend = get_backend(self.config.mode)
        return self._backend

    def _load_open_ledger_state(self) -> list[dict]:
        """Aggregate live ledger state across all paper_trader ledger files.

        Returns a list of dicts, one per open position. Each dict has:
        {plan_id, symbol, direction, initial_risk_usd, account_size_usd}.
        """
        if not self.ledger_dir or not self.ledger_dir.exists():
            return []
        opens = []
        for f in self.ledger_dir.glob("*.json"):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            pos = raw.get("position")
            if not pos or pos.get("closed"):
                continue
            opens.append({
                "plan_id": raw.get("plan_id"),
                "symbol": pos.get("symbol", "").upper(),
                "direction": pos.get("direction"),
                "initial_risk_usd": float(pos.get("initial_risk_usd", 0.0)),
            })
        return opens

    def _correlation_group(self, symbol: str, direction: str) -> set:
        """Which correlation groups does this (symbol, direction) belong to?

        Direction matters: long EURUSD belongs to USD_short.
        """
        sym = symbol.upper()
        groups = set()
        for gname, members in self.config.correlation_groups.items():
            if sym in [m.upper() for m in members]:
                # Crude direction inference: USD_short pairs (sym ends USD)
                # going long contribute to USD_short. Going short contributes
                # to USD_long. We don't try to be exact - the overseer LLM
                # will sanity-check via its system prompt.
                groups.add(gname)
        return groups

    def deterministic_check(
        self,
        proposed_symbol: str,
        proposed_direction: str,
        proposed_risk_usd: float,
        account_size_usd: float,
    ) -> tuple[bool, str]:
        """Run the cheap pre-check. Returns (tripped, reason).

        If not tripped, the overseer stays silent. If tripped, the caller
        runs the LLM intervention.
        """
        opens = self._load_open_ledger_state()
        if not opens:
            return False, "no open positions"

        # Account-level risk
        total_existing_risk = sum(p["initial_risk_usd"] for p in opens)
        prospective_total = total_existing_risk + proposed_risk_usd
        prospective_pct = (prospective_total / max(account_size_usd, 1.0)) * 100
        if prospective_pct >= self.config.account_risk_veto_pct:
            return True, (
                f"account-level risk veto: prospective open risk "
                f"{prospective_pct:.2f}% >= {self.config.account_risk_veto_pct}%"
            )
        if prospective_pct >= self.config.account_risk_warn_pct:
            return True, (
                f"account-level risk warn: prospective open risk "
                f"{prospective_pct:.2f}% >= {self.config.account_risk_warn_pct}%"
            )

        # Correlated exposure - count positions in same correlation groups
        proposed_groups = self._correlation_group(proposed_symbol, proposed_direction)
        if proposed_groups:
            same_group_count = 0
            for p in opens:
                p_groups = self._correlation_group(p["symbol"], p["direction"])
                if p_groups & proposed_groups:
                    same_group_count += 1
            # Crude: each open position in the same group counts as 1.0 unit
            # of correlated exposure. Adding the proposed = same_group_count + 1
            # versus warn threshold of e.g. 1.0 means "no more than 1 already-open
            # correlated position before warning". This is a v2.0 simplification;
            # a real implementation would weight by initial_risk_usd ratios.
            prospective = float(same_group_count + 1)
            if prospective >= self.config.correlated_exposure_veto:
                return True, (
                    f"correlated-exposure veto: {prospective:.1f} positions in "
                    f"groups {sorted(proposed_groups)} >= "
                    f"{self.config.correlated_exposure_veto}"
                )
            if prospective >= self.config.correlated_exposure_warn:
                return True, (
                    f"correlated-exposure warn: {prospective:.1f} positions in "
                    f"groups {sorted(proposed_groups)} >= "
                    f"{self.config.correlated_exposure_warn}"
                )

        return False, "all checks passed"

    def evaluate(
        self,
        proposed_decision: dict,
        proposed_symbol: str = "",
        proposed_direction: str = "",
        proposed_risk_usd: float = 0.0,
        account_size_usd: float = 50_000.0,
        transcript_excerpt: str = "",
    ) -> OverseerVerdict:
        """Run full overseer: deterministic check, then LLM if tripped.

        Returns OverseerVerdict. If not tripped, verdict.tripped=False and
        the caller should skip adding anything to the transcript.
        """
        tripped, reason = self.deterministic_check(
            proposed_symbol=proposed_symbol,
            proposed_direction=proposed_direction,
            proposed_risk_usd=proposed_risk_usd,
            account_size_usd=account_size_usd,
        )
        verdict = OverseerVerdict(tripped=tripped, trip_reason=reason)
        if not tripped:
            return verdict

        # LLM intervention
        opens = self._load_open_ledger_state()
        opens_block = "\n".join(
            f"  - {p['plan_id']}: {p['direction']} {p['symbol']} "
            f"risk ${p['initial_risk_usd']:.2f}"
            for p in opens
        ) or "  (none)"
        user_prompt = f"""DETERMINISTIC PRE-CHECK FLAGGED:
{reason}

PROPOSED DECISION:
{json.dumps(proposed_decision, indent=2)}

CURRENT OPEN POSITIONS:
{opens_block}

ACCOUNT:
  size_usd: {account_size_usd}
  total_existing_open_risk_usd: {sum(p['initial_risk_usd'] for p in opens):.2f}
  proposed_additional_risk_usd: {proposed_risk_usd:.2f}

RECENT DEBATE CONTEXT:
{transcript_excerpt or '(none)'}

Choose comment / modify / veto per your role brief. Output the JSON block.
"""
        resp = self.backend.call(
            system_prompt=self.config.system_prompt,
            user_prompt=user_prompt,
            model=self.config.model,
            tools_allowed=self.config.tools_allowed,
        )
        verdict.cost_usd = resp.cost_usd
        verdict.duration_ms = resp.duration_ms

        matches = JSON_BLOCK_RE.findall(resp.text)
        for raw in reversed(matches):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("action") in {"comment", "modify", "veto"}:
                verdict.action = payload["action"]
                verdict.text = payload.get("text", "")
                verdict.modification = payload.get("modification", "")
                verdict.veto_reason = payload.get("reason", "")
                return verdict

        # Fallback if no parseable JSON: treat as a comment with full text
        verdict.action = "comment"
        verdict.text = resp.text.strip()[:2000]
        return verdict
