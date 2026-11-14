# Runbook

## Health
`GET /healthz` -> `{"status": "ok", "analytics_backlog": N, "analytics_dropped": M}`.
Alert when `analytics_dropped` increases (analytics capacity) or backlog stays > 10k (store slow).

## Key metrics (production)
- redirect p50/p99 latency, 5xx rate, 404/410 rate (spikes = enumeration or expired campaign)
- cache hit ratio (target > 90%), store read/write throttles
- link creations per key, 429 rate (abuse)

## Failure modes
| Symptom | Likely cause | Action |
|---|---|---|
| 503 `code_space_exhausted` | code length too short for the volume | raise `SHORTENER_CODE_LENGTH` |
| rising `analytics_dropped` | analytics store slow/unavailable | redirects unaffected by design; scale/repair store |
| 429 for legitimate client | per-key limit too low | raise `SHORTENER_CREATE_RATE_PER_MIN` for that tier |
| phishing report | abuse | `DELETE /api/v1/links/{code}` with owner key; add host to blocklist |

## Operations
- Graceful shutdown (SIGTERM) flushes buffered click events.
- Retention: schedule `SQLiteAnalyticsStore.purge_before(now - retention)` daily
  (production: S3 lifecycle rule on raw events).
