"""SQLite storage for volumes, holdings and batch audit records."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    volume_label TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (title, volume_label)
);

CREATE TABLE IF NOT EXISTS issues (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    label     TEXT NOT NULL,
    UNIQUE (volume_id, position),
    UNIQUE (volume_id, label)
);

CREATE TABLE IF NOT EXISTS copies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id  INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    barcode    TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_groups (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id  INTEGER NOT NULL REFERENCES copies(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 2),
    UNIQUE (copy_id, position)
);

CREATE TABLE IF NOT EXISTS candidate_group_issues (
    group_id INTEGER NOT NULL REFERENCES candidate_groups(id) ON DELETE CASCADE,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, issue_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    volume_id  INTEGER NOT NULL REFERENCES volumes(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL UNIQUE,
    status     TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    reason     TEXT,
    request    TEXT NOT NULL,
    result     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    barcode    TEXT NOT NULL,
    action     TEXT NOT NULL,
    candidates TEXT NOT NULL,
    outcome    TEXT NOT NULL,
    message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_issues_volume ON issues(volume_id, position);
CREATE INDEX IF NOT EXISTS idx_copies_volume ON copies(volume_id);
CREATE INDEX IF NOT EXISTS idx_groups_copy ON candidate_groups(copy_id, position);
CREATE INDEX IF NOT EXISTS idx_group_issues_issue ON candidate_group_issues(issue_id);
CREATE INDEX IF NOT EXISTS idx_batches_volume ON batches(volume_id, seq);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id, position);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a connection in autocommit mode with sane pragmas.

    ``isolation_level=None`` means transactions are controlled explicitly with
    ``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK``.  That lets a batch acquire
    the SQLite write lock *before* reading holdings, which is what makes
    concurrent submissions serialise in their actual commit order.
    """
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | os.PathLike[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """Pin every read inside the block to one committed database snapshot.

    In WAL mode a plain (deferred) ``BEGIN`` takes a read mark on the first
    statement and then sees the same snapshot for the whole transaction.  All
    SELECTs issued by a GET endpoint therefore observe holdings, candidate
    groups and batch audit rows from the *same* commit point: a batch that
    commits mid-request can no longer splice pre-commit copies onto
    post-commit candidate groups.

    Readers take no write lock, so a concurrent ``BEGIN IMMEDIATE`` batch is
    neither blocked nor able to change the snapshot once it is taken.  The
    context manager is re-entrant: nested calls reuse the surrounding
    transaction instead of issuing a second ``BEGIN`` (which SQLite rejects).
    """
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        # Deferred transaction: the snapshot is established by the first read.
        conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if owns_transaction and conn.in_transaction:
            conn.execute("COMMIT")
