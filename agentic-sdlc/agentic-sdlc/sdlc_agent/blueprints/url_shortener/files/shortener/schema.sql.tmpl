-- Data schema for the URL shortener (generated from the architecture design).
-- Production mapping: `links` -> DynamoDB table (PK=code); `click_events` ->
-- Kinesis Firehose -> S3 (Athena); `click_daily` -> DynamoDB aggregates table.
-- Every statement uses IF NOT EXISTS, so the script runs safely on each startup.

-- One row per short link. Rows are never deleted, only disabled.
CREATE TABLE IF NOT EXISTS links (
    code        TEXT    PRIMARY KEY,          -- uniqueness enforces collision detection
    target_url  TEXT    NOT NULL,
    created_at  REAL    NOT NULL,             -- epoch seconds
    expires_at  REAL,                         -- NULL = never expires
    owner       TEXT,                         -- API key principal that created the link
    is_custom   INTEGER NOT NULL DEFAULT 0,   -- 1 = client-chosen alias
    disabled    INTEGER NOT NULL DEFAULT 0    -- 1 = soft-deleted, no longer redirects
);
-- Supports listing or auditing links by owner.
CREATE INDEX IF NOT EXISTS idx_links_owner ON links (owner);

-- Raw click events, kept only for the retention window (purged by age).
CREATE TABLE IF NOT EXISTS click_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL,
    ts            REAL    NOT NULL,            -- click time, epoch seconds
    referrer_host TEXT,                        -- host only, never the full referrer URL
    ua_class      TEXT    NOT NULL,            -- desktop | mobile | bot | unknown
    visitor       TEXT                         -- salted daily hash of client IP, or NULL
);
-- Every stats query filters by code and time window.
CREATE INDEX IF NOT EXISTS idx_click_events_code_ts ON click_events (code, ts);

-- Pre-aggregated daily totals: stats read these instead of counting raw events.
CREATE TABLE IF NOT EXISTS click_daily (
    code   TEXT    NOT NULL,
    day    TEXT    NOT NULL,                   -- UTC date, YYYY-MM-DD
    clicks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, day)                    -- one counter per link per day (upsert target)
);
