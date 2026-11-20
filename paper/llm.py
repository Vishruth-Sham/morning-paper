"""Language-model access for planning and composing.

Providers, tried in order when the profile says "auto":
  1. Anthropic API via the official SDK (ANTHROPIC_API_KEY, or an `ant auth login` profile).
  2. The Claude Code CLI (`claude -p`), using whatever account it is signed in to.
  3. None: the pipeline falls back to rule-based selection and extractive briefs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

MODEL = os.environ.get("MORNING_PAPER_MODEL", "claude-opus-5-5")


class LLMUnavailable(Exception):
    pass


class LLM:
    def __init__(self, provider: str = "auto"):
        self.provider = provider
        self.name = "none"
        self._client = None
        self.last_error = ""
        if provider in ("auto", "anthropic"):
            try:
                import anthropic  # noqa: F401
                client = anthropic.Anthropic()
                if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or provider == "anthropic" \
                        or _has_ant_profile():
                    self._client = client
                    self.name = "anthropic"
            except Exception as e:  # missing credentials or package
                self.last_error = f"Anthropic API not configured: {e}"
        if self.name == "none" and provider in ("auto", "claude-cli") and shutil.which("claude"):
            self.name = "claude-cli"

    @property
    def available(self) -> bool:
        return self.name != "none"

    def json(self, system: str, prompt: str, schema: dict, max_tokens: int = 16000, effort: str = "medium") -> dict:
        """Return a JSON object matching `schema`. Raises LLMUnavailable on failure."""
        if self.name == "anthropic":
            return self._anthropic_json(system, prompt, schema, max_tokens, effort)
        if self.name == "claude-cli":
            return self._cli_json(system, prompt, schema)
        raise LLMUnavailable("No language model configured")

    def _anthropic_json(self, system, prompt, schema, max_tokens, effort):
        import anthropic
        try:
            with self._client.messages.stream(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                resp = stream.get_final_message()
        except anthropic.APIStatusError as e:
            self.last_error = f"Anthropic API error {e.status_code}: {e.message}"
            raise LLMUnavailable(self.last_error) from e
        except anthropic.APIConnectionError as e:
            self.last_error = f"Anthropic API connection error: {e}"
            raise LLMUnavailable(self.last_error) from e
        if resp.stop_reason == "refusal":
            self.last_error = "Model declined the request"
            raise LLMUnavailable(self.last_error)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)

    def _cli_json(self, system, prompt, schema):
        full = (f"{system}\n\n{prompt}\n\nRespond with only a JSON object (no prose, no code fences) that matches this JSON Schema:\n"
                f"{json.dumps(schema)}")
        try:
            out = subprocess.run(["claude", "-p", "--output-format", "json"], input=full, capture_output=True,
                                 text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.last_error = f"claude CLI failed: {e}"
            raise LLMUnavailable(self.last_error) from e
        if out.returncode != 0:
            detail = out.stderr.strip()
            if not detail:
                try:
                    detail = str(json.loads(out.stdout).get("result", ""))
                except (ValueError, AttributeError):
                    detail = out.stdout.strip()
            self.last_error = f"claude CLI exited {out.returncode}: {detail[:300]}"
            raise LLMUnavailable(self.last_error)
        try:
            envelope = json.loads(out.stdout)
            text = envelope.get("result", "") if isinstance(envelope, dict) else out.stdout
        except json.JSONDecodeError:
            text = out.stdout
        obj = extract_json(text)
        if obj is None:
            self.last_error = "claude CLI did not return JSON"
            raise LLMUnavailable(self.last_error)
        return obj


def extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _has_ant_profile() -> bool:
    return os.path.isdir(os.path.expanduser("~/.config/anthropic"))
