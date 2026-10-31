"""AltronBek 3.0.1: biznes mantiq (Telegramga bog'liq emas, shuning uchun sinash oson)."""

import os
from datetime import date, datetime, timedelta

import requests

import app
import db
import qazo
import schedule_data as sd
import scoring

METHOD = os.environ.get("PRAYER_METHOD", "2")
WEEKDAY_NAMES = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
WEEKDAY_LOOKUP = {n.lower(): i for i, n in enumerate(WEEKDAY_NAMES)}
MODE_ICON = {"required": "✅", "optional": "💡", "off": "🔕"}
MODE_NAME = {"required": "majburiy", "optional": "ixtiyoriy", "off": "o'chirilgan"}


def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def get_city() -> str:
    return db.get_setting("city", os.environ.get("CITY", "Chirchiq"))


# ------------------------------------------------------------------ namoz vaqtlari

_prayer_cache: dict = {}
_fail_until: dict = {}


def fetch_prayer_times(city: str, country: str, d: date) -> dict:
    key = (city, country, d.isoformat())
    if key in _prayer_cache:
        return _prayer_cache[key]
    if _fail_until.get(key) and app.now() < _fail_until[key]:
        raise RuntimeError("Namoz vaqtlari manbasi vaqtincha javob bermayapti")
    try:
        resp = requests.get(
            "https://api.aladhan.com/v1/timingsByCity",
            params={"city": city, "country": country, "method": METHOD, "date": d.strftime("%d-%m-%Y")},
            timeout=10,
        )
        resp.raise_for_status()
        t = resp.json()["data"]["timings"]
    except Exception:
        _fail_until[key] = app.now() + timedelta(minutes=5)
        raise
    result = {
        "Bomdod": t["Fajr"][:5], "Quyosh": t["Sunrise"][:5], "Peshin": t["Dhuhr"][:5],
        "Asr": t["Asr"][:5], "Shom": t["Maghrib"][:5], "Xufton": t["Isha"][:5],
    }
    _prayer_cache[key] = result
    return result


# ------------------------------------------------------------------ shablonlarni yuklash

def seed_standard_reminders():
    """Standart shablonlarni bazaga bir marta yuklaydi (keyin /vaqt o'zgartirishlari buzilmaydi)."""
    if db.q1("SELECT COUNT(*) AS c FROM reminders WHERE source = 'standard'")["c"] > 0:
        return
    sql = (
        "INSERT INTO reminders (slug, source, kind, weekday, start, end, title, note, priority, scored, points) "
        "VALUES (?, 'standard', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    for item in sd.DAILY:
        db.ins(sql, (item["slug"], "daily", None, item["start"], item.get("end"), item["title"],
                     item.get("note", ""), item.get("priority", 1), int(item.get("scored", False)),
                     item.get("points", 0)))
    for weekday, items in sd.WEEKLY.items():
        for item in items:
            db.ins(sql, (item["slug"], "weekly", weekday, item["start"], item.get("end"), item["title"],
                         item.get("note", ""), item.get("priority", 1), int(item.get("scored", False)),
                         item.get("points", 0)))


# ------------------------------------------------------------------ pauza

def pause_until() -> date | None:
    raw = db.get_setting("pause_until")
    return date.fromisoformat(raw) if raw else None


def pause_active(d: date) -> bool:
    p = pause_until()
    return p is not None and d <= p


def set_pause(days: int):
    db.set_setting("pause_until", (app.today() + timedelta(days=days - 1)).isoformat())


def end_pause():
    db.del_setting("pause_until")


# ------------------------------------------------------------------ kunlik eslatmalar

def effective_mode(r: dict, d: date) -> str:
    """O'chirilgan eslatma muddati o'tgan bo'lsa, majburiy deb hisoblanadi."""
    if r["mode"] == "off" and r["off_until"] and r["off_until"] < d.isoformat():
        return "required"
    return r["mode"]


def reminders_for_day(d: date) -> list[dict]:
    """Kun uchun eslatmalar (standart + maxsus), bir kunlik o'zgartirishlar bilan. Pauzada bo'sh."""
    if pause_active(d):
        return []
    rows = db.q(
        "SELECT * FROM reminders WHERE active = 1 AND (kind = 'daily' "
        "OR (kind = 'weekly' AND weekday = ?) OR (kind = 'once' AND date = ?))",
        (d.weekday(), d.isoformat()),
    )
    overrides = {o["reminder_id"]: o for o in db.q("SELECT * FROM overrides WHERE date = ?", (d.isoformat(),))}
    result = []
    for r in rows:
        mode = effective_mode(r, d)
        if mode == "off":
            continue
        r["eff_mode"] = mode
        ov = overrides.get(r["id"])
        if ov:
            r["start"] = ov["start"] or r["start"]
            r["end"] = ov["end"] or r["end"]
        result.append(r)
    result.sort(key=lambda x: x["start"])
    return result


def disabled_for_day(d: date) -> list[dict]:
    """Shu kun uchun vaqtincha o'chirilgan (off) eslatmalar (jurnalda ko'rsatish uchun)."""
    if pause_active(d):
        return []
    rows = db.q(
        "SELECT * FROM reminders WHERE active = 1 AND mode = 'off' AND (kind = 'daily' "
        "OR (kind = 'weekly' AND weekday = ?) OR (kind = 'once' AND date = ?))",
        (d.weekday(), d.isoformat()),
    )
    return [r for r in rows if effective_mode(r, d) == "off"]


def resume_expired_off() -> list[str]:
    """Muddati o'tgan 'o'chirilgan' eslatmalarni qayta yoqadi. Nomlar ro'yxatini qaytaradi."""
    rows = db.q(
        "SELECT DISTINCT title FROM reminders WHERE mode = 'off' AND off_until IS NOT NULL AND off_until < ?",
        (app.today().isoformat(),),
    )
    if rows:
        db.ex(
            "UPDATE reminders SET mode = 'required', off_until = NULL "
            "WHERE mode = 'off' AND off_until IS NOT NULL AND off_until < ?",
            (app.today().isoformat(),),
        )
    return [r["title"] for r in rows]


def ensure_occurrence(reminder: dict, d: date) -> int:
    scheduled = app.at(d, reminder["start"]).isoformat()
    optional = 1 if reminder.get("eff_mode", reminder.get("mode")) == "optional" else 0
    row = db.q1(
        "SELECT id, status, is_optional FROM occurrences WHERE reminder_id = ? AND date = ? AND scheduled_at = ? AND prayer IS NULL",
        (reminder["id"], d.isoformat(), scheduled),
    )
    if row:
        if row["status"] == "pending" and row["is_optional"] != optional:
            db.ex("UPDATE occurrences SET is_optional = ? WHERE id = ?", (optional, row["id"]))
        return row["id"]
    return db.ins(
        "INSERT INTO occurrences (reminder_id, date, scheduled_at, title, scored, is_optional) VALUES (?,?,?,?,?,?)",
        (reminder["id"], d.isoformat(), scheduled, reminder["title"], reminder["scored"], optional),
    )


def ensure_prayer_occurrence(prayer: str, d: date, hhmm: str) -> int:
    scheduled = app.at(d, hhmm).isoformat()
    row = db.q1("SELECT id, status, scheduled_at FROM occurrences WHERE prayer = ? AND date = ?", (prayer, d.isoformat()))
    if row:
        if row["status"] == "pending" and row["scheduled_at"] != scheduled:
            db.ex("UPDATE occurrences SET scheduled_at = ? WHERE id = ?", (scheduled, row["id"]))
        return row["id"]
    return db.ins(
        "INSERT INTO occurrences (prayer, date, scheduled_at, title, scored) VALUES (?,?,?,?,0)",
        (prayer, d.isoformat(), scheduled, f"{prayer} namozi"),
    )


# ------------------------------------------------------------------ holatlar

def get_occurrence(occ_id: int) -> dict | None:
    return db.q1("SELECT * FROM occurrences WHERE id = ?", (occ_id,))


def get_reminder_of(occ: dict) -> dict | None:
    if occ.get("reminder_id") is None:
        return None
    return db.q1("SELECT * FROM reminders WHERE id = ?", (occ["reminder_id"],))


def get_priority(occ: dict) -> int:
    r = get_reminder_of(occ)
    return r["priority"] if r else 1


def get_points(occ: dict) -> int:
    r = get_reminder_of(occ)
    return r["points"] if r else 0


def mark_done(occ_id: int) -> dict:
    """Bajarildi. Kechikish faqat ma'lumot (jarima yo'q). {'late_minutes','points','was_postponed','date'}"""
    occ = get_occurrence(occ_id)
    if occ is None:
        return {"error": "topilmadi"}
    if occ["status"] == "done":
        return {"error": "allaqachon belgilangan"}
    current = app.now()
    late = max(0, int((current - datetime.fromisoformat(occ["scheduled_at"])).total_seconds() // 60))
    was_postponed = bool(occ.get("is_rescheduled"))
    db.ex("UPDATE occurrences SET status='done', done_at=?, late_minutes=? WHERE id=?",
          (current.isoformat(), late, occ_id))
    points = 0
    if occ["scored"] and not occ["is_subtask"]:
        points = scoring.award_points(occ_id, get_points(occ), was_postponed)
    return {"late_minutes": late, "points": points, "was_postponed": was_postponed, "date": occ["date"]}


def mark_skipped(occ_id: int) -> bool:
    occ = get_occurrence(occ_id)
    if occ is None or occ["status"] == "done":
        return False
    db.ex("UPDATE occurrences SET status='skipped' WHERE id=?", (occ_id,))
    return True


def set_optional_status(occ_id: int, status: str) -> bool:
    """Ixtiyoriy eslatma: 'planned' (boraman) yoki 'declined' (bugun emas)."""
    occ = get_occurrence(occ_id)
    if occ is None or occ["status"] == "done":
        return False
    db.ex("UPDATE occurrences SET status=? WHERE id=?", (status, occ_id))
    return True


def postpone(occ_id: int, target: datetime) -> int:
    occ = get_occurrence(occ_id)
    if occ is None or occ["status"] in ("done", "qazo", "qazo_done"):
        return 0
    db.ex("UPDATE occurrences SET status='postponed', postponed_to=? WHERE id=?",
          (target.isoformat(), occ_id))
    return db.ins(
        "INSERT INTO occurrences (reminder_id, prayer, date, scheduled_at, title, status, scored, is_optional, is_rescheduled) "
        "VALUES (?,?,?,?,?, 'pending', ?, ?, 1)",
        (occ["reminder_id"], None, target.date().isoformat(), target.isoformat(), occ["title"],
         occ["scored"], occ["is_optional"]),
    )


def quick_log_choices() -> list[dict]:
    """Tezkor belgilash uchun: ballli eslatmalar (standart slug bo'yicha guruhlangan)."""
    today_wd = app.today().weekday()
    rows = db.q("SELECT * FROM reminders WHERE active = 1 AND scored = 1 ORDER BY id")
    groups: dict = {}
    for r in rows:
        key = ("s", r["slug"]) if r["source"] == "standard" and r["slug"] else ("c", r["id"])
        best = groups.get(key)
        fits_today = r["kind"] == "daily" or r["weekday"] == today_wd
        if best is None or (fits_today and not best[1]):
            groups[key] = (r, fits_today)
    return [g[0] for g in groups.values()]


def quick_log(reminder_id: int) -> dict:
    """Ro'yxatdan tashqari bajarilgan ishni qayd etish. Bugun eslatma bo'lsa shuni, bo'lmasa yangisini belgilaydi."""
    rem = db.q1("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    if not rem:
        return {"error": "topilmadi"}
    today = app.today().isoformat()
    occ = db.q1(
        "SELECT * FROM occurrences WHERE reminder_id = ? AND date = ? AND prayer IS NULL "
        "ORDER BY (status = 'done') DESC, id DESC",
        (reminder_id, today),
    )
    if occ and occ["status"] == "done":
        return {"error": "bugun allaqachon belgilangan", "title": rem["title"]}
    if occ is None:
        optional = 1 if effective_mode(rem, app.today()) != "required" else 0
        occ_id = db.ins(
            "INSERT INTO occurrences (reminder_id, date, scheduled_at, title, scored, is_optional, is_manual) "
            "VALUES (?,?,?,?,?,?,1)",
            (reminder_id, today, app.now().isoformat(), rem["title"], rem["scored"], optional),
        )
    else:
        occ_id = occ["id"]
    result = mark_done(occ_id)
    result["title"] = rem["title"]
    return result


# ------------------------------------------------------------------ qazo avtomatik aniqlash

def has_pending_past_prayers(current: datetime) -> bool:
    """Tezkor tekshiruv: kutilayotgan namoz bo'lmasa HTTP va thread ochilmaydi."""
    d0 = current.date().isoformat()
    d1 = (current.date() - timedelta(days=1)).isoformat()
    row = db.q1(
        "SELECT 1 FROM occurrences WHERE prayer IS NOT NULL AND date IN (?, ?) "
        "AND status = 'pending' AND scheduled_at <= ? LIMIT 1",
        (d0, d1, current.isoformat()),
    )
    return row is not None


def find_new_qazo(current: datetime, city: str, country: str) -> list[dict]:
    """Muddati o'tgan, belgilanmagan namozlar uchun qazo yaratadi (bloklovchi: thread'da chaqiring)."""
    created = []
    for offset in (0, -1):
        d = (current + timedelta(days=offset)).date()
        rows = db.q("SELECT id, prayer, status FROM occurrences WHERE prayer IS NOT NULL AND date = ? AND status = 'pending'",
                    (d.isoformat(),))
        if not rows:
            continue
        try:
            today_t = fetch_prayer_times(city, country, d)
            next_t = fetch_prayer_times(city, country, d + timedelta(days=1))
        except Exception:
            continue
        dts = [app.at(d, today_t[p]) for p in sd.PRAYER_NAMES]
        next_fajr = app.at(d + timedelta(days=1), next_t["Bomdod"])
        for row in rows:
            deadline = qazo.qazo_deadline(sd.PRAYER_NAMES.index(row["prayer"]), dts, next_fajr)
            if deadline and current >= deadline:
                made = qazo.create_qazo(row["id"], row["prayer"], d, current.isoformat())
                if made:
                    created.append(made)
    return created


# ------------------------------------------------------------------ maktab va universitet

def school_classes(weekday: int) -> list[str]:
    raw = db.get_setting(f"school_classes_{weekday}")
    return raw.split(",") if raw else sd.SCHOOL_CLASSES.get(weekday, [])


def set_school_classes(weekday: int, classes: list[str]):
    db.set_setting(f"school_classes_{weekday}", ",".join(classes))


def school_lesson_reminders(d: date) -> list[dict]:
    """Har dars tugashidan 5 daqiqa oldin keyingi dars sinfi (ma'lumot, ballsiz)."""
    classes = school_classes(d.weekday())
    out = []
    for i in range(len(classes) - 1):
        _, end = sd.SCHOOL_BELLS[i + 1]
        out.append({"at": app.at(d, end) - timedelta(minutes=5),
                    "text": f"Keyingi dars: {i + 2}-soat, {classes[i + 1]}"})
    return out


def university_reminder(d: date) -> dict | None:
    lessons = sd.UNIVERSITY.get(d.weekday())
    if not lessons:
        return None
    lines = [f"{s}-{e}  {name} ({room})" for s, e, name, room in lessons]
    return {"at": app.at(d, "12:45"), "text": "Bugungi universitet darslari:\n" + "\n".join(lines)}
