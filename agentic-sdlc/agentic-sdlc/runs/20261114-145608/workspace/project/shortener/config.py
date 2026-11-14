"""Runtime settings.

Defaults were resolved from the requirement analysis (see ENGINEERING_SUMMARY.md,
"Assumptions"). Every value can be overridden with a SHORTENER_* environment
variable so the same build runs in dev, CI and production.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, fields, replace
from typing import Any, Mapping


@dataclass(frozen=True)
class Settings:
    # --- resolved from requirement analysis -------------------------------
    code_length: int = 7
    create_rate_per_min: int = 60
    require_api_key: bool = True
    analytics_ip_mode: str = "hash"          # "hash" | "drop"
    analytics_retention_days: int = 90
    default_ttl_days: int = 0               # 0 = links never expire by default
    allow_custom_alias: bool = True

    # --- operational -------------------------------------------------------
    base_url: str = "http://localhost:8080"
    db_path: str = "shortener.db"
    max_url_length: int = 2048
    max_body_bytes: int = 8192
    max_ttl_days: int = 3650
    cache_size: int = 10_000
    cache_ttl_s: float = 60.0
    negative_cache_ttl_s: float = 5.0
    analytics_batch_size: int = 500
    analytics_flush_interval_s: float = 1.0
    analytics_queue_size: int = 100_000
    api_keys: frozenset[str] = field(default_factory=frozenset)
    ip_hash_salt: str = field(default_factory=lambda: secrets.token_hex(16))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        base = cls()
        overrides: dict[str, Any] = {}
        for f in fields(cls):
            key = f"SHORTENER_{f.name.upper()}"
            if key not in env:
                continue
            raw = env[key]
            current = getattr(base, f.name)
            if isinstance(current, bool):
                overrides[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                overrides[f.name] = int(raw)
            elif isinstance(current, float):
                overrides[f.name] = float(raw)
            elif isinstance(current, frozenset):
                overrides[f.name] = frozenset(k.strip() for k in raw.split(",") if k.strip())
            else:
                overrides[f.name] = raw
        settings = replace(base, **overrides)
        settings.validate()
        return settings

    def validate(self) -> None:
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
