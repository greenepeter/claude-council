"""Transcript management — running log of turns + decision tracking + serialization."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
import json


@dataclass
class Turn:
    speaker: str         # specialist name, or 'MODERATOR (<name>)'
    role: str            # 'specialist' or 'moderator'
    content: str
    timestamp: str       # ISO 8601 UTC
    cost_usd: float = 0.0
    duration_ms: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class Decision:
    decision_id: str
    value: str
    rationale: str
    timestamp: str


class Transcript:
    def __init__(self, topic: str, decisions: list[str],
                 specialists: list[str], moderator: str):
        self.topic = topic
        self.decision_ids: list[str] = list(decisions)
        self.specialists: list[str] = list(specialists)
        self.moderator: str = moderator
        self.started_at: str = datetime.now(timezone.utc).isoformat()
        self.turns: list[Turn] = []
        self.decisions_recorded: list[Decision] = []
        self.ended: bool = False
        self.end_summary: str = ""
        self.research_packet: str = ""
        self.research_packet_cost_usd: float = 0.0
        self.research_packet_duration_ms: int = 0

    def add_turn(self, speaker: str, role: str, content: str,
                 cost_usd: float = 0.0, duration_ms: int = 0,
                 metadata: dict | None = None):
        self.turns.append(Turn(
            speaker=speaker, role=role, content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            cost_usd=cost_usd, duration_ms=duration_ms,
            metadata=metadata or {},
        ))

    def record_decision(self, decision_id: str, value: str, rationale: str):
        self.decisions_recorded.append(Decision(
            decision_id=decision_id, value=value, rationale=rationale,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))

    def decision_status(self) -> dict[str, str]:
        decided = {d.decision_id for d in self.decisions_recorded}
        return {did: ("decided" if did in decided else "pending")
                for did in self.decision_ids}

    def all_decided(self) -> bool:
        return all(s == "decided" for s in self.decision_status().values())

    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns) + self.research_packet_cost_usd

    def total_duration_ms(self) -> int:
        return sum(t.duration_ms for t in self.turns) + self.research_packet_duration_ms

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "decision_ids": self.decision_ids,
            "specialists": self.specialists,
            "moderator": self.moderator,
            "started_at": self.started_at,
            "turns": [asdict(t) for t in self.turns],
            "decisions": [asdict(d) for d in self.decisions_recorded],
            "ended": self.ended,
            "end_summary": self.end_summary,
            "research_packet": self.research_packet,
            "research_packet_cost_usd": self.research_packet_cost_usd,
            "research_packet_duration_ms": self.research_packet_duration_ms,
            "total_cost_usd": self.total_cost_usd(),
            "total_duration_ms": self.total_duration_ms(),
        }

    def to_markdown(self) -> str:
        lines = [
            "# Debate Transcript",
            "",
            f"**Topic:** {self.topic}",
            f"**Started:** {self.started_at}",
            f"**Specialists:** {', '.join(self.specialists)}",
            f"**Moderator:** {self.moderator}",
            f"**Decisions to resolve:** {', '.join(self.decision_ids)}",
            "",
            "---",
            "",
        ]
        if self.research_packet:
            lines += [
                "## Pre-Debate Research Packet",
                f"*cost ${self.research_packet_cost_usd:.4f} — "
                f"duration {self.research_packet_duration_ms}ms*",
                "",
                self.research_packet,
                "",
                "---",
                "",
            ]
        for turn in self.turns:
            lines.append(f"## {turn.speaker} ({turn.role})")
            lines.append(f"*{turn.timestamp} — cost ${turn.cost_usd:.4f} — "
                         f"duration {turn.duration_ms}ms*")
            lines.append("")
            lines.append(turn.content)
            lines.append("")
        lines += ["---", "", "## Final Decisions", ""]
        for d in self.decisions_recorded:
            lines += [
                f"### {d.decision_id}",
                f"**Value:** {d.value}",
                "",
                f"**Rationale:** {d.rationale}",
                "",
                f"*Recorded at {d.timestamp}*",
                "",
            ]
        if self.end_summary:
            lines += ["", "## Moderator's Summary", "", self.end_summary, ""]
        lines += [
            "",
            f"**Total cost:** ${self.total_cost_usd():.4f}",
            f"**Total duration:** {self.total_duration_ms() / 1000:.1f}s",
        ]
        return "\n".join(lines)

    def decisions_md(self) -> str:
        lines = [
            "# Decisions",
            "",
            f"**Topic:** {self.topic}",
            f"**Date:** {self.started_at[:10]}",
            "",
        ]
        for d in self.decisions_recorded:
            lines += [
                f"## {d.decision_id}",
                "",
                f"**Decided:** {d.value}",
                "",
                f"**Rationale:** {d.rationale}",
                "",
            ]
        if self.end_summary:
            lines += ["## Summary", "", self.end_summary, ""]
        return "\n".join(lines)

    def transcript_for_prompt(self) -> str:
        """Render the running transcript for inclusion in a specialist/moderator prompt."""
        if not self.turns:
            return "(no turns yet — this is the start of the debate)"
        lines = []
        for turn in self.turns:
            lines.append(f"--- {turn.speaker} ---")
            lines.append(turn.content)
            lines.append("")
        return "\n".join(lines).strip()


    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        """Reconstruct a Transcript from a previously-saved transcript.json."""
        t = cls(
            topic=data["topic"],
            decisions=data.get("decision_ids", []),
            specialists=data.get("specialists", []),
            moderator=data.get("moderator", ""),
        )
        t.started_at = data.get("started_at", t.started_at)
        t.ended = data.get("ended", False)
        t.end_summary = data.get("end_summary", "")
        t.research_packet = data.get("research_packet", "") or ""
        t.research_packet_cost_usd = float(data.get("research_packet_cost_usd", 0.0))
        t.research_packet_duration_ms = int(data.get("research_packet_duration_ms", 0))
        for raw in data.get("turns", []):
            t.turns.append(Turn(**raw))
        for raw in data.get("decisions", []):
            t.decisions_recorded.append(Decision(**raw))
        return t

    def save(self, output_dir: Path):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "transcript.md").write_text(self.to_markdown(), encoding="utf-8")
        (output_dir / "transcript.json").write_text(
            json.dumps(self.to_dict(), indent=2), encoding="utf-8"
        )
        (output_dir / "decisions.md").write_text(self.decisions_md(), encoding="utf-8")
