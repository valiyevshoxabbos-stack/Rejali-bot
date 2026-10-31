"""
AltronBek 3.0.1: SQLite (WAL + doimiy ulanish + tranzaksiyali migratsiya + zaxira/tiklash).

- Har oqim (thread) uchun bitta ulanish saqlanadi va WAL yoqiladi (Railway Volume kechikishini yo'qotadi).
- Migratsiyalar PRAGMA user_version orqali bitta tranzaksiyada yuritiladi.
- Mavjud baza yangilanishidan oldin `<baza>.pre-v<N>.bak` nusxasi olinadi.
- Eski 2.0 bazasi (user_version=0, lekin jadvallari bor) `.v2.bak` ga chetga olinadi.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager

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

# v2: qazo.code ustunidan global UNIQUE cheklovi olib tashlanadi (keyingi yillarda bir xil sana
# kodi to'qnashmasligi uchun); occurrence_id bo'yicha noyoblik saqlanadi.
MIGRATION_2 = """
CREATE TABLE qazo_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurrence_id INTEGER NOT NULL UNIQUE,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    done_at TEXT
);
INSERT INTO qazo_v2 (id, occurrence_id, code, title, date, status, created_at, done_at)
SELECT id, occurrence_id, code, title, date, status, created_at, done_at FROM qazo;
DROP TABLE qazo;
ALTER TABLE qazo_v2 RENAME TO qazo;
CREATE INDEX idx_qazo_code_status ON qazo(code, status);
"""

MIGRATIONS = [MIGRATION_1, MIGRATION_2]

REQUIRED_TABLES = (
    "settings", "reminders", "overrides", "checkpoints", "checkpoint_status",
    "occurrences", "qazo", "notes", "msg_log", "cleanup_prefs", "cleanup_events",
    "sleep_log", "birthdays", "birthday_notices", "weight_log",
)

_local = threading.local()
_conn_lock = threading.Lock()
_open_conns: list[sqlite3.Connection] = []


def path() -> str:
    return os.environ.get("DB_PATH", "altronbek.db")


def _open_configured(p: str) -> sqlite3.Connection:
    conn = sqlite3.connect(p, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_conn() -> sqlite3.Connection:
    p = path()
    conn = getattr(_local, "conn", None)
    cur_path = getattr(_local, "path", None)
    if conn is not None and cur_path == p:
        return conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    conn = _open_configured(p)
    _local.conn = conn
    _local.path = p
    with _conn_lock:
        _open_conns.append(conn)
    return conn


def close_db():
    """Barcha ochiq ulanishlarni yopadi (tiklash va migratsiyadan oldin chaqiriladi)."""
    with _conn_lock:
        conns = list(_open_conns)
        _open_conns.clear()
    for c in conns:
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


@contextmanager
def connect():
    conn = _get_conn()
    with conn:
        yield conn


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


def backup_to(src_path: str, dst_path: str):
    """SQLite online backup API orqali izchil nusxa yaratadi (WAL xavfsiz)."""
    src = sqlite3.connect(src_path, timeout=10.0)
    dst = sqlite3.connect(dst_path, timeout=10.0)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _remove_wal_sidecars(p: str):
    for ext in ("-wal", "-shm"):
        sidecar = p + ext
        if os.path.exists(sidecar):
            try:
                os.remove(sidecar)
            except OSError:
                pass


def _handle_legacy(p: str):
    """2.0 bazasi (user_version=0 va jadvallar bor) bo'lsa, chetga olib qo'yadi."""
    if not os.path.exists(p):
        return
    close_db()
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
        _remove_wal_sidecars(p)


def schema_version() -> int:
    conn = sqlite3.connect(path(), timeout=10.0)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _split_sql(script: str) -> list[str]:
    stmts = []
    buf = []
    for raw_line in script.splitlines():
        code_part = raw_line.split("--", 1)[0]
        if not code_part.strip():
            continue
        buf.append(code_part)
        candidate = "\n".join(buf)
        if sqlite3.complete_statement(candidate):
            stmts.append(candidate.strip())
            buf.clear()
    if buf and "".join(buf).strip():
        stmts.append("\n".join(buf).strip())
    return stmts


def migrate() -> list[str]:
    """
    Kutilayotgan migratsiyalarni bitta tranzaksiyada qo'llaydi.
    Mavjud baza yangilansa, har qadamdan oldin `<baza>.pre-v<N>.bak` yaratadi va
    yaratilgan zaxira fayllar ro'yxatini qaytaradi.
    """
    p = path()
    close_db()
    conn = sqlite3.connect(p, timeout=10.0, isolation_level=None)
    backups: list[str] = []
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= len(MIGRATIONS):
            return backups
        has_existing_data = version > 0 and bool(
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()
        )
        for i in range(version, len(MIGRATIONS)):
            target = i + 1
            if has_existing_data:
                bak_path = f"{p}.pre-v{target}.bak"
                backup_to(p, bak_path)
                backups.append(bak_path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(MIGRATIONS[i]):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {target}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            has_existing_data = True
    finally:
        conn.close()
    return backups


def init_db() -> list[str]:
    p = path()
    directory = os.path.dirname(p)
    if directory:
        os.makedirs(directory, exist_ok=True)
    _handle_legacy(p)
    return migrate()


def validate_backup_file(file_path: str) -> dict:
    """Yuklangan .db fayl yaroqliligini tekshiradi. {'ok': True, ...} yoki {'ok': False, 'error': ...}."""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        return {"ok": False, "error": "Fayl bo'sh yoki SQLite bazasi emas."}
    with open(file_path, "rb") as f:
        header = f.read(16)
    if header != b"SQLite format 3\x00":
        return {"ok": False, "error": "Bu fayl SQLite bazasi emas."}
    try:
        conn = sqlite3.connect(file_path, timeout=5.0)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0].lower() != "ok":
                return {"ok": False, "error": "Baza shikastlangan (integrity_check o'tmadi)."}
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                return {"ok": False, "error": "Bu eski (2.0) yoki bo'sh baza. Faqat 3.0+ zaxirasi tiklanadi."}
            if ver > len(MIGRATIONS):
                return {"ok": False, "error": f"Bu zaxira yangiroq versiyadan (v{ver}, joriy v{len(MIGRATIONS)})."}
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            if missing:
                return {"ok": False, "error": f"Kerakli jadvallar yetishmayapti: {', '.join(missing[:5])}"}
            counts = {
                "birthdays": conn.execute("SELECT COUNT(*) FROM birthdays").fetchone()[0],
                "custom_rem": conn.execute("SELECT COUNT(*) FROM reminders WHERE source='custom' AND active=1").fetchone()[0],
                "occurrences": conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0],
                "sleep": conn.execute("SELECT COUNT(*) FROM sleep_log WHERE duration_min IS NOT NULL").fetchone()[0],
                "weight": conn.execute("SELECT COUNT(*) FROM weight_log").fetchone()[0],
                "qazo_open": conn.execute("SELECT COUNT(*) FROM qazo WHERE status='open'").fetchone()[0],
            }
            return {"ok": True, "version": ver, "counts": counts}
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return {"ok": False, "error": "Faylni SQLite sifatida o'qib bo'lmadi."}


def restore_from_file(src_path: str, stamp: str) -> str:
    """
    Tasdiqlangan zaxira faylini asosiy baza o'rniga yozadi.
    Avval joriy bazadan `<baza>.before-restore-<stamp>.bak` yaratadi va shu yo'lni qaytaradi.
    """
    check = validate_backup_file(src_path)
    if not check["ok"]:
        raise ValueError(check["error"])
    p = path()
    safety_bak = f"{p}.before-restore-{stamp}.bak"
    close_db()
    if os.path.exists(p) and os.path.getsize(p) > 0:
        backup_to(p, safety_bak)
    close_db()
    _remove_wal_sidecars(p)
    if os.path.exists(p):
        os.remove(p)
    backup_to(src_path, p)
    migrate()
    return safety_bak
