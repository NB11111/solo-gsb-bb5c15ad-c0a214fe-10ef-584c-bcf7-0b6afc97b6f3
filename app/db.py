"""SQLite persistence and schema management."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS budgets (
    dataset_id TEXT PRIMARY KEY,
    total      REAL NOT NULL,
    used       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS releases (
    id              TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    dataset_id      TEXT NOT NULL,
    query_id        TEXT NOT NULL,
    epsilon         REAL NOT NULL,
    sensitivity     REAL NOT NULL,
    true_value      REAL NOT NULL,
    noise           REAL NOT NULL,
    released_value  REAL NOT NULL,
    budget_used     REAL NOT NULL,
    budget_remaining REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_releases_dataset ON releases(dataset_id);

CREATE TABLE IF NOT EXISTS transfers (
    id                      TEXT PRIMARY KEY,
    source_dataset          TEXT NOT NULL,
    target_dataset          TEXT NOT NULL,
    epsilon                 REAL NOT NULL,
    source_remaining_before REAL NOT NULL,
    source_remaining_after  REAL NOT NULL,
    target_remaining_before REAL NOT NULL,
    target_remaining_after  REAL NOT NULL,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_transfers_source ON transfers(source_dataset);
CREATE INDEX IF NOT EXISTS idx_transfers_target ON transfers(target_dataset);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.database_path,
        timeout=10.0,
        isolation_level=None,  # autocommit; transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # WAL allows concurrent readers while a writer holds the lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def immediate_transaction() -> Iterator[sqlite3.Connection]:
    """Open an IMMEDIATE write transaction.

    Acquires SQLite's reserved lock up-front, so concurrent requests racing
    for the last slice of a budget serialize: exactly one can commit a
    deduction, the others see the updated balance and fail with 402.
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
