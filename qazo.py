"""
Qazo tizimi. Qoida (foydalanuvchi belgilagan): namoz KEYINGI namoz vaqti kirgandan
15 daqiqa o'tgach qazo hisoblanadi, barcha namozlar uchun (bomdod uchun ham:
peshin 13:00 bo'lsa, bomdod 13:15 da). Xufton uchun keyingi - ertangi bomdod.
Qazo uchun ball berilmaydi ham, ayirilmaydi ham.
"""

import re
from datetime import date, datetime, timedelta

import db

QAZO_GRACE_MINUTES = 15

PRAYER_SLUGS = {"Bomdod": "bomdod", "Peshin": "peshin", "Asr": "asr", "Shom": "shom", "Xufton": "xufton"}
CODE_RE = re.compile(r"^/?(bomdod|peshin|asr|shom|xufton)_(\d{2})_(\d{2})$")


def make_code(prayer: str, d: date) -> str:
    return f"{PRAYER_SLUGS[prayer]}_{d.day:02d}_{d.month:02d}"


def parse_code(text: str):
    m = CODE_RE.match(text.strip().lower())
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


def qazo_deadline(idx: int, prayer_times: list[datetime], next_day_fajr: datetime | None):
    nxt = prayer_times[idx + 1] if idx < 4 else next_day_fajr
    return nxt + timedelta(minutes=QAZO_GRACE_MINUTES) if nxt else None


def create_qazo(occurrence_id: int, prayer: str, d: date, now_iso: str):
    code = make_code(prayer, d)
    if db.q1("SELECT id FROM qazo WHERE code = ?", (code,)):
        return None
    db.ins(
        "INSERT INTO qazo (occurrence_id, code, title, date, status, created_at) VALUES (?,?,?,?, 'open', ?)",
        (occurrence_id, code, f"{prayer} namozi", d.isoformat(), now_iso),
    )
    db.ex("UPDATE occurrences SET status = 'qazo' WHERE id = ?", (occurrence_id,))
    return {"code": code, "title": f"{prayer} namozi", "date": d}


def mark_qazo_done(code: str, now_iso: str) -> bool:
    row = db.q1("SELECT id, occurrence_id, status FROM qazo WHERE code = ?", (code,))
    if not row or row["status"] == "done":
        return False
    db.ex("UPDATE qazo SET status = 'done', done_at = ? WHERE id = ?", (now_iso, row["id"]))
    db.ex("UPDATE occurrences SET status = 'qazo_done' WHERE id = ?", (row["occurrence_id"],))
    return True


def open_qazo() -> list[dict]:
    return db.q("SELECT code, title, date FROM qazo WHERE status = 'open' ORDER BY date, id")
