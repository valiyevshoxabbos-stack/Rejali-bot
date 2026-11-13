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

# v3 (3.1/3.2): Xarajatlar kalkulyatori jadvallari (bozorlik, restoran, safar).
MIGRATION_3 = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                  -- market | restaurant | trip
    status TEXT NOT NULL DEFAULT 'open', -- open | closed
    service_pct REAL NOT NULL DEFAULT 0,
    participants TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    amount INTEGER NOT NULL,
    payer TEXT NOT NULL,
    qty REAL,
    unit TEXT,
    unit_price INTEGER,
    is_shared INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_expenses_session ON expenses(session_id);
CREATE INDEX idx_expenses_item ON expenses(title, unit);

CREATE TABLE expense_splits (
    expense_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    share_amount INTEGER NOT NULL,
    PRIMARY KEY (expense_id, user_name)
);
"""

# v4 (3.5): Universal Chelenj dvigateli va Universal Moliya tizimi jadvallari.
MIGRATION_4 = """
CREATE TABLE challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    bonus_points INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL DEFAULT 'active' -- active | completed | cancelled
);
CREATE INDEX idx_challenges_status ON challenges(status);

CREATE TABLE challenge_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    task_type TEXT NOT NULL,              -- binary | numeric | weekly_quota | schedule
    target_value REAL NOT NULL DEFAULT 1,
    unit TEXT NOT NULL DEFAULT '',
    schedule_days TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX idx_ch_tasks_cid ON challenge_tasks(challenge_id);

CREATE TABLE challenge_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', -- done | partial | pending
    UNIQUE(task_id, log_date)
);
CREATE INDEX idx_ch_logs_cid_date ON challenge_logs(challenge_id, log_date);

CREATE TABLE challenge_bonuses (
    challenge_id INTEGER NOT NULL,
    bonus_date TEXT NOT NULL,
    points INTEGER NOT NULL DEFAULT 10,
    PRIMARY KEY (challenge_id, bonus_date)
);

CREATE TABLE finance_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    last_4 TEXT,
    type TEXT NOT NULL,                   -- cash | debit | credit
    balance INTEGER NOT NULL DEFAULT 0,
    credit_limit INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    usage_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE finance_debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_name TEXT NOT NULL,
    amount INTEGER NOT NULL,              -- >0 haq (menga berishadi), <0 qarz (men beraman)
    due_date TEXT,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' -- active | settled
);
CREATE INDEX idx_fin_debts_status ON finance_debts(status);

CREATE TABLE finance_loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    total_debt INTEGER NOT NULL,
    remaining_debt INTEGER NOT NULL,
    monthly_payment INTEGER NOT NULL,
    due_day INTEGER NOT NULL,
    grace_deadline_day INTEGER,
    grace_deadline_time TEXT,
    total_installments INTEGER NOT NULL DEFAULT 1,
    remaining_installments INTEGER NOT NULL DEFAULT 1,
    last_paid_month TEXT,
    end_date TEXT,
    status TEXT NOT NULL DEFAULT 'active' -- active | paid
);
CREATE INDEX idx_fin_loans_status ON finance_loans(status);

CREATE TABLE finance_recurring (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    target_amount INTEGER NOT NULL,
    paid_amount_this_month INTEGER NOT NULL DEFAULT 0,
    due_start_day INTEGER NOT NULL,
    due_end_day INTEGER NOT NULL,
    last_paid_month TEXT,
    current_month TEXT,
    status TEXT NOT NULL DEFAULT 'active' -- active | paused
);
CREATE INDEX idx_fin_rec_status ON finance_recurring(status);

CREATE TABLE finance_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_account_id INTEGER,
    to_account_id INTEGER,
    amount INTEGER NOT NULL,
    category TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_fin_tx_created ON finance_transactions(created_at);
"""

# v5 (3.5.1): Moliya kategoriyalari, Tranzaksiya Undo metadata va Chelenj Muzlatish (Skip Day) tizimi.
MIGRATION_5 = """
CREATE TABLE finance_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                  -- expense | income
    icon TEXT NOT NULL DEFAULT '📌',
    system_key TEXT,                     -- sys_loan | sys_rent | sys_debt_out | sys_debt_in | NULL
    is_system INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    usage_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_fin_cat_type ON finance_categories(type, is_active);

ALTER TABLE finance_transactions ADD COLUMN category_id INTEGER;
ALTER TABLE finance_transactions ADD COLUMN linked_kind TEXT;
ALTER TABLE finance_transactions ADD COLUMN linked_id INTEGER;
ALTER TABLE finance_transactions ADD COLUMN undo_meta TEXT;

ALTER TABLE challenges ADD COLUMN freezes_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE challenges ADD COLUMN max_freezes INTEGER NOT NULL DEFAULT 2;

CREATE TABLE challenge_freezes (
    challenge_id INTEGER NOT NULL,
    freeze_date TEXT NOT NULL,
    PRIMARY KEY (challenge_id, freeze_date)
);
"""

# v6 (3.6): Smart Bozorlik, Dinamik Savat, Inflyatsiya Monitoringi va Savdo joylari tahlili.
MIGRATION_6 = """
CREATE TABLE IF NOT EXISTS market_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    default_unit TEXT DEFAULT 'kg',
    last_unit_price REAL DEFAULT 0,
    last_location_id INTEGER REFERENCES market_locations(id),
    last_bought_date DATE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_baskets (
    id TEXT PRIMARY KEY, -- UUID4 string
    title TEXT,
    mode TEXT NOT NULL, -- 'planned' yoki 'active'
    location_id INTEGER REFERENCES market_locations(id),
    status TEXT NOT NULL, -- 'draft', 'completed', 'cancelled'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mkt_baskets_status ON market_baskets(status, mode);

CREATE TABLE IF NOT EXISTS market_basket_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id TEXT REFERENCES market_baskets(id),
    item_id INTEGER REFERENCES market_items(id),
    location_id INTEGER REFERENCES market_locations(id),
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    unit_price REAL DEFAULT 0,
    total_price REAL DEFAULT 0,
    is_bought BOOLEAN DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mkt_bitems_bid ON market_basket_items(basket_id);

CREATE TABLE IF NOT EXISTS market_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    basket_id TEXT REFERENCES market_baskets(id),
    item_id INTEGER NOT NULL REFERENCES market_items(id),
    location_id INTEGER REFERENCES market_locations(id),
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    unit_price REAL NOT NULL,
    total_price REAL NOT NULL,
    price_diff_percent REAL DEFAULT 0,
    bought_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mkt_logs_item ON market_logs(item_id, location_id);
"""

MIGRATION_7 = """
CREATE TABLE IF NOT EXISTS health_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'medicine',
    icon TEXT DEFAULT '💊',
    is_system BOOLEAN DEFAULT 0,
    is_default BOOLEAN DEFAULT 1,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS health_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'medicine',
    category_id INTEGER REFERENCES health_categories(id),
    location TEXT NOT NULL DEFAULT 'home',
    form TEXT DEFAULT 'tabletka',
    default_dosage TEXT DEFAULT '',
    remaining_quantity REAL DEFAULT 0,
    unit TEXT DEFAULT 'dona',
    low_stock_threshold REAL DEFAULT 2,
    expiry_date DATE,
    is_active BOOLEAN DEFAULT 1,
    notes TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_health_items_loc ON health_items(location, is_active);

CREATE TABLE IF NOT EXISTS health_item_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES health_items(id),
    category_id INTEGER REFERENCES health_categories(id)
);

CREATE TABLE IF NOT EXISTS health_intake_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES health_items(id),
    course_id INTEGER,
    taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    quantity REAL DEFAULT 1,
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'taken'
);
CREATE INDEX IF NOT EXISTS idx_health_intake_time ON health_intake_logs(item_id, taken_at);

CREATE TABLE IF NOT EXISTS health_courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    doctor_notes TEXT DEFAULT '',
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS health_course_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER REFERENCES health_courses(id),
    item_name TEXT NOT NULL,
    schedule_times TEXT NOT NULL,
    dose_quantity REAL DEFAULT 1,
    dose_unit TEXT DEFAULT 'tabletka',
    relation_to_food TEXT DEFAULT 'ovqatdan keyin',
    is_active BOOLEAN DEFAULT 1
);

CREATE TABLE IF NOT EXISTS health_triage_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symptom_code TEXT UNIQUE NOT NULL,
    symptom_name TEXT NOT NULL,
    is_red_flag BOOLEAN DEFAULT 0,
    natural_remedies TEXT DEFAULT '',
    recommended_otc TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_error_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_type TEXT NOT NULL,
    error_message TEXT NOT NULL,
    traceback_text TEXT DEFAULT '',
    context_info TEXT DEFAULT '',
    occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sys_err_time ON system_error_logs(occurred_at);
"""

MIGRATIONS = [MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5, MIGRATION_6, MIGRATION_7]
LAST_MIGRATED_FROM: int | None = None

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


def vacuum():
    """Bo'shagan joyni fizik qisqartirish uchun tranzaksiyadan tashqarida VACUUM va WAL checkpoint bajaradi."""
    p = path()
    close_db()
    conn = sqlite3.connect(p, timeout=15.0, isolation_level=None)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


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
    global LAST_MIGRATED_FROM
    p = path()
    close_db()
    conn = sqlite3.connect(p, timeout=10.0, isolation_level=None)
    backups: list[str] = []
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= len(MIGRATIONS):
            LAST_MIGRATED_FROM = None
            return backups
        LAST_MIGRATED_FROM = version
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


SEED_HEALTH_CATEGORIES = [
    ("Og'riq va isitma", "medicine", "💊", 1, 1, 1),
    ("Shamollash va gripp", "medicine", "🤧", 1, 1, 2),
    ("Oshqozon va hazm", "medicine", "🫄", 1, 1, 3),
    ("Allergiya", "medicine", "🌸", 1, 1, 4),
    ("Antiseptik va bog'lov", "medicine", "🩹", 1, 1, 5),
    ("Vitaminlar va minerallar", "medicine", "✨", 1, 1, 6),
    ("Tozalash va yuvinish", "skincare", "🧼", 1, 1, 7),
    ("Namlantirish va tiklash", "skincare", "🧴", 1, 1, 8),
    ("Quyoshdan himoya (SPF)", "skincare", "☀️", 1, 1, 9),
    ("Lab va qo'l parvarishi", "skincare", "💄", 1, 1, 10),
]

SEED_HEALTH_ITEMS = [
    # (name, cat_name, kind, location, form, dosage, qty, unit, low_stock, notes)
    ("Paratsetamol 500mg", "Og'riq va isitma", "medicine", "home", "tabletka", "1 tabletka", 10.0, "dona", 2.0, "Isitma va bosh og'rig'iga"),
    ("Ibuprofen 400mg", "Og'riq va isitma", "medicine", "home", "tabletka", "1 tabletka", 10.0, "dona", 2.0, "Yallig'lanish va og'riq qoldiruvchi"),
    ("No-shpa (Drotaverin) 40mg", "Og'riq va isitma", "medicine", "home", "tabletka", "1-2 tabletka", 8.0, "dona", 2.0, "Spazm va sanchiqqa qarshi"),
    ("Mezim (Pankreatin)", "Oshqozon va hazm", "medicine", "home", "tabletka", "1-2 tabletka ovqat bilan", 12.0, "dona", 2.0, "Hazmni yaxshilash"),
    ("Aktivirlangan ko'mir", "Oshqozon va hazm", "medicine", "home", "tabletka", "1 tabletka / 10 kg vaznga", 20.0, "dona", 5.0, "Zaharlanish va intoksikatsiyada"),
    ("Tsetrin (Setirizin) 10mg", "Allergiya", "medicine", "home", "tabletka", "1 tabletka", 6.0, "dona", 2.0, "Allergiyaga qarshi"),
    ("Pantenol krem", "Namlantirish va tiklash", "skincare", "home", "maz", "Zarur joyga yupqa surtish", 1.0, "dona", 1.0, "Kuyish va terini tiklash uchun"),
    ("Xlorgeksidin 0.05%", "Antiseptik va bog'lov", "medicine", "home", "tomchi", "Zarur joyga surtish", 1.0, "dona", 1.0, "Antiseptik suyuqlik"),
    ("Steril bint va leykoplastir", "Antiseptik va bog'lov", "medicine", "home", "boshqa", "Zaruratga ko'ra", 5.0, "dona", 2.0, "Bog'lov vositalari"),
    ("Yumshoq yuvinish geli", "Tozalash va yuvinish", "skincare", "home", "boshqa", "Har tong va kechqurun", 1.0, "dona", 1.0, "Yuzni tozalash"),
    ("Namlantiruvchi krem", "Namlantirish va tiklash", "skincare", "home", "maz", "Yuvinishdan so'ng", 1.0, "dona", 1.0, "Terini namlantirish"),
    ("Quyoshdan himoya kremi SPF 50", "Quyoshdan himoya (SPF)", "skincare", "home", "maz", "Ko'chaga chiqishdan 15 daqiqa oldin", 1.0, "dona", 1.0, "UV himoya"),
    ("Gigiyenik lab balzami", "Lab va qo'l parvarishi", "skincare", "bag", "boshqa", "Muntazam", 1.0, "dona", 1.0, "Lab yorilishidan himoya"),
]

SEED_TRIAGE_RULES = [
    # (code, name, is_red_flag, natural_remedies, recommended_otc)
    (
        "chest_pain",
        "Ko'krak qafasidagi qattiq / bosuvchi og'riq",
        1,
        "Harakatlanmang, o'tiring yoki yarim yotgan holatda bo'ling, kiyim yoqasini bo'shating, chuqur va sekin nafas oling. O'zingiz transport haydamang!",
        "",
    ),
    (
        "vomiting_blood",
        "Qon qusish yoki to'q qora najas",
        1,
        "Hech narsa yemang va ichmang, qoringa sovuq kompress qo'ying, mutlaq tinch yoting.",
        "",
    ),
    (
        "fainting",
        "Hushdan ketish, kutilmagan nutq yoki ko'rish buzilishi",
        1,
        "Bemorning oyoqlarini biroz baland ko'taring, toza havo oqimini ta'minlang, tinchlik bering.",
        "",
    ),
    (
        "stiff_neck_fever",
        "Yuqori isitma (>39°C) + bo'yin qotishi yoki toshmalar",
        1,
        "Xonani shamollating, bemorga tinchlik bering, darhol tez tibbiy yordam chaqiring.",
        "",
    ),
    (
        "severe_breath",
        "Og'ir nafas qisilishi, bo'g'ilish hissi",
        1,
        "Derazani oching, tik yoki yarim o'tirgan holatni egallang, tor kiyimlarni yeching.",
        "",
    ),
    (
        "headache",
        "Bosh og'rig'i (yengil / o'rtacha)",
        0,
        "1-2 stakan iliq toza suv iching, qorong'i va tinch xonada 20 daqiqa ko'zlarni yumib dam oling, peshonaga salqin mato qo'ying.",
        "Paratsetamol yoki Ibuprofen",
    ),
    (
        "cold_flu",
        "Shamollash / Burun oqishi / Yengil tomoq og'rig'i",
        0,
        "Ko'p miqdorda iliq suyuqlik (limonli choy, na'matak damlamasi) iching. Iliq tuzli suv bilan tomoqni chayqang, dam oling.",
        "Paratsetamol, C vitamini, Tomoq pastilkasi",
    ),
    (
        "stomach_heaviness",
        "Oshqozonda og'irlik / Noqulaylik",
        0,
        "Og'ir va yog'li ovqatlardan saqlaning, yalpizli yoki moychechakli (romashka) iliq choy iching, biroz sekin piyoda yuring.",
        "Mezim (Pankreatin)",
    ),
    (
        "heartburn",
        "Zarda (jig'ildon qaynashi)",
        0,
        "Yotmang (tik turing yoki o'tiring), 1 stakan iliq suv iching. Kofe, shokolad, achchiq va qovurilgan ovqatlarni cheklang.",
        "Renni yoki Almagel",
    ),
    (
        "fatigue",
        "Charchoq / Madorsizlik",
        0,
        "To'liq 7-8 soat uxlashga harakat qiling, toza havoda 15 daqiqa sayr qiling, suv balansiga e'tibor bering.",
        "Vitamin C, Multivitamin",
    ),
]


def seed_health_data():
    """Retseptsiz dorilar, gigiyena/skincare vositalari va triage qoidalarini bazaga yuklaydi."""
    cat_map = {}
    for name, kind, icon, is_sys, is_def, sort_o in SEED_HEALTH_CATEGORIES:
        ex(
            "INSERT INTO health_categories (name, kind, icon, is_system, is_default, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, icon=excluded.icon",
            (name, kind, icon, is_sys, is_def, sort_o),
        )
    for row in q("SELECT id, name FROM health_categories"):
        cat_map[row["name"]] = row["id"]

    for code, name, is_red, natural, otc in SEED_TRIAGE_RULES:
        ex(
            "INSERT INTO health_triage_rules (symptom_code, symptom_name, is_red_flag, natural_remedies, recommended_otc) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symptom_code) DO UPDATE SET symptom_name=excluded.symptom_name, "
            "is_red_flag=excluded.is_red_flag, natural_remedies=excluded.natural_remedies, recommended_otc=excluded.recommended_otc",
            (code, name, is_red, natural, otc),
        )

    # Health items: agar birorta ham dori bo'lmasa, boshlang'ich zaxirani kiritamiz
    count = q1("SELECT COUNT(*) AS c FROM health_items")["c"]
    if count == 0:
        for name, cat_name, kind, loc, form, dosage, qty, unit, low, notes in SEED_HEALTH_ITEMS:
            cat_id = cat_map.get(cat_name)
            item_id = ins(
                "INSERT INTO health_items (name, kind, category_id, location, form, default_dosage, remaining_quantity, unit, low_stock_threshold, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, kind, cat_id, loc, form, dosage, qty, unit, low, notes),
            )
            if cat_id:
                ins(
                    "INSERT INTO health_item_categories (item_id, category_id) VALUES (?, ?)",
                    (item_id, cat_id),
                )

