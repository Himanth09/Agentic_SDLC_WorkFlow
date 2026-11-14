-- Data schema for the URL shortener (generated from the architecture design).
-- Production mapping: `links` -> DynamoDB table (PK=code); `click_events` ->
-- Kinesis Firehose -> S3 (Athena); `click_daily` -> DynamoDB aggregates table.

CREATE TABLE IF NOT EXISTS links (
    code        TEXT    PRIMARY KEY,          -- uniqueness enforces collision detection
    target_url  TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    expires_at  REAL,                         -- NULL = never expires
    owner       TEXT,                         -- API key principal that created the link
    is_custom   INTEGER NOT NULL DEFAULT 0,
    disabled    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_owner ON links (owner);

CREATE TABLE IF NOT EXISTS click_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL,
    ts            REAL    NOT NULL,
    referrer_host TEXT,
    ua_class      TEXT    NOT NULL,
    visitor       TEXT                         -- salted daily hash of client IP, or NULL
);
CREATE INDEX IF NOT EXISTS idx_click_events_code_ts ON click_events (code, ts);

CREATE TABLE IF NOT EXISTS click_daily (
    code   TEXT    NOT NULL,
    day    TEXT    NOT NULL,                   -- UTC date, YYYY-MM-DD
    clicks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, day)
);
