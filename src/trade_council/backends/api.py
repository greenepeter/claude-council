"""Anthropic API backend — direct SDK calls. v2.0 Phase 2."""
from __future__ import annotations
import os
import time
from .base import Backend, BackendResponse


# Per-1M-token USD pricing. Update if Anthropic pricing changes.
MODEL_PRICING = {
    "claude-sonnet-4-6":     {"in": 3.00,  "out": 15.00},
    "claude-opus-4-6":       {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00},
}

# Short aliases -> full model strings
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}


class APIBackend(Backend):
    """Direct Anthropic Messages API. Use this for autonomous operation
    (no subscription rate limits, deterministic latency, true async-friendly).

    v2.0 limitations:
    - tools_allowed parameter accepted but currently ignored. Specialists in
      API mode reason from the prompt + research packet only. The research
      packet is built by Claude Code (subscription) so still gets fresh web
      data. Per-specialist web search in API mode is a v2.1+ feature (would
      wire the native anthropic web_search_20250305 tool).
    """

    def __init__(self, default_max_tokens: int = 4096, timeout_sec: int = 300):
        try:
            import anthropic  # noqa: F401 - import-check only
        except ImportError as e:
            raise RuntimeError(
                "API backend requires the `anthropic` SDK. Install with: "
                "pip install anthropic\n"
                f"Underlying error: {e}"
            )
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "API backend requires ANTHROPIC_API_KEY env var. "
                "Set it in .env (gitignored) or your shell environment."
            )
        from anthropic import Anthropic
        # max_retries=20 lets the SDK ride out Tier 1 rate-limit windows
        # (30K input TPM on Sonnet). The SDK respects the `retry-after` header
        # Anthropic returns on 429s, so each retry sleeps for the suggested
        # duration. Once the org auto-tiers up after ~$100 of spend, this
        # becomes mostly inert — retries only kick in on genuine bursts.
        self.client = Anthropic(api_key=api_key, timeout=timeout_sec, max_retries=20)
        self.default_max_tokens = default_max_tokens

    def call(self, system_prompt: str, user_prompt: str, model: str = "sonnet",
             tools_allowed: list[str] | None = None) -> BackendResponse:
        resolved_model = MODEL_ALIASES.get(model, model)

        # Map our generic tool names onto Anthropic's server-side tools.
        # WebSearch -> web_search_20250305 (server-managed, no client-side loop needed).
        # WebFetch -> no native equivalent in the API. Specialists requesting WebFetch
        #             in api mode get a no-op; the research packet covers most needs.
        tools = []
        if tools_allowed:
            if "WebSearch" in tools_allowed:
                tools.append({"type": "web_search_20250305", "name": "web_search"})

        create_kwargs = dict(
            model=resolved_model,
            max_tokens=self.default_max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        if tools:
            create_kwargs["tools"] = tools

        start = time.monotonic()
        try:
            resp = self.client.messages.create(**create_kwargs)
        except Exception as e:
            raise RuntimeError(f"Anthropic API call failed: {type(e).__name__}: {e}")
        duration_ms = int((time.monotonic() - start) * 1000)

        # Extract text from content blocks. With server-side tools, the response
        # may include tool_use / tool_result blocks too; we skip those and keep
        # only the model's narrative text.
        text_parts = []
        for block in resp.content:
            if hasattr(block, "text") and getattr(block, "type", "text") == "text":
                text_parts.append(block.text)
        text = "".join(text_parts)

        # Compute cost from usage
        usage = resp.usage
        in_tokens = getattr(usage, "input_tokens", 0)
        out_tokens = getattr(usage, "output_tokens", 0)
        pricing = MODEL_PRICING.get(resolved_model, {"in": 0.0, "out": 0.0})
        cost_usd = (in_tokens * pricing["in"] + out_tokens * pricing["out"]) / 1_000_000

        return BackendResponse(
            text=text,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            metadata={
                "model": resolved_model,
                "input_tokens": in_tokens,
                "output_tokens": out_tokens,
                "stop_reason": getattr(resp, "stop_reason", None),
            },
        )
