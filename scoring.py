"""
Ball tizimi. Ball yig'iladi, kamaymaydi. Har eslatmaning balli qo'lda belgilanadi.
Vaqtida yoki kech bajarilsa to'liq ball (kechikish faqat timerda ko'rinadi).
Kechiktirilib keyin bajarilsa yarim ball. Qazo va sub-task ball bermaydi.
Ixtiyoriy (optional) eslatmalar foiz hisobiga kirmaydi, bajarilsa faqat ball (bonus) beradi.
"""

from datetime import date, timedelta

import app
import db

POSTPONED_FACTOR = 0.5
PRIORITY_NAMES = {1: "oddiy", 2: "muhim", 3: "juda muhim"}
PRIORITY_ICON = {1: "", 2: "❗", 3: "🔥"}


def week_bounds(d: date):
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def award_points(occ_id: int, base: int, was_postponed: bool = False) -> int:
    pts = int(base * POSTPONED_FACTOR) if was_postponed else base
    db.ex("UPDATE occurrences SET points = ? WHERE id = ?", (pts, occ_id))
    return pts


def _summary(rows: list[dict]) -> dict:
    required = [r for r in rows if not r["is_optional"]]
    done = sum(1 for r in required if r["status"] == "done")
    bonus = sum(1 for r in rows if r["is_optional"] and r["status"] == "done")
    total = len(required)
    return {
        "points": sum(r["points"] for r in rows),
        "done": done, "total": total, "bonus": bonus,
        "percent": round(done / total * 100) if total else 0,
    }


def _range_rows(first: date, last: date) -> list[dict]:
    """Faqat vaqti kelgan (yoki allaqachon bajarilgan) eslatmalar hisoblanadi: kun davomida foiz sun'iy pasaymasin."""
    return db.q(
        "SELECT status, points, is_optional FROM occurrences "
        "WHERE date BETWEEN ? AND ? AND is_subtask = 0 AND scored = 1 "
        "AND (scheduled_at <= ? OR status = 'done')",
        (first.isoformat(), last.isoformat(), app.now().isoformat()),
    )


def week_summary(monday: date) -> dict:
    s = _summary(_range_rows(monday, monday + timedelta(days=6)))
    s["monday"] = monday
    return s


def day_summary(d: date) -> dict:
    return _summary(_range_rows(d, d))


def month_summary(year: int, month: int) -> dict:
    first = date(year, month, 1)
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return _summary(_range_rows(first, nxt - timedelta(days=1)))


def weekly_history(weeks: int, today: date) -> list[dict]:
    monday, _ = week_bounds(today)
    return [week_summary(monday - timedelta(weeks=i)) for i in range(weeks)]


def record_week(today: date):
    first = db.q1("SELECT MIN(date) AS d FROM occurrences")
    if not first or not first["d"]:
        return None
    monday, _ = week_bounds(date.fromisoformat(first["d"]))
    best = None
    while monday <= today:
        s = week_summary(monday)
        if best is None or s["points"] > best["points"]:
            best = s
        monday += timedelta(weeks=1)
    return best


def delta_text(current: int, previous: int) -> str:
    diff = current - previous
    if diff > 0:
        return f"▲ +{diff}"
    if diff < 0:
        return f"▼ {diff}"
    return "= o'zgarmagan"
