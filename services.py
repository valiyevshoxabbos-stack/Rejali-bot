"""
RejaliBot 2.0: biznes mantiq (Telegramga bog'liq emas, shuning uchun sinash oson).
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import db
import scoring
import qazo
import schedule_data as sd

TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
METHOD = os.environ.get("PRAYER_METHOD", "2")

WEEKDAY_NAMES = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
WEEKDAY_LOOKUP = {n.lower(): i for i, n in enumerate(WEEKDAY_NAMES)}


def now() -> datetime:
    return datetime.now(TZ)


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def at(d: date, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


# ---------------------------------------------------------------- shablonlarni yuklash

def seed_standard_reminders():
    """
    schedule_data.py dagi standart shablonlarni bazaga bir marta yuklaydi.
    Baza bo'sh bo'lmasa (standart eslatmalar allaqachon bor), tegmaydi:
    foydalanuvchi /vaqt bilan o'zgartirgan vaqtlar buzilmasligi uchun.
    """
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM reminders WHERE source = 'standard'").fetchone()["c"]
        if count > 0:
            return
        for item in sd.DAILY:
            conn.execute(
                "INSERT INTO reminders (slug, source, kind, start, end, title, note, priority, scored, points) "
                "VALUES (?, 'standard', 'daily', ?, ?, ?, ?, ?, ?, ?)",
                (item["slug"], item["start"], item.get("end"), item["title"],
                 item.get("note", ""), item.get("priority", 1), int(item.get("scored", False)),
                 item.get("points", 0)),
            )
        for weekday, items in sd.WEEKLY.items():
            for item in items:
                conn.execute(
                    "INSERT INTO reminders "
                    "(slug, source, kind, weekday, start, end, title, note, priority, scored, points) "
                    "VALUES (?, 'standard', 'weekly', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item["slug"], weekday, item["start"], item.get("end"), item["title"],
                     item.get("note", ""), item.get("priority", 1), int(item.get("scored", False)),
                     item.get("points", 0)),
                )


# ---------------------------------------------------------------- namoz vaqtlari

_prayer_cache: dict[tuple, dict] = {}


def fetch_prayer_times(city: str, country: str, d: date) -> dict[str, str]:
    key = (city, country, d.isoformat())
    if key in _prayer_cache:
        return _prayer_cache[key]
    resp = requests.get(
        "https://api.aladhan.com/v1/timingsByCity",
        params={"city": city, "country": country, "method": METHOD, "date": d.strftime("%d-%m-%Y")},
        timeout=10,
    )
    resp.raise_for_status()
    t = resp.json()["data"]["timings"]
    result = {
        "Bomdod": t["Fajr"][:5],
        "Quyosh": t["Sunrise"][:5],
        "Peshin": t["Dhuhr"][:5],
        "Asr": t["Asr"][:5],
        "Shom": t["Maghrib"][:5],
        "Xufton": t["Isha"][:5],
    }
    _prayer_cache[key] = result
    return result


# ---------------------------------------------------------------- kunlik eslatmalar ro'yxati

def reminders_for_day(d: date) -> list[dict]:
    """
    Kun uchun barcha eslatmalar (standart + maxsus), bir kunlik o'zgartirishlar
    hisobga olingan holda, vaqt bo'yicha saralangan.
    """
    weekday = d.weekday()
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM reminders
            WHERE active = 1 AND (
                kind = 'daily'
                OR (kind = 'weekly' AND weekday = ?)
                OR (kind = 'once' AND date = ?)
            )
            """,
            (weekday, d.isoformat()),
        ).fetchall()
        overrides = {
            r["reminder_id"]: r
            for r in conn.execute("SELECT * FROM overrides WHERE date = ?", (d.isoformat(),)).fetchall()
        }
    result = []
    for r in rows:
        item = dict(r)
        ov = overrides.get(item["id"])
        if ov:
            if ov["start"]:
                item["start"] = ov["start"]
            if ov["end"]:
                item["end"] = ov["end"]
        result.append(item)
    result.sort(key=lambda x: x["start"])
    return result


def ensure_occurrence(reminder: dict, d: date) -> int:
    """Bugungi eslatma uchun occurrence yozuvini yaratadi (mavjud bo'lsa qaytaradi)."""
    scheduled = at(d, reminder["start"]).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM occurrences WHERE reminder_id = ? AND date = ? AND scheduled_at = ? AND prayer IS NULL",
            (reminder["id"], d.isoformat(), scheduled),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO occurrences (reminder_id, date, scheduled_at, title, scored) VALUES (?, ?, ?, ?, ?)",
            (reminder["id"], d.isoformat(), scheduled, reminder["title"], reminder["scored"]),
        )
        return cur.lastrowid


def ensure_prayer_occurrence(prayer: str, d: date, hhmm: str) -> int:
    scheduled = at(d, hhmm).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM occurrences WHERE prayer = ? AND date = ?", (prayer, d.isoformat())
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO occurrences (prayer, date, scheduled_at, title, scored) VALUES (?, ?, ?, ?, 0)",
            (prayer, d.isoformat(), scheduled, f"{prayer} namozi"),
        )
        return cur.lastrowid


# ---------------------------------------------------------------- holatlarni belgilash

def get_occurrence(occ_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM occurrences WHERE id = ?", (occ_id,)).fetchone()
    return dict(row) if row else None


def get_priority(occ: dict) -> int:
    if occ.get("reminder_id") is None:
        return 1
    with db.connect() as conn:
        row = conn.execute("SELECT priority FROM reminders WHERE id = ?", (occ["reminder_id"],)).fetchone()
    return row["priority"] if row else 1


def get_points(occ: dict) -> int:
    """Eslatmaga qo'lda belgilangan ball qiymati (namoz/qazo uchun 0)."""
    if occ.get("reminder_id") is None:
        return 0
    with db.connect() as conn:
        row = conn.execute("SELECT points FROM reminders WHERE id = ?", (occ["reminder_id"],)).fetchone()
    return row["points"] if row else 0


def mark_done(occ_id: int) -> dict:
    """
    Bajarildi. Kechikish daqiqasi hisoblanadi (faqat ma'lumot uchun, jarima yo'q).
    Natija: {'late_minutes', 'points', 'was_postponed'}
    """
    occ = get_occurrence(occ_id)
    if occ is None:
        return {"error": "topilmadi"}
    if occ["status"] in ("done",):
        return {"error": "allaqachon belgilangan"}

    current = now()
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    late = max(0, int((current - scheduled).total_seconds() // 60))
    was_postponed = bool(occ.get("is_rescheduled"))

    with db.connect() as conn:
        conn.execute(
            "UPDATE occurrences SET status = 'done', done_at = ?, late_minutes = ? WHERE id = ?",
            (current.isoformat(), late, occ_id),
        )

    points = 0
    if occ["scored"] and not occ["is_subtask"]:
        points = scoring.award_points(occ_id, get_points(occ), was_postponed)
    return {"late_minutes": late, "points": points, "was_postponed": was_postponed}


def mark_skipped(occ_id: int) -> bool:
    occ = get_occurrence(occ_id)
    if occ is None or occ["status"] == "done":
        return False
    with db.connect() as conn:
        conn.execute("UPDATE occurrences SET status = 'skipped' WHERE id = ?", (occ_id,))
    return True


def postpone(occ_id: int, target: datetime) -> bool:
    """
    Boshqa vaqt/kunga ko'chirish. Asl occurrence 'postponed' bo'ladi, yangisi yaratiladi.
    Yangi occurrence ball oladi (qisman), chunki postponed_to belgilanadi.
    """
    occ = get_occurrence(occ_id)
    if occ is None or occ["status"] in ("done", "qazo", "qazo_done"):
        return False
    with db.connect() as conn:
        conn.execute(
            "UPDATE occurrences SET status = 'postponed', postponed_to = ? WHERE id = ?",
            (target.isoformat(), occ_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO occurrences "
            "(reminder_id, date, scheduled_at, title, status, scored, is_rescheduled) "
            "VALUES (?, ?, ?, ?, 'pending', ?, 1)",
            (occ["reminder_id"], target.date().isoformat(), target.isoformat(),
             occ["title"], occ["scored"]),
        )
    return True


# ---------------------------------------------------------------- qazo avtomatik aniqlash

def find_new_qazo(current: datetime, city: str, country: str) -> list[dict]:
    """
    Muddati o'tgan, hali belgilanmagan namozlarni topib, qazo yaratadi.
    Qoida: keyingi namoz kirgandan 15 daqiqa o'tgach (barcha namozlar uchun).
    Qaytaradi: yangi yaratilgan qazolar ro'yxati (bot ularni xabar qiladi).
    """
    created = []
    for offset in (0, -1):  # bugun va kecha (kechagi xufton uchun)
        d = (current + timedelta(days=offset)).date()
        try:
            times_today = fetch_prayer_times(city, country, d)
            times_next = fetch_prayer_times(city, country, d + timedelta(days=1))
        except Exception:
            continue
        prayer_dts = [at(d, times_today[p]) for p in sd.PRAYER_NAMES]
        next_fajr = at(d + timedelta(days=1), times_next["Bomdod"])

        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, prayer, status FROM occurrences WHERE prayer IS NOT NULL AND date = ?",
                (d.isoformat(),),
            ).fetchall()
        for row in rows:
            if row["status"] != "pending":
                continue
            idx = sd.PRAYER_NAMES.index(row["prayer"])
            deadline = qazo.qazo_deadline(idx, prayer_dts, next_fajr)
            if deadline and current >= deadline:
                made = qazo.create_qazo(row["id"], row["prayer"], d, current.isoformat())
                if made:
                    created.append(made)
    return created


# ---------------------------------------------------------------- maktab va universitet

def school_lesson_reminders(d: date) -> list[dict]:
    """
    Har dars tugashidan 5 daqiqa oldin: keyingi dars qaysi sinfda.
    Oxirgi darsdan keyin eslatma yo'q. Sub-task: ball va statistikaga kirmaydi.
    """
    classes = _school_classes(d.weekday())
    result = []
    for i in range(len(classes) - 1):
        lesson_no = i + 1
        _, end = sd.SCHOOL_BELLS[lesson_no]
        remind_at = at(d, end) - timedelta(minutes=5)
        result.append({
            "at": remind_at,
            "text": f"Keyingi dars: {lesson_no + 1}-soat, {classes[i + 1]}",
        })
    return result


def _school_classes(weekday: int) -> list[str]:
    raw = db.get_setting(f"school_classes_{weekday}")
    if raw:
        return raw.split(",")
    return sd.SCHOOL_CLASSES.get(weekday, [])


def set_school_classes(weekday: int, classes: list[str]):
    db.set_setting(f"school_classes_{weekday}", ",".join(classes))


def university_reminder(d: date) -> dict | None:
    """Universitet darsi boshlanishidan oldin (12:45) o'sha kun darslari."""
    lessons = sd.UNIVERSITY.get(d.weekday())
    if not lessons:
        return None
    lines = [f"{s}-{e}  {name} ({room})" for s, e, name, room in lessons]
    return {"at": at(d, "12:45"), "text": "Bugungi universitet darslari:\n" + "\n".join(lines)}
