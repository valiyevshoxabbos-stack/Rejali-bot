"""
AltronBek 3.0: SQLite.

Migratsiyalar PRAGMA user_version orqali yuritiladi: keyingi versiyalar
(3.1, 3.2 ...) MIGRATIONS ro'yxatining oxiriga yangi qism qo'shadi, baza
noldan boshlanmaydi.

Eski 2.0 bazasi (user_version=0, lekin jadvallari bor) topilsa, u o'chirilmaydi:
`.v2.bak` nomi bilan chetga olinadi va yangi baza yaratiladi.
"""

import os
import sqlite3
from contextlib import contextmanager


def path() -> str:
    return os.environ.get("DB_PATH", "altronbek.db")


MIGRATION_1 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT,
    source TEXT NOT NULL,                 -- standard | custom
    kind TEXT NOT NULL,                   -- daily | weekly | once
    weekday INTEGER,
    date TEXT,
    start TEXT NOT NULL,
    end TEXT,
    title TEXT NOT NULL,
    note TEXT DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 1,  -- 1 oddiy, 2 muhim, 3 juda muhim
    scored INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'required',-- required | optional | off
    off_until TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE overrides (
    reminder_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    start TEXT,
    end TEXT,
    PRIMARY KEY (reminder_id, date)
);

CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE checkpoint_status (
    checkpoint_id INTEGER NOT NULL,
    occurrence_id INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (checkpoint_id, occurrence_id)
);

CREATE TABLE occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,
    prayer TEXT,
    date TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | planned | done | skipped | declined | postponed | qazo | qazo_done
    done_at TEXT,
    late_minutes INTEGER,
    points INTEGER NOT NULL DEFAULT 0,
    scored INTEGER NOT NULL DEFAULT 0,
    is_subtask INTEGER NOT NULL DEFAULT 0,
    is_optional INTEGER NOT NULL DEFAULT 0,
    is_manual INTEGER NOT NULL DEFAULT 0,
    is_rescheduled INTEGER NOT NULL DEFAULT 0,
    postponed_to TEXT
);
CREATE INDEX idx_occ_date ON occurrences(date);
CREATE INDEX idx_occ_reminder ON occurrences(reminder_id, date);

CREATE TABLE qazo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    done_at TEXT
);

CREATE TABLE notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Xabarlarni tozalash tizimi
CREATE TABLE msg_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    text TEXT,
    actionable INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL,
    deleted_at TEXT,
    deleted_by TEXT                       -- user | auto | expired | restored | flow
);
CREATE INDEX idx_msg_mid ON msg_log(message_id);

CREATE TABLE cleanup_prefs (
    category TEXT PRIMARY KEY,
    auto_delete INTEGER NOT NULL DEFAULT 0,
    delay_min INTEGER NOT NULL DEFAULT 120
);

CREATE TABLE cleanup_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    at TEXT NOT NULL
);

-- Uyqu, tug'ilgan kunlar, vazn
CREATE TABLE sleep_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bed_confirmed_at TEXT,
    sleep_start TEXT,
    wake_at TEXT,
    duration_min INTEGER,
    wake_source TEXT
);

CREATE TABLE birthdays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    day INTEGER NOT NULL,
    month INTEGER NOT NULL,
    year INTEGER
);

CREATE TABLE birthday_notices (
    birthday_id INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    offset INTEGER NOT NULL,
    PRIMARY KEY (birthday_id, event_date, offset)
);

CREATE TABLE weight_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kg REAL NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0
);
"""

# Keyingi versiyalar shu ro'yxat oxiriga yangi element qo'shadi.
MIGRATIONS = [MIGRATION_1]


@contextmanager
def connect():
    conn = sqlite3.connect(path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def q(sql: str, args=()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql: str, args=()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=()) -> int:
    """Yozish. O'zgargan qatorlar sonini qaytaradi."""
    with connect() as c:
        return c.execute(sql, args).rowcount


def ins(sql: str, args=()) -> int:
    """INSERT. Yangi qator id sini qaytaradi."""
    with connect() as c:
        return c.execute(sql, args).lastrowid


def get_setting(key: str, default: str | None = None) -> str | None:
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    ex(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def del_setting(key: str):
    ex("DELETE FROM settings WHERE key = ?", (key,))


def _handle_legacy(p: str):
    """2.0 bazasi (user_version=0 va jadvallar bor) bo'lsa, chetga olib qo'yadi."""
    if not os.path.exists(p):
        return
    conn = sqlite3.connect(p)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_tables = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders'"
        ).fetchone()
    finally:
        conn.close()
    if version == 0 and has_tables:
        os.replace(p, p + ".v2.bak")


def schema_version() -> int:
    conn = sqlite3.connect(path())
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def migrate():
    conn = sqlite3.connect(path())
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            conn.executescript(MIGRATIONS[i])
            conn.execute(f"PRAGMA user_version = {i + 1}")
            conn.commit()
    finally:
        conn.close()


def init_db():
    p = path()
    directory = os.path.dirname(p)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _handle_legacy(p)
    migrate()
