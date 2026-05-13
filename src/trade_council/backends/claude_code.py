"""Claude Code subprocess backend — uses `claude -p` and subscription auth."""
from __future__ import annotations
import subprocess
import json
import os
import time
from .base import Backend, BackendResponse


# Full set of Claude Code tools. Anything in this set that is NOT in
# `tools_allowed` for a given call gets passed via `--disallowedTools`.
ALL_TOOLS = (
    "Bash", "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep", "WebSearch", "WebFetch",
    "Task", "TodoWrite", "ExitPlanMode",
)


class ClaudeCodeBackend(Backend):
    """Invokes `claude -p` as a subprocess. Uses subscription auth (no API key)."""

    def __init__(self, claude_path: str = "claude", max_stdin_mb: int = 9,
                 timeout_sec: int = 600,
                 max_turns_no_tools: int = 1,
                 max_turns_with_tools: int = 10):
        self.claude_path = claude_path
        self.max_stdin_bytes = max_stdin_mb * 1024 * 1024
        self.timeout_sec = timeout_sec
        self.max_turns_no_tools = max_turns_no_tools
        self.max_turns_with_tools = max_turns_with_tools

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str = "sonnet",
        tools_allowed: list[str] | None = None,
    ) -> BackendResponse:
        # Combined-prompt cap (defensive; stdin itself has a 10MB ceiling in claude -p).
        total_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
        if total_bytes > self.max_stdin_bytes:
            raise RuntimeError(
                f"Combined prompt too large ({total_bytes / 1024 / 1024:.1f} MB > "
                f"{self.max_stdin_bytes / 1024 / 1024:.1f} MB cap). "
                f"Reduce transcript length or split the debate."
            )

        allowed = set(tools_allowed or [])
        disallowed = [t for t in ALL_TOOLS if t not in allowed]
        max_turns = self.max_turns_with_tools if allowed else self.max_turns_no_tools

        # The user_prompt is piped via stdin (not argv) so it can grow arbitrarily
        # large without hitting the Windows ~32KB CreateProcess command-line limit.
        # The system_prompt stays on argv — it's per-specialist and small (<10KB).
        cmd = [
            self.claude_path,
            "-p",
            "--system-prompt", system_prompt,
            "--max-turns", str(max_turns),
            "--disallowedTools", ",".join(disallowed),
            "--output-format", "json",
            "--model", model,
        ]
        # Pre-approve the allowed tools so `claude -p` doesn't try to prompt
        # interactively (there's no TTY in subprocess mode, which surfaces as
        # permission denials).
        if allowed:
            cmd += ["--allowedTools", ",".join(sorted(allowed))]

        # Strip ANTHROPIC_API_KEY from subprocess env so subscription auth is used.
        # Per Claude Code docs, having the env var set may override subscription billing.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, env=env,
                timeout=self.timeout_sec,
                encoding="utf-8", errors="replace",
                input=user_prompt,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"claude -p timed out after {self.timeout_sec}s. "
                f"Either the prompt is too complex or there's a network issue."
            )
        wall_duration_ms = int((time.monotonic() - start) * 1000)

        if result.returncode != 0:
            raise RuntimeError(
                f"claude -p exited with code {result.returncode}.\n"
                f"STDERR:\n{result.stderr.strip()}\n"
                f"STDOUT (first 2000 chars):\n{result.stdout[:2000]}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Could not parse claude -p JSON output: {e}\n"
                f"STDOUT (first 2000 chars):\n{result.stdout[:2000]}"
            )

        if payload.get("type") != "result":
            raise RuntimeError(
                f"Unexpected claude -p response type: {payload.get('type')!r}\n"
                f"Full payload: {json.dumps(payload, indent=2)[:1000]}"
            )

        return BackendResponse(
            text=payload.get("result", ""),
            cost_usd=float(payload.get("total_cost_usd", 0.0)),
            duration_ms=int(payload.get("duration_ms", wall_duration_ms)),
            metadata={
                "raw": payload,
                "wall_duration_ms": wall_duration_ms,
                "tools_allowed": sorted(allowed),
                "max_turns_used": max_turns,
            },
        )
