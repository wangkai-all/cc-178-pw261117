"""SQLite persistence for the incremental regional sales roll-up.

The database is the single source of truth.  Every committed batch is stored
together with its per-op ledger, its signed per-region deltas, the resulting
summary snapshot, a full copy of the source rows that snapshot was based on,
and an item-by-item reconciliation against a full recomputation.
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stores (
    store_id      TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    zone_id       TEXT REFERENCES zones(zone_id),
    version       INTEGER NOT NULL DEFAULT 1,
    created_batch TEXT,
    updated_batch TEXT
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id       TEXT PRIMARY KEY,
    store_id      TEXT,
    amount_cents  INTEGER,
    status        TEXT NOT NULL CHECK (status IN ('active', 'deleted')),
    version       INTEGER NOT NULL,
    created_batch TEXT NOT NULL,
    updated_batch TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id          TEXT PRIMARY KEY,
    seq               INTEGER NOT NULL,
    label             TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    payload           TEXT NOT NULL,
    strict            INTEGER NOT NULL,
    base_version      INTEGER,
    snapshot_version  INTEGER NOT NULL,
    applied_at        TEXT NOT NULL,
    op_count          INTEGER NOT NULL,
    accepted_count    INTEGER NOT NULL,
    rejected_count    INTEGER NOT NULL,
    entry_count       INTEGER NOT NULL,
    status            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_ops (
    batch_id      TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    op_type       TEXT NOT NULL,
    payload       TEXT NOT NULL,
    result        TEXT NOT NULL,
    error_code    TEXT,
    error_message TEXT,
    warnings      TEXT,
    PRIMARY KEY (batch_id, idx)
);

CREATE TABLE IF NOT EXISTS ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           TEXT NOT NULL,
    op_index           INTEGER NOT NULL,
    sale_id            TEXT NOT NULL,
    store_id           TEXT,
    phase              TEXT NOT NULL CHECK (phase IN ('retract', 'add')),
    reason             TEXT NOT NULL,
    zone_key           TEXT NOT NULL,
    order_delta        INTEGER NOT NULL,
    amount_count_delta INTEGER NOT NULL,
    null_count_delta   INTEGER NOT NULL,
    zero_count_delta   INTEGER NOT NULL,
    total_delta_cents  INTEGER NOT NULL,
    old_amount_cents   INTEGER,
    new_amount_cents   INTEGER,
    old_zone_key       TEXT,
    new_zone_key       TEXT
);

CREATE TABLE IF NOT EXISTS group_deltas (
    batch_id             TEXT NOT NULL,
    zone_key             TEXT NOT NULL,
    retract_orders       INTEGER NOT NULL,
    add_orders           INTEGER NOT NULL,
    retract_amount_count INTEGER NOT NULL,
    add_amount_count     INTEGER NOT NULL,
    retract_null_count   INTEGER NOT NULL,
    add_null_count       INTEGER NOT NULL,
    retract_zero_count   INTEGER NOT NULL,
    add_zero_count       INTEGER NOT NULL,
    retract_total_cents  INTEGER NOT NULL,
    add_total_cents      INTEGER NOT NULL,
    net_orders           INTEGER NOT NULL,
    net_amount_count     INTEGER NOT NULL,
    net_null_count       INTEGER NOT NULL,
    net_zero_count       INTEGER NOT NULL,
    net_total_cents      INTEGER NOT NULL,
    PRIMARY KEY (batch_id, zone_key)
);

CREATE TABLE IF NOT EXISTS snapshots (
    version    INTEGER PRIMARY KEY,
    batch_id   TEXT NOT NULL,
    label      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summary_groups (
    version          INTEGER NOT NULL,
    zone_key         TEXT NOT NULL,
    zone_name        TEXT NOT NULL,
    order_count      INTEGER NOT NULL,
    amount_count     INTEGER NOT NULL,
    null_count       INTEGER NOT NULL,
    zero_count       INTEGER NOT NULL,
    total_cents      INTEGER NOT NULL,
    unit_price_cents INTEGER,
    PRIMARY KEY (version, zone_key)
);

CREATE TABLE IF NOT EXISTS full_groups (
    version          INTEGER NOT NULL,
    zone_key         TEXT NOT NULL,
    zone_name        TEXT NOT NULL,
    order_count      INTEGER NOT NULL,
    amount_count     INTEGER NOT NULL,
    null_count       INTEGER NOT NULL,
    zero_count       INTEGER NOT NULL,
    total_cents      INTEGER NOT NULL,
    unit_price_cents INTEGER,
    PRIMARY KEY (version, zone_key)
);

CREATE TABLE IF NOT EXISTS reconciliation (
    version           INTEGER NOT NULL,
    zone_key          TEXT NOT NULL,
    zone_name         TEXT NOT NULL,
    measure           TEXT NOT NULL,
    incremental_value TEXT,
    full_value        TEXT,
    is_match          INTEGER NOT NULL,
    PRIMARY KEY (version, zone_key, measure)
);

CREATE TABLE IF NOT EXISTS source_sales_snapshot (
    version      INTEGER NOT NULL,
    sale_id      TEXT NOT NULL,
    store_id     TEXT,
    amount_cents INTEGER,
    status       TEXT NOT NULL,
    PRIMARY KEY (version, sale_id)
);

CREATE TABLE IF NOT EXISTS source_stores_snapshot (
    version  INTEGER NOT NULL,
    store_id TEXT NOT NULL,
    name     TEXT NOT NULL,
    zone_id  TEXT,
    PRIMARY KEY (version, store_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT,
    payload_hash     TEXT NOT NULL,
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL,
    batch_id         TEXT,
    error_code       TEXT,
    error            TEXT,
    fail_at          TEXT,
    applied_ops      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger(batch_id, op_index);
CREATE INDEX IF NOT EXISTS idx_ledger_zone ON ledger(zone_key);
CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);
CREATE INDEX IF NOT EXISTS idx_attempts_hash ON attempts(payload_hash);
CREATE INDEX IF NOT EXISTS idx_summary_version ON summary_groups(version);
"""

DEFAULT_DB_PATH = os.environ.get(
    "APP_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "increment.sqlite3"),
)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with explicit transaction control."""
    db_path = path or DEFAULT_DB_PATH
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('initialized', '1')")


def format_cents(cents: int | None) -> str | None:
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"
