"""Database initialisation and helpers for CareCircle.

Uses plain sqlite3 – no ORM.  Timestamps are ISO-8601 in Asia/Kolkata.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))

_DB_PATH: str = "carecircle.db"


def set_db_path(path: str) -> None:
    """Override the default database file path."""
    global _DB_PATH
    _DB_PATH = path


def get_connection() -> sqlite3.Connection:
    """Return a connection with row-factory set to sqlite3.Row."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_ist() -> str:
    """Current timestamp in ISO format, Asia/Kolkata."""
    return datetime.now(IST).isoformat(timespec="seconds")


# ── Schema ────────────────────────────────────────────────────────────

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('patient','caregiver','viewer')),
    token_hash  TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS medications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES people(id),
    name          TEXT    NOT NULL,
    dose          TEXT    NOT NULL,
    times_json    TEXT    NOT NULL,   -- JSON array of HH:MM strings
    aliases_json  TEXT    NOT NULL DEFAULT '[]',  -- JSON array of alias strings
    pills_left    INTEGER NOT NULL DEFAULT 0,
    pills_per_day REAL    NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS dose_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id  INTEGER NOT NULL REFERENCES medications(id),
    logged_by      INTEGER NOT NULL REFERENCES people(id),
    taken_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id),
    author_id   INTEGER NOT NULL REFERENCES people(id),
    kind        TEXT    NOT NULL CHECK (kind IN ('symptom','general')),
    text        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES people(id),
    doctor      TEXT    NOT NULL,
    specialty   TEXT    NOT NULL,
    starts_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS refill_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id  INTEGER NOT NULL REFERENCES medications(id),
    requested_by   INTEGER NOT NULL REFERENCES people(id),
    quantity       INTEGER NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER NOT NULL REFERENCES people(id),
    action      TEXT    NOT NULL,
    item_type   TEXT    NOT NULL,
    item_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);
"""


def init_db() -> None:
    """Create all tables if they don't exist."""
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.close()


# ── Query helpers ─────────────────────────────────────────────────────

def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Execute *sql* and return the first row as a dict, or None."""
    conn = get_connection()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Execute *sql* and return all rows as a list of dicts."""
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def execute(sql: str, params: tuple = ()) -> int:
    """Execute a write statement and return lastrowid."""
    conn = get_connection()
    cur = conn.execute(sql, params)
    conn.commit()
    lastrowid = cur.lastrowid
    conn.close()
    return lastrowid  # type: ignore[return-value]


def get_patient_id() -> int:
    """Return the id of the first patient in the DB."""
    row = fetch_one("SELECT id FROM people WHERE role='patient' LIMIT 1")
    if row is None:
        raise RuntimeError("No patient in database. Run seed.py first.")
    return row["id"]


def find_medication(name: str, patient_id: int) -> dict[str, Any] | None:
    """Fuzzy-match a medication by name or alias for the given patient.

    Uses simple case-insensitive substring matching – good enough for
    demo phrases like 'BP tablet' matching 'Amlodipine (BP tablet)'.
    """
    meds = fetch_all(
        "SELECT * FROM medications WHERE person_id = ?", (patient_id,)
    )
    name_lower = name.lower().strip()
    for med in meds:
        # Check canonical name
        if name_lower in med["name"].lower():
            return med
        # Check aliases
        aliases: list[str] = json.loads(med.get("aliases_json", "[]"))
        for alias in aliases:
            if name_lower in alias.lower() or alias.lower() in name_lower:
                return med
    return None
