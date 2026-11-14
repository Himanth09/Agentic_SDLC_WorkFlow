"""Optional LLM provider layer.

The system is fully runnable *without* an LLM: every agent has a deterministic
implementation (heuristics + a blueprint catalogue). When a provider is
configured (`SDLC_LLM=anthropic|bedrock`), agents use it to *augment* their
work (extra ambiguity detection, generation for domains with no blueprint).

LLM output is never trusted directly:
  * it must parse as JSON and pass a caller-supplied validator,
  * it is retried at most `attempts` times with the validation error fed back,
  * on failure the caller falls back to its deterministic path,
  * any generated code still goes through the same guardrails + validator.
"""
# Store annotations as strings.
from __future__ import annotations

# json: parse the model's JSON answer.
import json
# os: read SDLC_LLM / SDLC_LLM_MODEL environment variables.
import os
# re: pull the JSON block out of a response that may contain other text.
import re
# Typing helpers: Protocol for the provider interface, TypeVar for the validator's return type.
from typing import Any, Callable, Protocol, TypeVar

# Whatever type the caller's validator returns.
T = TypeVar("T")


# Interface every provider implements (structural typing: no inheritance needed).
class LLMProvider(Protocol):
    # Provider name shown in the summary.
    name: str

    # Send a system prompt and user prompt; return the text reply.
    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> str: ...


# Provider backed by the Anthropic API.
class AnthropicProvider:
    name = "anthropic"

    # Create the client; the model comes from the argument, SDLC_LLM_MODEL, or a default.
    def __init__(self, model: str | None = None) -> None:
        import anthropic  # lazy: optional dependency
        # Reads ANTHROPIC_API_KEY from the environment.
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("SDLC_LLM_MODEL", "claude-sonnet-5-5")

    # Single-turn Messages API call.
    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> str:
        msg = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": prompt}])
        # Join only the text blocks of the reply.
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# Provider backed by Amazon Bedrock's Converse API.
class BedrockProvider:
    name = "bedrock"

    # Create the Bedrock runtime client; the model id must be given or set in SDLC_LLM_MODEL.
    def __init__(self, model: str | None = None) -> None:
        import boto3  # lazy: optional dependency
        # Uses the standard AWS credential chain and region.
        self.client = boto3.client("bedrock-runtime")
        # Raises KeyError if no model is configured.
        self.model = model or os.environ["SDLC_LLM_MODEL"]

    # Single-turn Converse call.
    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> str:
        resp = self.client.converse(
            modelId=self.model, system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens})
        # Join the text parts of the reply.
        return "".join(c.get("text", "") for c in resp["output"]["message"]["content"])


# Build a provider by name (or from SDLC_LLM); None means "run offline".
def get_provider(name: str | None) -> LLMProvider | None:
    # Fall back to the environment variable, defaulting to "none".
    name = name or os.environ.get("SDLC_LLM", "none")
    # Offline: agents use their deterministic paths only.
    if name in ("", "none", "offline"):
        return None
    if name == "anthropic":
        return AnthropicProvider()
    if name == "bedrock":
        return BedrockProvider()
    # Typos should fail loudly rather than silently run offline.
    raise ValueError(f"unknown LLM provider '{name}'")


# Greedy match of the outermost {...} or [...] block (re.S lets "." span newlines).
_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.S)


# Ask the LLM for JSON, validate it, and retry with the error message; None on failure.
def structured(provider: LLMProvider | None, system: str, prompt: str,
               validate: Callable[[Any], T], attempts: int = 2) -> T | None:
    """Ask for JSON, validate it, feed errors back once; None => caller falls back."""
    # No provider configured: the caller uses its fallback.
    if provider is None:
        return None
    # Error message from the previous attempt, appended to the next prompt.
    feedback = ""
    for _ in range(attempts):
        try:
            # Call the model, insisting on JSON only.
            raw = provider.complete(system, prompt + feedback +
                                    "\n\nRespond with JSON only, no prose.")
            # Find the JSON in the reply.
            match = _JSON_BLOCK.search(raw)
            if not match:
                raise ValueError("no JSON object in response")
            # Parse and run the caller's validator; success returns immediately.
            return validate(json.loads(match.group(0)))
        except Exception as exc:  # noqa: BLE001 - any failure => retry then fall back
            # Tell the model what was wrong for the next attempt.
            feedback = f"\n\nYour previous answer was rejected: {exc}. Fix it."
    # All attempts failed.
    return None
