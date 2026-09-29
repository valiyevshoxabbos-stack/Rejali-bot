"""
RejaliBot 2.0: ball tizimi.

Qoidalar (spetsifikatsiyadan):
  - Ball yig'iladi, kamaymaydi. Haftalik maqsad (219) yo'q, faqat solishtirish.
  - Har bir eslatmaning ball qiymati QO'LDA belgilanadi (reminders.points),
    ustuvorlik darajasidan mustaqil. Ustuvorlik (1/2/3) faqat ko'rsatish uchun.
  - Faqat scored=1 bo'lgan eslatmalar ball beradi.
  - Vaqtida yoki kech bajarilsa to'liq ball qo'shiladi (kechikish faqat
    timerda ko'rinadi, jarima yo'q).
  - Bajarilmasa ball qo'shilmaydi.
  - Kechiktirilgan (boshqa kunga surilgan) vazifa keyinroq bajarilsa,
    yarim ball oladi.
  - Qazo va sub-tasklar ball bermaydi.
"""

from datetime import date, timedelta

import db

POSTPONED_FACTOR = 0.5  # kechiktirilib keyin bajarilganda ballning yarmi
PRIORITY_NAMES = {1: "oddiy", 2: "muhim", 3: "juda muhim"}
PRIORITY_ICON = {1: "", 2: "❗", 3: "🔥"}


def week_bounds(d: date) -> tuple[date, date]:
    """Dushanbadan yakshanbagacha."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def award_points(occurrence_id: int, base_points: int, was_postponed: bool = False) -> int:
    """Bajarilgan eslatma uchun ball beradi."""
    pts = int(base_points * POSTPONED_FACTOR) if was_postponed else base_points
    with db.connect() as conn:
        conn.execute("UPDATE occurrences SET points = ? WHERE id = ?", (pts, occurrence_id))
    return pts


def week_summary(monday: date) -> dict:
    """Bir hafta uchun: ball, bajarilgan/jami (ballli eslatmalar bo'yicha)."""
    sunday = monday + timedelta(days=6)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT status, points, scored FROM occurrences
            WHERE date BETWEEN ? AND ? AND is_subtask = 0 AND scored = 1
            """,
            (monday.isoformat(), sunday.isoformat()),
        ).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    points = sum(r["points"] for r in rows)
    percent = round(done / total * 100) if total else 0
    return {"monday": monday, "points": points, "done": done, "total": total, "percent": percent}


def weekly_history(weeks: int, today: date) -> list[dict]:
    monday, _ = week_bounds(today)
    history = []
    for i in range(weeks):
        m = monday - timedelta(weeks=i)
        history.append(week_summary(m))
    return history


def record_week() -> dict | None:
    """Barcha davrdagi eng yaxshi hafta (ball bo'yicha)."""
    with db.connect() as conn:
        first = conn.execute("SELECT MIN(date) AS d FROM occurrences").fetchone()
    if not first or not first["d"]:
        return None
    start = date.fromisoformat(first["d"])
    monday, _ = week_bounds(start)
    today = date.today()
    best = None
    while monday <= today:
        s = week_summary(monday)
        if best is None or s["points"] > best["points"]:
            best = s
        monday += timedelta(weeks=1)
    return best


def month_summary(year: int, month: int) -> dict:
    first = date(year, month, 1)
    nxt = date(year + (month // 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT status, points FROM occurrences
            WHERE date BETWEEN ? AND ? AND is_subtask = 0 AND scored = 1
            """,
            (first.isoformat(), last.isoformat()),
        ).fetchall()
    total = len(rows)
    done = sum(1 for r in rows if r["status"] == "done")
    points = sum(r["points"] for r in rows)
    percent = round(done / total * 100) if total else 0
    return {"year": year, "month": month, "points": points, "done": done, "total": total, "percent": percent}


def format_delta(current: int, previous: int) -> str:
    diff = current - previous
    if diff > 0:
        return f"▲ +{diff}"
    if diff < 0:
        return f"▼ {diff}"
    return "= o'zgarmagan"
