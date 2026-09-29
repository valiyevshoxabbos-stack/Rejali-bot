"""
RejaliBot 2.0: SQLite ma'lumotlar bazasi.

Railway'da ma'lumotlar qayta deploy qilinganda o'chib ketmasligi uchun
DB_PATH ni Railway Volume ichidagi yo'lga qo'ying (masalan /data/rejabot.db).
Lokal ishga tushirishda oddiy fayl ishlatiladi.
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "rejabot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Standart reja shablonlari (schedule_data.py dan yuklanadi) va
-- foydalanuvchi qo'shgan maxsus eslatmalar: bitta umumiy jadval.
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT,                        -- standart eslatmalar uchun qisqa id ("zal"), maxsus uchun NULL
    source TEXT NOT NULL,             -- 'standard' | 'custom'
    kind TEXT NOT NULL,               -- 'daily' | 'weekly' | 'once'
    weekday INTEGER,                  -- weekly uchun 0..6
    date TEXT,                        -- once uchun YYYY-MM-DD
    start TEXT NOT NULL,              -- HH:MM
    end TEXT,                         -- HH:MM (ixtiyoriy)
    title TEXT NOT NULL,
    note TEXT DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 1,   -- 1 oddiy, 2 muhim, 3 juda muhim (faqat ko'rsatish uchun)
    scored INTEGER NOT NULL DEFAULT 0,     -- ballga ta'sir qiladimi
    points INTEGER NOT NULL DEFAULT 0,     -- shu eslatma uchun qo'lda belgilangan ball
    qazo_tracked INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- Standart eslatmani bir kunga o'zgartirish (doimiy o'zgarish reminders.start ni yangilaydi)
CREATE TABLE IF NOT EXISTS overrides (
    reminder_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    start TEXT,
    end TEXT,
    PRIMARY KEY (reminder_id, date)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

-- Chek-pointning har bir kundagi (occurrence) belgilanishi
CREATE TABLE IF NOT EXISTS checkpoint_status (
    checkpoint_id INTEGER NOT NULL,
    occurrence_id INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (checkpoint_id, occurrence_id)
);

-- Har bir kun uchun har bir eslatmaning holati
CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,
    prayer TEXT,                      -- namoz uchun nomi (bomdod, peshin, ...), aks holda NULL
    date TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,       -- ISO datetime
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
        -- pending | done | skipped | postponed | qazo | qazo_done
    done_at TEXT,
    late_minutes INTEGER,
    points INTEGER NOT NULL DEFAULT 0,
    scored INTEGER NOT NULL DEFAULT 0,
    is_subtask INTEGER NOT NULL DEFAULT 0,
    postponed_to TEXT,
    is_rescheduled INTEGER NOT NULL DEFAULT 0,  -- kechiktirilgan eslatmaning yangi nusxasi
    UNIQUE (reminder_id, prayer, date, scheduled_at)
);

CREATE TABLE IF NOT EXISTS qazo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER NOT NULL,
    code TEXT NOT NULL UNIQUE,        -- masalan bomdod_21_09
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | done
    created_at TEXT NOT NULL,
    done_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER,
    date TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_occ_date ON occurrences(date);
CREATE INDEX IF NOT EXISTS idx_occ_status ON occurrences(status);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
