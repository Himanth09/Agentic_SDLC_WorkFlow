"""Domain knowledge interfaces.

A *domain pack* teaches the deterministic agents about a class of system:
how to recognise it, which capabilities and NFR dimensions matter, the
acceptance criteria per capability, architecture/trade-off/risk knowledge,
and a blueprint for generating code. Packs are data; the agents are generic.
Requirements outside every pack are still analysed, decomposed and planned,
and generation is delegated to an LLM provider or escalated to a human.
"""
# Store annotations as strings.
from __future__ import annotations

# re: keyword scoring, answer parsing and template placeholders.
import re
# dataclass/field: pack data structures.
from dataclasses import dataclass, field
# Path: blueprint directory.
from pathlib import Path
# Any/Callable: settings values and parser functions.
from typing import Any, Callable

# Models referenced by pack data.
from .models import AcceptanceCriterion, Risk, Severity, TradeOff


# A feature the system may have.
@dataclass
class Capability:
    # Capability id.
    id: str
    # Human-readable description.
    description: str
    # Regexes that mean the requirement mentions it.
    patterns: list[str]
    default: bool                       # included when the requirement does not mention it
    # Acceptance criteria added when the capability is included.
    criteria: list[AcceptanceCriterion]


# A non-functional or scope dimension the requirement may leave unspecified.
@dataclass
class Dimension:
    """An NFR / scope dimension that must be pinned down before building."""
    # Dimension id (Ambiguity.dimension refers to it).
    id: str
    # Title, e.g. "Scale".
    title: str
    # Clarifying question to ask.
    question: str
    # Default assumption as text.
    default: str
    answered_by: list[str]               # regexes that mean the requirement already specifies it
    triggered_by: list[str] = field(default_factory=list)  # vague words that raise severity
    # Name of the setting this dimension controls (None = informational only).
    setting: str | None = None
    # Parses a human answer into the setting's value (raises ValueError if invalid).
    parse: Callable[[str], Any] | None = None
    # Setting value used when the question isn't answered.
    default_value: Any = None


# A unit of generated code (or a test suite), mapped to one generator task.
@dataclass
class Component:
    # Task id that generates it.
    task_id: str
    # Task title.
    title: str
    # Blueprint files it renders.
    files: list[str]
    # Task ids it depends on.
    depends_on: list[str]
    # Why it is a separate task / ordered this way.
    rationale: str


# Everything the agents know about one domain.
@dataclass
class DomainPack:
    # Pack id, e.g. "url_shortener".
    id: str
    # Display title.
    title: str
    # Regex -> weight; the sum over matches is the recognition score.
    keywords: dict[str, float]
    # Capability catalogue.
    capabilities: list[Capability]
    # NFR/scope dimensions.
    dimensions: list[Dimension]
    # Code components to generate.
    components: list[Component]
    # Test suites to generate.
    test_suites: list[Component]
    # Architecture knowledge (style, components, flows, AWS mapping, capacity...).
    architecture: dict[str, Any]
    # Architecture decisions.
    tradeoffs: list[TradeOff]
    # Static risk register.
    risks: list[Risk]
    # Directory containing the *.tmpl blueprint files.
    blueprint_dir: Path
    bug_variants: dict[str, tuple[str, str, str]]    # task -> (file, find, replace)
    # Builds the OpenAPI contract from resolved settings.
    build_contract: Callable[[dict[str, Any]], dict[str, Any]]
    # Recipe id -> brownfield change recipe.
    brownfield_recipes: dict[str, Any] = field(default_factory=dict)

    # Recognition score: sum of weights of keyword regexes found in the text.
    def score(self, text: str) -> float:
        low = text.lower()
        return sum(w for kw, w in self.keywords.items() if re.search(kw, low))

    # Default setting values for every dimension that controls a setting.
    def default_settings(self) -> dict[str, Any]:
        return {d.setting: d.default_value for d in self.dimensions if d.setting}


# --------------------------------------------------------------------------- #
# Generic vagueness lexicon (domain independent)
# --------------------------------------------------------------------------- #
# Regex for vague wording -> the concept it relates to.
VAGUE_TERMS: dict[str, str] = {
    r"\bfast\b|\bquick(ly)?\b|\bperformant\b|\blow latency\b": "latency",
    r"\bscal(able|e|ability)\b|\bhigh traffic\b|\blots of users\b": "scale",
    r"\bsecure\b|\bsafe\b|\bsecurity\b": "security",
    r"\breliable\b|\balways (up|on)\b|\bhighly available\b|\brobust\b": "availability",
    r"\btracks? (stuff|things|everything)\b|\bsome (stats|metrics|analytics)\b": "analytics_scope",
    r"\bstuff\b|\bthings\b|\betc\.?\b|\bsome\b|\bvarious\b": "scope",
    r"\bsimple\b|\beasy\b|\bnice\b|\buser[- ]friendly\b|\bmodern\b": "subjective",
    r"\breal[- ]?time\b": "freshness",
    r"\basap\b|\bsoon\b": "timeline",
}

# Concept -> (question template with {term}, default assumption, severity) for concepts not tied to a dimension.
GENERIC_QUESTIONS: dict[str, tuple[str, str, Severity]] = {
    "scope": ("The requirement uses open-ended wording ('{term}'). What exactly is in scope?",
              "Scope is limited to the capabilities listed in the normalised requirement.",
              Severity.MEDIUM),
    "subjective": ("'{term}' is subjective. What observable property should it translate to?",
                   "Interpreted as: documented API, consistent errors, sensible defaults.",
                   Severity.LOW),
    "freshness": ("'{term}': what staleness is acceptable for derived data?",
                  "Analytics visible within ~1-5 seconds (asynchronous batch flush).",
                  Severity.LOW),
    "timeline": ("'{term}': is there a delivery deadline that should trade scope for time?",
                 "No fixed deadline; full scope delivered in one increment.", Severity.LOW),
}


# Parser factory: extract the first integer from an answer and require lo <= value <= hi.
def int_in(lo: int, hi: int) -> Callable[[str], int]:
    def parse(answer: str) -> int:
        # Commas removed so "10,000" parses as 10000.
        m = re.search(r"\d+", answer.replace(",", ""))
        if not m:
            raise ValueError(f"expected a number between {lo} and {hi}")
        v = int(m.group(0))
        if not lo <= v <= hi:
            raise ValueError(f"{v} outside [{lo}, {hi}]")
        return v
    return parse


# Parser factory: map an answer to True/False using regexes ("no" words are checked first).
def yes_no(yes_words: str, no_words: str) -> Callable[[str], bool]:
    def parse(answer: str) -> bool:
        low = answer.lower()
        if re.search(no_words, low):
            return False
        if re.search(yes_words, low):
            return True
        raise ValueError("expected a yes/no style answer")
    return parse


# Parser factory: return the value of the first regex in `options` that matches the answer.
def choice(options: dict[str, str]) -> Callable[[str], str]:
    def parse(answer: str) -> str:
        low = answer.lower()
        for pattern, value in options.items():
            if re.search(pattern, low):
                return value
        raise ValueError(f"expected one of {list(options.values())}")
    return parse


# Registered packs by id.
_REGISTRY: dict[str, DomainPack] = {}


# Add a pack to the registry (called by each pack module when imported).
def register(pack: DomainPack) -> None:
    _REGISTRY[pack.id] = pack


# All packs; imports the built-in packs the first time it is called.
def packs() -> list[DomainPack]:
    if not _REGISTRY:
        from .blueprints import url_shortener  # noqa: F401  (registers itself)
    return list(_REGISTRY.values())


# Look up a pack by id (None if unknown).
def get_pack(domain_id: str) -> DomainPack | None:
    # Make sure the built-in packs are loaded.
    packs()
    return _REGISTRY.get(domain_id)


# Look up a pack by id, escalating to a human if it doesn't exist.
def require_pack(domain_id: str) -> DomainPack:
    pack = get_pack(domain_id)
    if pack is None:
        # Local import avoids a circular import (errors -> models).
        from .errors import NeedsHuman
        raise NeedsHuman(f"no domain pack for '{domain_id}'")
    return pack


# Template placeholder syntax: {{ name }}.
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# Render blueprint file `<blueprint_dir>/<rel>.tmpl`, replacing {{ setting }} placeholders.
def render(pack: DomainPack, rel: str, settings: dict[str, Any]) -> str:
    """Render a blueprint file. Every placeholder must resolve (no silent blanks)."""
    text = (pack.blueprint_dir / f"{rel}.tmpl").read_text(encoding="utf-8")

    # Replacement callback: a missing setting is an error, never an empty string.
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in settings:
            raise KeyError(f"blueprint {rel} needs setting '{key}'")
        return str(settings[key])

    return _PLACEHOLDER.sub(sub, text)
