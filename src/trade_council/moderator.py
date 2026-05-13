"""Moderator — drives the debate, asks follow-ups, commits decisions."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from .config import ModeratorConfig
from .backends import get_backend, BackendResponse
from .builtin_decisions import BUILTIN_DECISIONS_GUIDANCE


@dataclass
class ModeratorAction:
    action: str          # 'follow_up' | 'decide' | 'end'
    payload: dict
    raw_response: str


STRUCTURED_OUTPUT_INSTRUCTION = """

YOUR TURN — STRUCTURED OUTPUT REQUIRED:

You may reason openly in narrative form. But your response MUST end with a
JSON code block declaring your action. Choose EXACTLY ONE of these schemas:

For a follow-up question to a specific specialist or to all of them:
```json
{
  "action": "follow_up",
  "to": "<specialist_name as listed under PARTICIPANTS, or 'all'>",
  "question": "<your follow-up question>"
}
```

To commit a decision (advances the agenda):
```json
{
  "action": "decide",
  "decision_id": "<exact id from DECISIONS TO RESOLVE>",
  "value": "<your committed answer to that decision>",
  "rationale": "<1-3 sentences of reasoning>"
}
```

To end the debate (only valid when ALL decisions are decided):
```json
{
  "action": "end",
  "summary": "<brief summary of the resolved decisions>"
}
```

The orchestrator parses the LAST json code block in your response. If your
response doesn't contain a valid JSON block, you'll be re-prompted.
""" + BUILTIN_DECISIONS_GUIDANCE

JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class Moderator:
    def __init__(self, config: ModeratorConfig):
        self.config = config
        self.backend = get_backend(config.mode)

    @property
    def name(self) -> str:
        return self.config.name

    def respond(self, user_prompt: str) -> tuple[BackendResponse, ModeratorAction]:
        full_prompt = user_prompt + STRUCTURED_OUTPUT_INSTRUCTION
        resp = self.backend.call(
            system_prompt=self.config.system_prompt,
            user_prompt=full_prompt,
            model=self.config.model,
            tools_allowed=self.config.tools_allowed,
        )
        action = self._parse_action(resp.text)
        return resp, action

    def _parse_action(self, text: str) -> ModeratorAction:
        matches = JSON_BLOCK_RE.findall(text)
        if not matches:
            raise ValueError(
                "Moderator response did not contain a ```json fenced block.\n"
                f"Response (first 2000 chars):\n{text[:2000]}"
            )
        for raw in reversed(matches):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("action") in {"follow_up", "decide", "end"}:
                return ModeratorAction(
                    action=payload["action"],
                    payload=payload,
                    raw_response=text,
                )
        raise ValueError(
            "Moderator response had JSON blocks but none matched the required schema.\n"
            f"Response (first 2000 chars):\n{text[:2000]}"
        )
