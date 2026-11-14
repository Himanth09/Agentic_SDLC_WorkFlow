"""Domain pack: URL shortener (links, redirects, click analytics)."""
# Store annotations as strings.
from __future__ import annotations

# Path: locate the blueprint template directory next to this file.
from pathlib import Path

# Pack data types, answer-parser factories and the registry hook.
from ...knowledge import Capability, Component, Dimension, DomainPack, choice, int_in, register, yes_no
# Acceptance criterion, aliased to AC to keep the tables compact.
from ...models import AcceptanceCriterion as AC
# Risk and trade-off records.
from ...models import Risk, TradeOff
# Brownfield change recipes (alias-overwrite fix, link expiration...).
from .brownfield import RECIPES
# Builder for the OpenAPI contract, parameterised by settings.
from .contract import build_openapi

# Capability catalogue: (id, description, regexes that mention it, on-by-default?, acceptance criteria).
CAPABILITIES = [
    # Create short links.
    Capability("create_link", "Create a short link for a long URL", [r"shorten", r"short (link|url)", r"create"],
               True, [AC("AC-CREATE", "POST /api/v1/links with a valid http(s) URL returns 201 with a "
                                      "unique short code and short_url")]),
    # Redirect from a code to its target.
    Capability("redirect", "Resolve a short code and redirect (HTTP 302)", [r"redirect", r"resolve"], True,
               [AC("AC-REDIRECT", "GET /{code} returns 302 with Location set to the original URL; "
                                  "unknown codes return 404")]),
    # Reject dangerous/malformed target URLs.
    Capability("url_safety", "Reject unsafe or malformed target URLs", [r"valid", r"safe", r"secur"], True,
               [AC("AC-VALIDATE", "Non-http(s), credential-bearing, local or private-network target "
                                  "URLs are rejected with 422", "security")]),
    # Vanity aliases; a taken alias must never be overwritten.
    Capability("custom_alias", "Optional custom alias (vanity code)", [r"alias", r"vanity", r"custom (code|slug|name)"],
               True, [AC("AC-ALIAS", "A custom alias can be requested; a taken alias returns 409 and "
                                     "never overwrites the existing link")]),
    # Optional TTL; expired links return 410.
    Capability("expiration", "Optional link expiry (TTL)", [r"expir", r"\bttl\b", r"time[- ]limited"], True,
               [AC("AC-EXPIRY", "Links may carry a TTL; expired links return 410 Gone")]),
    # Links and analytics survive restarts.
    Capability("persistence", "Durable storage of links and analytics",
               [r"persist", r"stor(e|age)", r"database", r"durab"], True,
               [AC("AC-PERSIST", "Links and analytics survive a process restart")]),
    # Click analytics plus privacy requirements.
    Capability("analytics", "Click analytics per link", [r"analytic", r"track", r"click", r"stat", r"metric"], True,
               [AC("AC-ANALYTICS", "Every redirect records a click; the stats endpoint returns totals, "
                                   "per-day counts, top referrers and user-agent classes"),
                AC("AC-PRIVACY", "Raw client IPs and full referrer URLs are never stored; raw events "
                                 "are purgeable after the retention window", "security")]),
    # Concurrency safety and a cache-first redirect path.
    Capability("scalability", "Horizontal scalability of the redirect path", [r"scal", r"high traffic", r"million"],
               True, [AC("AC-CONCURRENCY", "Code allocation is collision-safe under concurrent writers "
                                           "(no duplicate codes, no lost or overwritten links)", "nfr"),
                      AC("AC-HOTPATH", "Redirects are served from cache after first access and analytics "
                                       "recording never blocks or fails a redirect", "nfr")]),
    # Rate limiting and API-key protection.
    Capability("abuse_protection", "Rate limiting and API-key protection of write endpoints",
               [r"abuse", r"rate.?limit", r"secur", r"api key", r"auth"], True,
               [AC("AC-ABUSE", "Link creation is rate limited per client (429 + Retry-After); write "
                               "endpoints require an API key when configured; only owners can disable "
                               "a link", "security")]),
    # Versioned REST API matching the OpenAPI contract.
    Capability("api", "Versioned REST API with a published contract", [r"\bapis?\b", r"rest", r"endpoint"], True,
               [AC("AC-CONTRACT", "The HTTP API conforms exactly to the published OpenAPI contract; "
                                  "errors use application/problem+json")]),
]

# NFR/scope dimensions: (id, title, question, default text, regexes meaning 'already specified',
# vague concepts that raise severity, [setting name, answer parser, default value]).
DIMENSIONS = [
    # Throughput and volume targets (informational, no setting).
    Dimension("scale", "Scale targets",
              "What peak redirect throughput and total link volume must the service support?",
              "Design target 10k redirects/s peak, 100 creates/s, ~1B links over 5 years "
              "(read:write ~ 100:1). The prototype is sized for one node; the design scales out.",
              [r"\d[\d,.]*\s*(k|m|million|billion|thousand)?\s*(rps|qps|req|requests|redirects|links|urls)"],
              ["scale"]),
    # Redirect latency objective.
    Dimension("latency", "Latency objective",
              "What latency objective applies to redirects?",
              "p99 redirect latency < 50 ms inside the service (cache hit < 5 ms).",
              [r"\d+\s*ms\b", r"\bp9\d\b"], ["latency"]),
    # Availability objective.
    Dimension("availability", "Availability objective",
              "What availability target applies, and may link creation degrade before redirects?",
              "99.95% for redirects; creation and analytics may degrade first (analytics is "
              "best-effort and must never block redirects).",
              [r"99\.\d+", r"\bslo\b", r"\bsla\b", r"uptime"], ["availability"]),
    # Write access control -> setting require_api_key (yes/no).
    Dimension("auth", "Write access control",
              "Who may create and delete links? Is an API key required for write endpoints?",
              "Write endpoints require an API key (X-API-Key); redirects and stats are public.",
              [r"api key", r"oauth", r"\bauth", r"login", r"public api"], ["security"],
              setting="require_api_key",
              parse=yes_no(r"\b(yes|key|required|require|auth)", r"\b(no auth|public|open|anonymous|no)\b"),
              default_value=True),
    # Analytics privacy -> setting analytics_ip_mode ("hash" or "drop").
    Dimension("privacy", "Analytics privacy",
              "Which visitor data may analytics store? (raw IPs are personal data under GDPR)",
              "Never store raw IPs: keep a salted, daily-rotating hash for unique-visitor counts.",
              [r"gdpr", r"privacy", r"\bpii\b", r"ip address", r"anonymi"], ["security", "analytics_scope"],
              setting="analytics_ip_mode",
              parse=choice({r"drop|no ip|don'?t store|do not store|none": "drop", r"hash|anonymi": "hash"}),
              default_value="hash"),
    # Raw event retention -> setting analytics_retention_days (1..3650).
    Dimension("retention", "Analytics retention",
              "How long must raw click events be retained?",
              "Raw click events 90 days; daily aggregates kept for the life of the link.",
              [r"retention", r"retain", r"\d+\s*days? of"], ["analytics_scope"],
              setting="analytics_retention_days", parse=int_in(1, 3650), default_value=90),
    # Which analytics dimensions are required (informational).
    Dimension("analytics_scope", "Analytics dimensions",
              "Which analytics are required (clicks, uniques, referrers, devices, geography)?",
              "Total and daily clicks, unique visitors, top referrer hosts, user-agent class. "
              "Geo-IP is out of scope for v1 (needs a licensed database).",
              [r"referr", r"geo", r"country", r"device"], ["analytics_scope"]),
    # Abuse limits -> setting create_rate_per_min (1..100000).
    Dimension("rate_limit", "Abuse limits",
              "What creation rate per client is acceptable before throttling?",
              "60 link creations per minute per API key (or per IP when keys are disabled).",
              [r"rate.?limit", r"throttl", r"quota", r"per minute"], ["security"],
              setting="create_rate_per_min", parse=int_in(1, 100_000), default_value=60),
    # Short-code length -> setting code_length (5..12).
    Dimension("code_length", "Short-code format",
              "How long may short codes be, and must they be unguessable?",
              "7-character random base62 codes (62^7 = 3.5e12), unguessable, no sequential IDs.",
              [r"code length", r"\d+[- ]char"], [],
              setting="code_length", parse=int_in(5, 12), default_value=7),
    # Default expiry -> setting default_ttl_days (0 = never).
    Dimension("expiry_default", "Default expiry",
              "Should links expire by default? If so after how many days?",
              "Links never expire unless the caller sets ttl_seconds.",
              [r"expire after", r"default (ttl|expir)"], [],
              setting="default_ttl_days", parse=int_in(0, 3650), default_value=0),
    # Custom aliases on/off -> setting allow_custom_alias.
    Dimension("aliases", "Custom aliases",
              "Are caller-chosen custom aliases allowed?",
              "Yes - custom aliases allowed, first-come-first-served, never overwritten.",
              [r"alias", r"vanity"], [],
              setting="allow_custom_alias",
              parse=yes_no(r"\b(yes|allow|enable|support)", r"\b(no|disallow|disable|not)\b"),
              default_value=True),
]

# Code components = codegen tasks: (task id, title, blueprint files, dependencies, rationale).
COMPONENTS = [
    # Pure domain core (no I/O).
    Component("gen_domain", "Domain model, validation, short-code generation, config, errors",
              ["shortener/__init__.py", "shortener/config.py", "shortener/errors.py",
               "shortener/domain.py", "shortener/codes.py"], ["design_contract"],
              "Pure core with no I/O; everything else depends on it."),
    # Storage layer.
    Component("gen_persistence", "Link repository port + SQLite/in-memory adapters",
              ["shortener/repository.py"], ["gen_domain"],
              "Implements the data schema from design_contract; uniqueness enforced by storage."),
    # Asynchronous click analytics.
    Component("gen_analytics", "Async click pipeline + analytics store",
              ["shortener/analytics.py"], ["gen_persistence"],
              "Shares the database handle; decoupled from redirects by a bounded queue."),
    # Cache and rate limiter (parallel with persistence).
    Component("gen_edge", "Hot-path cache and rate limiter",
              ["shortener/cache.py", "shortener/ratelimit.py"], ["gen_domain"],
              "Independent of storage: generated in parallel with persistence/analytics."),
    # Business rules composing the ports.
    Component("gen_service", "Application service (business rules)",
              ["shortener/service.py"], ["gen_persistence", "gen_analytics", "gen_edge"],
              "Composes the ports; the only place business rules live."),
    # HTTP adapter + entry point + packaging.
    Component("gen_api", "HTTP adapter implementing the OpenAPI contract + composition root",
              ["shortener/http.py", "shortener/main.py", "pyproject.toml"], ["gen_service", "design_contract"],
              "Must match the contract exactly; validated by contract tests."),
]

# Test suites = testgen tasks (only depend on the contract, so they run in parallel with codegen).
TEST_SUITES = [
    # Unit tests.
    Component("gen_unit_tests", "Unit tests (domain, codes, cache, rate limiter, service, analytics)",
              ["tests/__init__.py", "tests/support.py", "tests/conftest.py", "tests/unit/__init__.py",
               "tests/unit/test_domain_and_codes.py", "tests/unit/test_cache_and_ratelimit.py",
               "tests/unit/test_service.py", "tests/unit/test_analytics_recorder.py"],
              ["design_contract"], "Written from acceptance criteria + contract, in parallel with code."),
    # Black-box integration/contract/concurrency tests.
    Component("gen_integration_tests", "Integration, contract, persistence and concurrency tests",
              ["tests/integration/__init__.py", "tests/integration/test_api.py",
               "tests/integration/test_contract.py",
               "tests/integration/test_persistence_and_concurrency.py"],
              ["design_contract"], "Black-box against the contract; real SQLite; real threads."),
]

# Architecture knowledge used by the Architect (doc) and RiskAnalyst (failure scenarios).
ARCHITECTURE = {
    # Overall architectural style.
    "style": "Hexagonal (ports & adapters) service; stateless app tier; storage behind ports",
    # (component, responsibility) rows.
    "components": [
        ("HTTP adapter (http.py)", "Routing per OpenAPI contract, auth, rate limiting, problem+json errors"),
        ("LinkService (service.py)",
         "Business rules: validation, code allocation with collision retry, expiry, ownership"),
        ("Code generator (codes.py)", "CSPRNG base62 codes; no coordinator needed across instances"),
        ("LinkRepository port (repository.py)",
         "Atomic insert-if-absent; SQLite adapter (dev) / DynamoDB conditional put (prod)"),
        ("TTLCache (cache.py)", "In-process LRU+TTL with negative caching; ElastiCache in prod"),
        ("ClickRecorder (analytics.py)",
         "Bounded queue + batch flusher; never blocks redirects; Kinesis/Firehose in prod"),
        ("AnalyticsStore", "Raw events (retention-bounded) + pre-aggregated daily counters"),
        ("TokenBucketLimiter (ratelimit.py)", "Per-client write throttling; API Gateway usage plans / Redis in prod"),
    ],
    # Step-by-step request flows.
    "flows": {
        "create": "client -> POST /api/v1/links -> auth -> rate limit -> validate URL/alias/TTL -> "
                  "allocate code -> repository.insert (unique constraint) -> retry on collision (<=5) -> 201",
        "redirect": "client -> GET /{code} -> cache (positive/negative) -> repository.get -> "
                    "expired? 410 : disabled? 404 -> enqueue ClickEvent (non-blocking) -> 302",
        "analytics": "flusher thread -> batch insert raw events + upsert daily counters (one transaction) -> "
                     "GET /stats reads aggregates + bounded raw window",
    },
    # How each concern maps to AWS in production.
    "aws_mapping": [
        ("Edge", "CloudFront + WAF (bot/abuse rules) -> ALB"),
        ("Compute", "ECS Fargate service (stateless), autoscaled on p99 latency and CPU"),
        ("Links store", "DynamoDB on-demand, PK=code, ConditionExpression attribute_not_exists(code)"),
        ("Cache", "ElastiCache (Redis) read-through, 60s TTL, negative entries 5s"),
        ("Click pipeline", "Kinesis Data Streams -> Firehose -> S3 (Parquet) -> Athena; Lambda consumer "
                           "for DynamoDB daily counters (atomic ADD)"),
        ("Rate limiting", "API Gateway usage plans or Redis token buckets"),
        ("Observability", "CloudWatch metrics: redirect p99, cache hit ratio, analytics backlog/drops, 5xx"),
    ],
    # (failure, designed behaviour) pairs.
    "failure_scenarios": [
        ("Link store unavailable", "Cached links keep redirecting; uncached -> 5xx; creates fail fast. "
         "Prod: multi-AZ DynamoDB, alarms on throttles/errors."),
        ("Analytics store slow/down", "Redirects unaffected (bounded queue); events dropped and counted "
         "on /healthz; flusher survives write errors."),
        ("Code collision storm", "Bounded retry (5) then 503 code_space_exhausted - signals code length "
         "is too short for volume instead of looping."),
        ("Duplicate alias race", "Storage uniqueness constraint arbitrates; exactly one writer wins, "
         "others get 409 - verified by a concurrent test."),
        ("Link disabled while cached on other nodes", "Local cache is invalidated immediately; other "
         "nodes converge within cache TTL (60s). Prod: publish invalidations or lower TTL."),
        ("Abusive client", "Per-key token bucket returns 429 with Retry-After; owner can disable links."),
        ("Process crash", "Up to one flush interval of click events lost (in-memory buffer); links are "
         "durable. Prod: durable stream (Kinesis) closes the gap."),
    ],
    # Back-of-envelope capacity estimates.
    "capacity": [
        "1B links x ~500 B/item ~ 0.5 TB in DynamoDB; 10k redirects/s ~ 10k RCU-equivalents before cache",
        "With a 90% cache hit ratio the store sees ~1k reads/s; 100 creates/s is trivial for conditional puts",
        "Click events: 10k/s x ~150 B ~ 1.5 MB/s -> 2 Kinesis shards with headroom",
    ],
}

# Architecture decisions (rendered as ADRs): (decision, chosen, alternatives, rationale).
TRADEOFFS = [
    # How short codes are allocated.
    TradeOff("Short-code allocation", "Random base62 (CSPRNG) + uniqueness constraint + bounded retry",
             "Global counter + base62; hash(url) truncation; pre-allocated key ranges (KGS)",
             "No coordinator or hot counter, codes are unguessable (no enumeration), collisions are "
             "detectable atomically by the store. Cost: one extra write attempt in ~1e-3 cases at 1B links."),
    # 302 vs 301 redirects.
    TradeOff("Redirect status code", "302 Found", "301 Moved Permanently",
             "301 is cached by browsers forever, so clicks after the first are invisible to analytics "
             "and disabling/expiring a link would not take effect. 302 costs one round-trip per click."),
    # Async vs sync analytics writes.
    TradeOff("Analytics write path", "Asynchronous bounded queue, batch writes, drop on overload",
             "Synchronous write per redirect; exactly-once streaming",
             "Redirect latency/availability is the product; analytics is best-effort. Drops are "
             "counted and exposed on /healthz so loss is observable, not silent."),
    # Raw events vs aggregates.
    TradeOff("Analytics storage model", "Raw events (bounded retention) + pre-aggregated daily counters",
             "Raw events only; aggregates only",
             "Totals stay O(days) to query and survive raw-event purging (privacy/retention); raw "
             "window still supports referrer/UA breakdowns."),
    # Key-value vs relational store.
    TradeOff("Primary store", "Key-value semantics behind a port (SQLite in prototype, DynamoDB in prod)",
             "Relational DB (Aurora) with sharding", "Access pattern is single-key get/put at very high "
             "read volume; KV gives predictable latency and horizontal scale without sharding logic."),
    # IP handling.
    TradeOff("Visitor privacy", "Salted daily-rotating IP hash (or drop)", "Store raw IPs",
             "Unique-visitor counts without retaining personal data or enabling cross-day tracking."),
    # Web framework choice.
    TradeOff("Framework", "Standard-library WSGI adapter", "FastAPI/Starlette",
             "Zero runtime dependencies for the prototype and fully in-process tests; the adapter is "
             "thin (one module) and can be swapped without touching service or storage code."),
]

# Static risk register: (id, title, likelihood, impact, mitigation, category).
RISKS = [
    # Phishing/malware abuse.
    Risk("R-ABUSE", "Service used for phishing / malware redirection", "high", "high",
         "URL safety validation, API keys, per-key rate limits, owner disable; add Safe Browsing "
         "lookup + WAF bot rules in production", "security"),
    # Guessable codes.
    Risk("R-ENUM", "Enumeration of short codes exposing private links", "medium", "medium",
         "Random 7+ char codes (3.5e12 space), negative caching, WAF rate rules on 404 bursts", "security"),
    # Hot keys / cache stampede.
    Risk("R-HOTKEY", "Viral link creates a hot partition / cache stampede", "medium", "high",
         "Read-through cache in front of the store; DynamoDB adaptive capacity; request coalescing "
         "(future) for cold misses", "scalability"),
    # Analytics loss.
    Risk("R-ANALYTICS-LOSS", "Click events lost under overload or crash (in-memory buffer)", "medium", "low",
         "Bounded queue with drop counters on /healthz; flush on SIGTERM; durable stream (Kinesis) in prod",
         "reliability"),
    # Personal data retention.
    Risk("R-PRIVACY", "Analytics retaining personal data beyond purpose", "low", "high",
         "No raw IPs, referrer reduced to host, retention purge for raw events", "compliance"),
    # Single-node prototype limits.
    Risk("R-SINGLE-NODE", "Prototype uses SQLite and in-process cache/limiter (single node)", "high", "medium",
         "All are behind ports; production adapters listed in the architecture; limiter must move to a "
         "shared store before scaling out", "scalability"),
]

# Defects injected by the 'bug:<task>' fault: task -> (file, text to find, replacement).
BUG_VARIANTS = {
    # A plausible generation defect: SQL that drifts from the data contract.
    "gen_persistence": ("shortener/repository.py",
                        '"SELECT code, target_url, created_at, expires_at, owner',
                        '"SELECT code, target_url, created_at, expiry, owner'),
    # A plausible API defect: wrong status code for the redirect.
    "gen_api": ("shortener/http.py", "return Response(302, None", "return Response(200, None"),
}

# Assemble the pack and register it (runs once, when knowledge.packs() imports this module).
register(DomainPack(
    # Pack id used as NormalizedRequirement.domain.
    id="url_shortener",
    # Display name.
    title="URL shortener service",
    # Recognition keywords (regex -> weight); see DomainPack.score().
    keywords={r"url shortener": 5, r"shorten": 4, r"short[- ]?(link|url)s?": 4, r"\bredirect": 2,
              r"\bslug": 1.5, r"\balias": 1.5, r"\blinks?\b": 1.5, r"\burls?\b": 1.0,
              r"campaign": 0.5, r"clicks?": 0.5},
    capabilities=CAPABILITIES,
    dimensions=DIMENSIONS,
    components=COMPONENTS,
    test_suites=TEST_SUITES,
    architecture=ARCHITECTURE,
    tradeoffs=TRADEOFFS,
    risks=RISKS,
    # Template directory: blueprints/url_shortener/files/*.tmpl.
    blueprint_dir=Path(__file__).parent / "files",
    bug_variants=BUG_VARIANTS,
    # Contract builder used by Architect._contract().
    build_contract=build_openapi,
    # Brownfield recipes used by CodebaseAnalyst/Planner/CodeGenerator.
    brownfield_recipes=RECIPES,
))
