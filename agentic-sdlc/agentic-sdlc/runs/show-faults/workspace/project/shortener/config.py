"""Runtime settings.

Defaults were resolved from the requirement analysis (see ENGINEERING_SUMMARY.md,
"Assumptions"). Every value can be overridden with a SHORTENER_* environment
variable so the same build runs in dev, CI and production.
"""
from __future__ import annotations

# os: read SHORTENER_* environment variables.
import os
# secrets: generate a random per-process salt when none is configured.
import secrets
# dataclass helpers: immutable settings, field introspection, copy-with-overrides.
from dataclasses import dataclass, field, fields, replace
from typing import Any, Mapping


# Immutable (frozen) so settings can't change while the service is running.
@dataclass(frozen=True)
class Settings:
    # The values in this block were filled in by the generator from the analysed requirement
    # (and any human answers given at the requirements gate).
    # --- resolved from requirement analysis -------------------------------
    # Characters per generated short code.
    code_length: int = 7
    # Link creations allowed per client per minute.
    create_rate_per_min: int = 60
    # Whether write endpoints need an API key.
    require_api_key: bool = True
    # How visitor IPs are handled in analytics.
    analytics_ip_mode: str = "hash"          # "hash" | "drop"
    # Days raw click events are kept before purging (daily totals are kept).
    analytics_retention_days: int = 90
    # Expiry applied to new links that don't specify one.
    default_ttl_days: int = 0               # 0 = links never expire by default
    # Whether clients may choose their own code (alias).
    allow_custom_alias: bool = True

    # Infrastructure and safety limits, independent of the requirement.
    # --- operational -------------------------------------------------------
    # Public origin used to build short URLs in responses.
    base_url: str = "http://localhost:8080"
    # SQLite database file.
    db_path: str = "shortener.db"
    # Longest target URL accepted.
    max_url_length: int = 2048
    # Largest request body accepted (protects against oversized payloads).
    max_body_bytes: int = 8192
    # Longest expiry a client may request (about 10 years).
    max_ttl_days: int = 3650
    # Redirect cache: entry count, positive TTL and negative (unknown code) TTL.
    cache_size: int = 10_000
    cache_ttl_s: float = 60.0
    negative_cache_ttl_s: float = 5.0
    # Analytics recorder: batch size, flush interval and queue bound.
    analytics_batch_size: int = 500
    analytics_flush_interval_s: float = 1.0
    analytics_queue_size: int = 100_000
    # Accepted API keys (comma-separated in SHORTENER_API_KEYS); empty by default.
    api_keys: frozenset[str] = field(default_factory=frozenset)
    # Salt for IP hashing; random per process unless SHORTENER_IP_HASH_SALT is set.
    ip_hash_salt: str = field(default_factory=lambda: secrets.token_hex(16))

    # Build settings from defaults plus SHORTENER_<FIELD> overrides, then validate them.
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        # Tests pass a dict; production reads the real environment.
        env = os.environ if env is None else env
        base = cls()
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            key = f"SHORTENER_{f.name.upper()}"
            if key not in env:
                continue
            raw = env[key]
            # Parse the string according to the type of the field's default.
            current = getattr(base, f.name)
            # bool is checked before int because bool is a subclass of int.
            if isinstance(current, bool):
                overrides[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                overrides[f.name] = int(raw)
            elif isinstance(current, float):
                overrides[f.name] = float(raw)
            elif isinstance(current, frozenset):
                # Comma-separated list; blanks are ignored.
                overrides[f.name] = frozenset(k.strip() for k in raw.split(",") if k.strip())
            else:
                overrides[f.name] = raw
        # Frozen dataclass: build a new instance with the overrides applied.
        settings = replace(base, **overrides)
        settings.validate()
        return settings

    # Reject configurations that would make the service misbehave, at startup rather than later.
    def validate(self) -> None:
        # Too short is guessable or collision-prone; too long defeats the purpose.
        if not 5 <= self.code_length <= 12:
            raise ValueError("code_length must be between 5 and 12")
        if self.analytics_ip_mode not in ("hash", "drop"):
            raise ValueError("analytics_ip_mode must be 'hash' or 'drop'")
        if self.create_rate_per_min <= 0:
            raise ValueError("create_rate_per_min must be positive")
        if self.require_api_key and not self.api_keys:
            # Fail closed: a deployment that requires keys but has none configured
            # would otherwise reject every write with no obvious reason.
            raise ValueError("require_api_key is set but SHORTENER_API_KEYS is empty")
