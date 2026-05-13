"""Parse specialist and moderator definitions from markdown files with YAML frontmatter."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml


def _parse_tools_allowed(raw) -> list[str]:
    """Accept either a list (yaml list) or a comma-separated string."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    raise ValueError(f"tools_allowed must be a list or comma-separated string, got {type(raw).__name__}")


@dataclass
class SpecialistConfig:
    name: str
    expertise: str
    system_prompt: str
    mode: str = "claude_code"
    model: str = "sonnet"
    tools_allowed: list[str] = field(default_factory=list)
    path: Path | None = None


@dataclass
class ModeratorConfig:
    name: str
    role: str
    system_prompt: str
    mode: str = "claude_code"
    model: str = "sonnet"
    tools_allowed: list[str] = field(default_factory=list)
    path: Path | None = None


def parse_markdown_with_frontmatter(path: Path) -> tuple[dict, str]:
    """Returns (frontmatter_dict, body_str). Raises if frontmatter missing."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter (file must start with ---)")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: malformed frontmatter (expected '---' delimiters)")
    frontmatter = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n").rstrip()
    return frontmatter, body


def load_specialist(path: Path) -> SpecialistConfig:
    fm, body = parse_markdown_with_frontmatter(path)
    if "name" not in fm:
        raise ValueError(f"{path}: missing required 'name' field in frontmatter")
    if "expertise" not in fm:
        raise ValueError(f"{path}: missing required 'expertise' field in frontmatter")
    return SpecialistConfig(
        name=fm["name"],
        expertise=fm["expertise"],
        system_prompt=body,
        mode=fm.get("mode", "claude_code"),
        model=fm.get("model", "sonnet"),
        tools_allowed=_parse_tools_allowed(fm.get("tools_allowed")),
        path=path,
    )


def load_specialists(specialists_dir: Path, names: list[str] | None = None) -> list[SpecialistConfig]:
    """Load specialists from a directory.

    - names=None or ["all"]: load every .md file not starting with '_'
    - names=["alice", "bob"]: load those specific specialists by filename stem
    """
    if not specialists_dir.exists():
        raise FileNotFoundError(f"Specialists directory not found: {specialists_dir}")
    all_files = sorted(p for p in specialists_dir.glob("*.md") if not p.name.startswith("_"))
    by_stem = {p.stem: p for p in all_files}
    if names is None or names == ["all"]:
        return [load_specialist(p) for p in all_files]
    out = []
    for n in names:
        if n not in by_stem:
            raise ValueError(
                f"Specialist '{n}' not found in {specialists_dir}.\n"
                f"Available: {sorted(by_stem.keys())}"
            )
        out.append(load_specialist(by_stem[n]))
    return out


def load_moderator(moderators_dir: Path, name: str) -> ModeratorConfig:
    if not moderators_dir.exists():
        raise FileNotFoundError(f"Moderators directory not found: {moderators_dir}")
    path = moderators_dir / f"{name}.md"
    if not path.exists():
        available = sorted(p.stem for p in moderators_dir.glob("*.md"))
        raise ValueError(
            f"Moderator '{name}' not found in {moderators_dir}.\n"
            f"Available: {available}"
        )
    fm, body = parse_markdown_with_frontmatter(path)
    return ModeratorConfig(
        name=fm.get("name", name),
        role=fm.get("role", ""),
        system_prompt=body,
        mode=fm.get("mode", "claude_code"),
        model=fm.get("model", "sonnet"),
        tools_allowed=_parse_tools_allowed(fm.get("tools_allowed")),
        path=path,
    )
