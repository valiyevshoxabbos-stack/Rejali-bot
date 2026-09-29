"""
RejaliBot 2.0: qazo tizimi.

Qoida (foydalanuvchi belgilagan): namoz vaqti KEYINGI namoz vaqti kirgandan
15 daqiqa o'tgach qazo hisoblanadi. Bu barcha namozlar uchun bir xil,
jumladan bomdod uchun ham (masalan peshin 13:00 bo'lsa, bomdod 13:15 da qazo).
Xufton uchun keyingi namoz ertangi bomdod hisoblanadi.

Qazo uchun ball berilmaydi va ball ayrilmaydi.
"""

import re
from datetime import date, datetime, timedelta

import db

QAZO_GRACE_MINUTES = 15

# Kod nomlari (lotin, bo'sh joysiz, buyruq sifatida yozilishi mumkin)
PRAYER_SLUGS = {
    "Bomdod": "bomdod",
    "Peshin": "peshin",
    "Asr": "asr",
    "Shom": "shom",
    "Xufton": "xufton",
}
SLUG_TO_PRAYER = {v: k for k, v in PRAYER_SLUGS.items()}

# /bomdod_21_09 kabi kod
CODE_RE = re.compile(r"^/?(bomdod|peshin|asr|shom|xufton)_(\d{2})_(\d{2})$")


def make_code(prayer: str, d: date) -> str:
    return f"{PRAYER_SLUGS[prayer]}_{d.day:02d}_{d.month:02d}"


def parse_code(text: str) -> tuple[str, int, int] | None:
    """'/bomdod_21_09' -> ('bomdod', 21, 9), aks holda None."""
    m = CODE_RE.match(text.strip().lower())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def qazo_deadline(prayer_index: int, prayer_times: list[datetime], next_day_fajr: datetime | None) -> datetime | None:
    """
    prayer_times - kunning 5 ta namoz vaqti tartib bilan [Bomdod, Peshin, Asr, Shom, Xufton].
    Namoz uchun muddat = keyingi namoz vaqti + 15 daqiqa.
    Xufton uchun keyingi = ertangi bomdod (agar ma'lum bo'lsa).
    """
    if prayer_index < 4:
        nxt = prayer_times[prayer_index + 1]
    else:
        nxt = next_day_fajr
    if nxt is None:
        return None
    return nxt + timedelta(minutes=QAZO_GRACE_MINUTES)


def create_qazo(occurrence_id: int, prayer: str, d: date, now_iso: str) -> dict | None:
    """Qazo yozuvini yaratadi. Kod takrorlansa (bir kunda bir namoz) qayta yaratmaydi."""
    code = make_code(prayer, d)
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM qazo WHERE code = ?", (code,)).fetchone()
        if existing:
            return None
        conn.execute(
            "INSERT INTO qazo (occurrence_id, code, title, date, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (occurrence_id, code, f"{prayer} namozi", d.isoformat(), now_iso),
        )
        conn.execute("UPDATE occurrences SET status = 'qazo' WHERE id = ?", (occurrence_id,))
    return {"code": code, "title": f"{prayer} namozi", "date": d}


def mark_qazo_done(code: str, now_iso: str) -> bool:
    """Qazo namozi o'qilganini belgilaydi. Topilmasa yoki allaqachon o'qilgan bo'lsa False."""
    with db.connect() as conn:
        row = conn.execute("SELECT id, occurrence_id, status FROM qazo WHERE code = ?", (code,)).fetchone()
        if row is None or row["status"] == "done":
            return False
        conn.execute("UPDATE qazo SET status = 'done', done_at = ? WHERE id = ?", (now_iso, row["id"]))
        conn.execute("UPDATE occurrences SET status = 'qazo_done' WHERE id = ?", (row["occurrence_id"],))
    return True


def open_qazo() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT code, title, date FROM qazo WHERE status = 'open' ORDER BY date, id").fetchall()
    return [dict(r) for r in rows]


def qazo_created_on(d: date) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT code, title, date FROM qazo WHERE date = ? ORDER BY id", (d.isoformat(),)
        ).fetchall()
    return [dict(r) for r in rows]
