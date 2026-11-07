"""
Ball tizimi (3.5). Ball yig'iladi, kamaymaydi. Har eslatmaning balli qo'lda belgilanadi.
Vaqtida yoki kech bajarilsa to'liq ball (kechikish faqat timerda ko'rinadi).
Kechiktirilib keyin bajarilsa yarim ball. Qazo va sub-task ball bermaydi.
Ixtiyoriy (optional) eslatmalar foiz hisobiga kirmaydi, bajarilsa faqat ball (bonus) beradi.
Chelenjning kunlik vazifalari to'liq yopilganda ham kunlik bonus ball qo'shiladi.
"""

from datetime import date, timedelta

import app
import db

POSTPONED_FACTOR = 0.5
PRIORITY_NAMES = {1: "oddiy", 2: "muhim", 3: "juda muhim"}
PRIORITY_ICON = {1: "", 2: "❗", 3: "🔥"}
DEFAULT_CHALLENGE_BONUS = 10


def week_bounds(d: date):
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def award_points(occ_id: int, base: int, was_postponed: bool = False) -> int:
    pts = int(base * POSTPONED_FACTOR) if was_postponed else base
    db.ex("UPDATE occurrences SET points = ? WHERE id = ?", (pts, occ_id))
    return pts


def set_challenge_day_bonus(challenge_id: int, d: date, points: int = DEFAULT_CHALLENGE_BONUS, completed: bool = True) -> int:
    """
    Chelenjning `d` sanasidagi vazifalari to'liq yopilganda bonus ball yozadi,
    aks holda (bekor qilingan/o'zgartirilgan bo'lsa) shu kunlik bonusni olib tashlaydi.
    Yangi qo'shilgan ball miqdorini qaytaradi (avval berilgan bo'lsa 0).
    """
    d_iso = d.isoformat()
    existing = db.q1(
        "SELECT points FROM challenge_bonuses WHERE challenge_id = ? AND bonus_date = ?",
        (challenge_id, d_iso),
    )
    if completed:
        if existing:
            return 0
        pts = max(1, int(points))
        db.ex(
            "INSERT INTO challenge_bonuses (challenge_id, bonus_date, points) VALUES (?,?,?)",
            (challenge_id, d_iso, pts),
        )
        return pts
    if existing:
        db.ex("DELETE FROM challenge_bonuses WHERE challenge_id = ? AND bonus_date = ?", (challenge_id, d_iso))
    return 0


def _challenge_bonus_range(first: date, last: date) -> tuple[int, int]:
    row = db.q1(
        "SELECT COALESCE(SUM(points), 0) AS pts, COUNT(*) AS cnt "
        "FROM challenge_bonuses WHERE bonus_date BETWEEN ? AND ?",
        (first.isoformat(), last.isoformat()),
    )
    if not row:
        return 0, 0
    return int(row["pts"]), int(row["cnt"])


def _summary(rows: list[dict], extra_points: int = 0, challenge_days: int = 0) -> dict:
    required = [r for r in rows if not r["is_optional"]]
    done = sum(1 for r in required if r["status"] == "done")
    bonus = sum(1 for r in rows if r["is_optional"] and r["status"] == "done") + challenge_days
    total = len(required)
    return {
        "points": sum(r["points"] for r in rows) + extra_points,
        "done": done,
        "total": total,
        "bonus": bonus,
        "challenge_points": extra_points,
        "challenge_days": challenge_days,
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
    last = monday + timedelta(days=6)
    ch_pts, ch_cnt = _challenge_bonus_range(monday, last)
    s = _summary(_range_rows(monday, last), ch_pts, ch_cnt)
    s["monday"] = monday
    return s


def day_summary(d: date) -> dict:
    ch_pts, ch_cnt = _challenge_bonus_range(d, d)
    return _summary(_range_rows(d, d), ch_pts, ch_cnt)


def month_summary(year: int, month: int) -> dict:
    first = date(year, month, 1)
    nxt = date(year + month // 12, month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    ch_pts, ch_cnt = _challenge_bonus_range(first, last)
    return _summary(_range_rows(first, last), ch_pts, ch_cnt)


def weekly_history(weeks: int, today: date) -> list[dict]:
    monday, _ = week_bounds(today)
    return [week_summary(monday - timedelta(weeks=i)) for i in range(weeks)]


def record_week(today: date):
    first_occ = db.q1("SELECT MIN(date) AS d FROM occurrences")
    first_ch = db.q1("SELECT MIN(bonus_date) AS d FROM challenge_bonuses")
    candidates = [x["d"] for x in (first_occ, first_ch) if x and x["d"]]
    if not candidates:
        return None
    monday, _ = week_bounds(date.fromisoformat(min(candidates)))
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
