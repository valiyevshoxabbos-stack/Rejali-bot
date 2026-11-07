"""
Universal Chelenj Dvigateli (3.5.1 — feat_challenges.py):
- Hech qanday oldindan qotirilgan (hardcode) chelenj yo'q: barcha chelenj va vazifalar dinamik CRUD (FSM + inline) orqali boshqariladi.
- Yangi chelenjda boshlanish sanasi: «🟢 Bugundan» yoki «⏳ Ertadan» (ertadan tanlansa bugungi xulosaga kirmaydi va uzilish yozilmaydi).
- Har bir chelenj uchun ko'pi bilan 2 marta «🧊 Muzlatish (Skip Day)» imkoniyati.
- 4 xil vazifa turi:
  1) `binary` — Ha/Yo'q tugmasi
  2) `numeric` — sonli ko'rsatkich (masalan bet, km, daqiqa)
  3) `weekly_quota` — haftasiga N kun kvotasi (masalan 3/3 kun)
  4) `schedule` — haftaning belgilangan kunlari ('mon,wed,fri')
- Davomiylik qoidasi: o'tkazib yuborilgan kun chelenjni bekor qilmaydi va noldan boshlamaydi; kunlar hisobi davom etadi, faqat o'sha kun «to'liq emas» deb qayd etiladi.
- `scoring.py` integratsiyasi: kunlik barcha vazifalar to'liq yopilganda bonus ball beriladi.
- `/chelenjlar`, «🏆 Chelenjlar» menyusi va 21:45 kechki xulosaga inline boshqaruv paneli sifatida ulanadi.
"""

from datetime import date, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import feat_journal as journal
import messenger
import scoring

router = Router()

DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_UZ = {
    "mon": "Du", "tue": "Se", "wed": "Chor", "thu": "Pay",
    "fri": "Ju", "sat": "Sha", "sun": "Yak",
}
UZ_TO_KEY = {
    "du": "mon", "dush": "mon", "dushanba": "mon", "mon": "mon",
    "se": "tue", "sesh": "tue", "seshanba": "tue", "tue": "tue",
    "chor": "wed", "chorshanba": "wed", "wed": "wed",
    "pay": "thu", "payshanba": "thu", "thu": "thu",
    "ju": "fri", "juma": "fri", "fri": "fri",
    "sha": "sat", "shan": "sat", "shanba": "sat", "sat": "sat",
    "yak": "sun", "yakshanba": "sun", "sun": "sun",
}
TYPE_LABELS = {
    "binary": "☑️ Ha / Yo'q",
    "numeric": "🔢 Sonli maqsad",
    "weekly_quota": "📅 Haftalik kvota",
    "schedule": "🗓 Kunlar jadvali",
}


MAX_FREEZES_DEFAULT = 2


class ChFSM(StatesGroup):
    ch_title = State()
    ch_duration = State()
    ch_start = State()
    task_title = State()
    task_type = State()
    task_target = State()
    task_schedule = State()
    numeric_input = State()
    edit_ch_title = State()
    edit_ch_dur = State()
    edit_ch_bonus = State()
    edit_task_title = State()
    edit_task_target = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _fmt_num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def normalize_schedule_days(raw: str | list[str]) -> str:
    items = raw if isinstance(raw, list) else [x.strip() for x in (raw or "").replace(";", ",").replace(" ", ",").split(",")]
    found = []
    for it in items:
        k = UZ_TO_KEY.get(it.strip().lower())
        if k and k not in found:
            found.append(k)
    found.sort(key=lambda x: DAY_KEYS.index(x))
    return ",".join(found)


def parse_schedule_set(raw: str) -> list[str]:
    norm = normalize_schedule_days(raw or "")
    return [x for x in norm.split(",") if x] if norm else []


def fmt_schedule_uz(raw: str) -> str:
    days = parse_schedule_set(raw)
    return "-".join(DAY_UZ[k] for k in days) if days else "—"


# ------------------------------------------------------------------ Biznes mantiq va CRUD funksiyalar

def sync_lifecycle(ref_today: date | None = None):
    """Muddati tugagan ('end_date < today') faol chelenjlarni avtomatik 'completed' holatiga o'tkazadi."""
    d_iso = (ref_today or app.today()).isoformat()
    db.ex(
        "UPDATE challenges SET status = 'completed' WHERE status = 'active' AND end_date < ?",
        (d_iso,),
    )


def create_challenge(
    title: str,
    duration_days: int,
    start_date: date | None = None,
    bonus_points: int = scoring.DEFAULT_CHALLENGE_BONUS,
    max_freezes: int = MAX_FREEZES_DEFAULT,
) -> int:
    s_date = start_date or app.today()
    dur = max(1, int(duration_days))
    e_date = s_date + timedelta(days=dur - 1)
    return db.ins(
        "INSERT INTO challenges (title, duration_days, start_date, end_date, bonus_points, status, freezes_used, max_freezes) "
        "VALUES (?,?,?,?,?,'active',0,?)",
        (title.strip(), dur, s_date.isoformat(), e_date.isoformat(), max(1, int(bonus_points)), max(0, int(max_freezes))),
    )


def freeze_day(challenge_id: int, d: date | None = None) -> dict:
    """
    Chelenj kunini muzlatadi («🧊 Muzlatish (Skip Day)»).
    Har bir chelenj uchun ko'pi bilan `max_freezes` (standart 2) marta ruxsat etiladi.
    """
    d = d or app.today()
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
    if not ch or ch["status"] != "active":
        return {"ok": False, "error": "Faol chelenj topilmadi."}
    s_date = date.fromisoformat(ch["start_date"])
    e_date = date.fromisoformat(ch["end_date"])
    if d < s_date:
        return {"ok": False, "error": "Chelenj hali boshlanmagan."}
    if d > e_date:
        return {"ok": False, "error": "Chelenj muddati tugagan."}
    if db.q1("SELECT 1 FROM challenge_freezes WHERE challenge_id = ? AND freeze_date = ?", (challenge_id, d.isoformat())):
        return {"ok": False, "error": "Bugungi kun allaqachon muzlatilgan."}
    used = int(ch.get("freezes_used") or 0)
    limit = int(ch.get("max_freezes") if ch.get("max_freezes") is not None else MAX_FREEZES_DEFAULT)
    if used >= limit:
        return {"ok": False, "error": f"Muzlatish limiti tugagan ({used}/{limit} marta ishlatilgan)."}

    db.ex(
        "INSERT INTO challenge_freezes (challenge_id, freeze_date) VALUES (?,?)",
        (challenge_id, d.isoformat()),
    )
    new_used = used + 1
    db.ex("UPDATE challenges SET freezes_used = ? WHERE id = ?", (new_used, challenge_id))
    scoring.set_challenge_day_bonus(challenge_id, d, ch["bonus_points"], completed=False)
    return {
        "ok": True,
        "freezes_used": new_used,
        "max_freezes": limit,
        "remaining": max(0, limit - new_used),
    }


def add_task(
    challenge_id: int,
    title: str,
    task_type: str,
    target_value: float = 1.0,
    schedule_days: str = "",
    unit: str = "",
) -> int:
    if task_type not in TYPE_LABELS:
        raise ValueError(f"Noto'g'ri vazifa turi: {task_type}")
    sched = normalize_schedule_days(schedule_days) if task_type == "schedule" else ""
    tgt = 1.0 if task_type in ("binary", "schedule") else max(0.1, float(target_value))
    if task_type == "weekly_quota":
        tgt = float(min(7, max(1, int(round(tgt)))))
    return db.ins(
        "INSERT INTO challenge_tasks (challenge_id, title, task_type, target_value, unit, schedule_days, active) "
        "VALUES (?,?,?,?,?,?,1)",
        (challenge_id, title.strip(), task_type, tgt, unit.strip(), sched),
    )


def update_challenge(
    challenge_id: int,
    title: str | None = None,
    duration_days: int | None = None,
    bonus_points: int | None = None,
):
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
    if not ch:
        return
    new_title = title.strip() if title else ch["title"]
    new_dur = max(1, int(duration_days)) if duration_days is not None else int(ch["duration_days"])
    s_date = date.fromisoformat(ch["start_date"])
    new_end = (s_date + timedelta(days=new_dur - 1)).isoformat()
    new_bonus = max(1, int(bonus_points)) if bonus_points is not None else int(ch["bonus_points"])
    new_status = ch["status"]
    if new_status in ("active", "completed"):
        new_status = "completed" if new_end < app.today().isoformat() else "active"
    db.ex(
        "UPDATE challenges SET title = ?, duration_days = ?, end_date = ?, bonus_points = ?, status = ? WHERE id = ?",
        (new_title, new_dur, new_end, new_bonus, new_status, challenge_id),
    )


def update_task(
    task_id: int,
    title: str | None = None,
    target_value: float | None = None,
    schedule_days: str | None = None,
    unit: str | None = None,
):
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (task_id,))
    if not t:
        return
    new_title = title.strip() if title else t["title"]
    new_tgt = float(target_value) if target_value is not None else float(t["target_value"])
    if t["task_type"] == "weekly_quota":
        new_tgt = float(min(7, max(1, int(round(new_tgt)))))
    new_sched = normalize_schedule_days(schedule_days) if schedule_days is not None else t["schedule_days"]
    new_unit = unit.strip() if unit is not None else (t["unit"] or "")
    db.ex(
        "UPDATE challenge_tasks SET title = ?, target_value = ?, schedule_days = ?, unit = ? WHERE id = ?",
        (new_title, new_tgt, new_sched, new_unit, task_id),
    )


def delete_task(task_id: int):
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (task_id,))
    if not t:
        return
    db.ex("UPDATE challenge_tasks SET active = 0 WHERE id = ?", (task_id,))
    ev = evaluate_day(t["challenge_id"], app.today())
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (t["challenge_id"],))
    if ch:
        scoring.set_challenge_day_bonus(
            ch["id"], app.today(), ch["bonus_points"], completed=(ev["day_status"] == "complete")
        )


def cancel_challenge(challenge_id: int):
    db.ex("UPDATE challenges SET status = 'cancelled' WHERE id = ?", (challenge_id,))


def _weekly_done_count(task_id: int, d: date, ch_start: date, ch_end: date) -> tuple[int, int]:
    """Shu haftada (Du..Yak, chelenj oralig'ida) bajarilgan kunlar soni va `d` dan keyin qolgan kunlar."""
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    w_start = max(monday, ch_start)
    w_end = min(sunday, ch_end)
    row = db.q1(
        "SELECT COUNT(*) AS c FROM challenge_logs "
        "WHERE task_id = ? AND log_date BETWEEN ? AND ? AND status = 'done'",
        (task_id, w_start.isoformat(), w_end.isoformat()),
    )
    rem_after = max(0, (w_end - d).days)
    return int(row["c"]) if row else 0, rem_after


def evaluate_day(challenge_id: int, d: date) -> dict:
    """
    `d` sanasi uchun chelenj holatini hisoblaydi.
    `day_status`: 'complete' | 'incomplete' | 'rest' | 'outside'
    O'tkazib yuborilgan kun 'incomplete' bo'ladi, lekin chelenj bekor bo'lmaydi!
    """
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
    if not ch:
        return {"day_status": "outside", "tasks": []}
    s_date = date.fromisoformat(ch["start_date"])
    e_date = date.fromisoformat(ch["end_date"])
    if d < s_date or d > e_date:
        return {"day_status": "outside", "tasks": []}

    tasks = db.q("SELECT * FROM challenge_tasks WHERE challenge_id = ? AND active = 1 ORDER BY id", (challenge_id,))
    if not tasks:
        return {"day_status": "rest", "tasks": []}

    logs = {
        r["task_id"]: r
        for r in db.q(
            "SELECT * FROM challenge_logs WHERE challenge_id = ? AND log_date = ?",
            (challenge_id, d.isoformat()),
        )
    }

    day_key = DAY_KEYS[d.weekday()]
    has_non_quota_due = any(
        t["task_type"] in ("binary", "numeric")
        or (t["task_type"] == "schedule" and day_key in parse_schedule_set(t["schedule_days"]))
        for t in tasks
    )

    task_states = []
    due_count = 0
    all_satisfied = True
    any_done_today = False

    for t in tasks:
        lg = logs.get(t["id"])
        val = float(lg["value"]) if lg else 0.0
        st = lg["status"] if lg else "pending"
        ttype = t["task_type"]
        tgt = float(t["target_value"])

        if ttype == "binary":
            due = True
            done_today = st == "done" or val >= 1.0
            satisfied = done_today
            info = "✅" if done_today else "⬜"
        elif ttype == "numeric":
            due = True
            done_today = val >= tgt
            satisfied = done_today
            u_s = f" {t['unit']}" if t.get("unit") else ""
            mark = "✅" if done_today else ("⏳" if val > 0 else "⬜")
            info = f"{mark} {_fmt_num(val)}/{_fmt_num(tgt)}{u_s}"
        elif ttype == "schedule":
            sched = parse_schedule_set(t["schedule_days"])
            due = day_key in sched
            done_today = st == "done" or val >= 1.0
            satisfied = done_today if due else True
            info = ("✅" if done_today else "⬜") if due else f"⏸ ({fmt_schedule_uz(t['schedule_days'])})"
        else:  # weekly_quota
            quota = max(1, int(round(tgt)))
            done_wk, rem_after = _weekly_done_count(t["id"], d, s_date, e_date)
            done_today = st == "done" or val >= 1.0
            due = done_today or (done_wk < quota)
            if done_today or done_wk >= quota:
                satisfied = True
            elif has_non_quota_due and rem_after >= (quota - done_wk):
                satisfied = True
            else:
                satisfied = False
            mark = "✅" if (done_today or done_wk >= quota) else "⬜"
            info = f"{mark} hafta {done_wk}/{quota}"

        if due:
            due_count += 1
        if done_today:
            any_done_today = True
        if not satisfied:
            all_satisfied = False

        task_states.append({
            "task": t,
            "value": val,
            "log_status": st,
            "due": due,
            "done_today": done_today,
            "satisfied": satisfied,
            "badge": info,
        })

    is_frozen = (
        db.q1(
            "SELECT 1 FROM challenge_freezes WHERE challenge_id = ? AND freeze_date = ?",
            (challenge_id, d.isoformat()),
        )
        is not None
    )
    if due_count == 0 and not any_done_today:
        day_status = "rest"
    elif all_satisfied and (any_done_today or all(ts["satisfied"] for ts in task_states)):
        # Agar faqat weekly_quota bo'lib, kvota avvalgi kunlarda to'lgan bo'lsa va bugun hech narsa qilinmagan bo'lsa,
        # bugungi kun 'rest' (kvota bajarilgan), bajarilgan kunda esa 'complete'!
        if not has_non_quota_due and not any_done_today:
            day_status = "rest"
        else:
            day_status = "complete"
    elif is_frozen:
        day_status = "frozen"
    else:
        day_status = "incomplete"

    return {"day_status": day_status, "tasks": task_states, "frozen": is_frozen}


def log_task(
    task_id: int,
    d: date | None = None,
    value: float | None = None,
    toggle: bool = False,
    delta: float | None = None,
) -> dict:
    """
    Vazifa bajarilishini yozadi va kunlik chelenj bonusini `scoring.py` orqali sinxronlaydi.
    Qaytaradi: {'ok': bool, 'day_status': str, 'bonus_awarded': int, 'value': float, 'status': str}
    """
    d = d or app.today()
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ? AND active = 1", (task_id,))
    if not t:
        return {"ok": False}
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (t["challenge_id"],))
    if not ch or ch["status"] == "cancelled":
        return {"ok": False}

    existing = db.q1(
        "SELECT * FROM challenge_logs WHERE task_id = ? AND log_date = ?",
        (task_id, d.isoformat()),
    )
    cur_val = float(existing["value"]) if existing else 0.0
    cur_st = existing["status"] if existing else "pending"

    if t["task_type"] == "numeric":
        tgt = float(t["target_value"])
        if delta is not None:
            new_val = max(0.0, cur_val + float(delta))
        elif value is not None:
            new_val = max(0.0, float(value))
        else:
            new_val = tgt
        if new_val >= tgt:
            new_st = "done"
        elif new_val > 0:
            new_st = "partial"
        else:
            new_st = "pending"
    else:
        if toggle:
            done_now = not (cur_st == "done" or cur_val >= 1.0)
        else:
            done_now = bool(value is None or float(value) >= 1.0)
        new_val = 1.0 if done_now else 0.0
        new_st = "done" if done_now else "pending"

    db.ex(
        "INSERT INTO challenge_logs (challenge_id, task_id, log_date, value, status) VALUES (?,?,?,?,?) "
        "ON CONFLICT(task_id, log_date) DO UPDATE SET value = excluded.value, status = excluded.status",
        (ch["id"], task_id, d.isoformat(), new_val, new_st),
    )

    ev = evaluate_day(ch["id"], d)
    awarded = scoring.set_challenge_day_bonus(
        ch["id"], d, ch["bonus_points"], completed=(ev["day_status"] == "complete")
    )
    return {
        "ok": True,
        "challenge_id": ch["id"],
        "day_status": ev["day_status"],
        "bonus_awarded": awarded,
        "value": new_val,
        "status": new_st,
    }


def challenge_progress(challenge_id: int, ref_today: date | None = None) -> dict:
    """
    30 kunlik (yoki `duration_days`) uzluksiz davomiylik ko'rsatkichi.
    O'tkazib yuborilgan kunlar chelenjni noldan boshlamaydi — faqat `incomplete_days` ga tushadi.
    """
    ref_today = ref_today or app.today()
    sync_lifecycle(ref_today)
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (challenge_id,))
    if not ch:
        return {}
    s_date = date.fromisoformat(ch["start_date"])
    e_date = date.fromisoformat(ch["end_date"])
    dur = int(ch["duration_days"])

    if ref_today < s_date:
        day_index = 0
    else:
        day_index = min(dur, (ref_today - s_date).days + 1)

    last_eval = min(ref_today, e_date)
    complete_days = 0
    incomplete_days = 0
    rest_days = 0
    frozen_days = 0
    cur = s_date
    while cur <= last_eval:
        ev = evaluate_day(challenge_id, cur)
        if ev["day_status"] == "complete":
            complete_days += 1
        elif ev["day_status"] == "frozen":
            frozen_days += 1
        elif ev["day_status"] == "rest":
            rest_days += 1
        elif cur < ref_today:
            incomplete_days += 1
        cur += timedelta(days=1)

    today_ev = (
        evaluate_day(challenge_id, ref_today)
        if s_date <= ref_today <= e_date
        else {"day_status": "outside", "tasks": [], "frozen": False}
    )
    freezes_used = int(ch.get("freezes_used") or 0)
    max_freezes = int(ch.get("max_freezes") if ch.get("max_freezes") is not None else MAX_FREEZES_DEFAULT)
    return {
        "challenge": ch,
        "day_index": day_index,
        "duration_days": dur,
        "complete_days": complete_days,
        "incomplete_days": incomplete_days,
        "rest_days": rest_days,
        "frozen_days": frozen_days,
        "freezes_used": freezes_used,
        "max_freezes": max_freezes,
        "not_started": ref_today < s_date,
        "today_eval": today_ev,
    }


# ------------------------------------------------------------------ Ekranlar (In-Place UI)

async def _edit_or_send(target_msg, text: str, kb: InlineKeyboardMarkup | None, state: FSMContext | None = None):
    markup = messenger.with_delete(kb)
    mid = target_msg.message_id if target_msg is not None else None
    if mid is None and state is not None:
        mid = (await state.get_data()).get("panel_mid")
    if mid is not None:
        try:
            await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=markup)
            db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, mid))
            if state is not None:
                await state.update_data(panel_mid=mid)
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return mid
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    if sent and state is not None:
        await state.update_data(panel_mid=sent.message_id)
    return sent.message_id if sent else None


def dashboard_view(ref_today: date | None = None) -> tuple[str, InlineKeyboardMarkup]:
    ref_today = ref_today or app.today()
    sync_lifecycle(ref_today)
    actives = db.q("SELECT * FROM challenges WHERE status = 'active' ORDER BY id")
    lines = ["🏆 <b>Chelenjlar boshqaruv paneli</b>\n"]
    rows = []

    if not actives:
        lines.append("Hozir faol chelenj yo'q. Yangi chelenj yaratish uchun pastdagi tugmani bosing.")
    else:
        for ch in actives:
            prog = challenge_progress(ch["id"], ref_today)
            t_ev = prog["today_eval"]
            if prog["not_started"]:
                lines.append(
                    f"⏳ <b>{app.esc(ch['title'])}</b> — boshlanishi: <b>{ch['start_date']}</b> "
                    f"({prog['duration_days']} kun, +{ch['bonus_points']} ball/kun)"
                )
            else:
                st_icon = (
                    "✅" if t_ev["day_status"] == "complete"
                    else ("🧊" if t_ev["day_status"] == "frozen" else ("⏸" if t_ev["day_status"] == "rest" else "🔥"))
                )
                lines.append(
                    f"{st_icon} <b>{app.esc(ch['title'])}</b> — kun <b>{prog['day_index']}/{prog['duration_days']}</b> "
                    f"(✅ {prog['complete_days']} kun, 🧊 {prog['freezes_used']}/{prog['max_freezes']}, +{ch['bonus_points']} ball/kun)"
                )
                for ts in t_ev["tasks"]:
                    t = ts["task"]
                    lines.append(f"   • {app.esc(t['title'])}: {ts['badge']}")
                    if ts["due"] or t["task_type"] == "weekly_quota":
                        if t["task_type"] == "numeric":
                            rows.append([btn(f"🔢 {t['title'][:20]} ({ts['badge']})", f"ch:num:{t['id']}:d")])
                        else:
                            mark = "✅" if ts["done_today"] else "⬜"
                            rows.append([btn(f"{mark} {t['title'][:24]}", f"ch:tg:{t['id']}:d")])
                if t_ev["day_status"] == "incomplete" and prog["freezes_used"] < prog["max_freezes"]:
                    rem_f = prog["max_freezes"] - prog["freezes_used"]
                    rows.append([btn(f"🧊 Muzlatish (Skip Day) [{rem_f}/{prog['max_freezes']}]", f"ch:frz:{ch['id']}:d")])
            rows.append([btn(f"⚙️ {ch['title'][:24]}", f"ch:open:{ch['id']}")])
            lines.append("")

    rows.append([btn("➕ Yangi chelenj", "ch:new"), btn("📂 Tarix", "ch:hist")])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(inline_keyboard=rows)


def detail_view(challenge_id: int, ref_today: date | None = None) -> tuple[str, InlineKeyboardMarkup]:
    ref_today = ref_today or app.today()
    prog = challenge_progress(challenge_id, ref_today)
    if not prog:
        return dashboard_view(ref_today)
    ch = prog["challenge"]
    st_map = {"active": "🟢 Faol", "completed": "🏁 Yakunlangan", "cancelled": "⏹ To'xtatilgan"}
    day_s = (
        f"⏳ {ch['start_date']} dan boshlanadi"
        if prog["not_started"]
        else f"<b>{prog['day_index']}/{prog['duration_days']}-kun</b>"
    )
    lines = [
        f"🏆 <b>{app.esc(ch['title'])}</b> ({st_map.get(ch['status'], ch['status'])})",
        f"📅 Muddat: {ch['start_date']} → {ch['end_date']} ({day_s})",
        f"📊 Natija: ✅ <b>{prog['complete_days']}</b> to'liq · ⚠️ <b>{prog['incomplete_days']}</b> to'liq emas"
        + (f" · 🧊 {prog['frozen_days']} muzlatilgan" if prog["frozen_days"] else "")
        + (f" · ⏸ {prog['rest_days']} dam" if prog["rest_days"] else ""),
        f"🧊 Muzlatish limiti: <b>{prog['freezes_used']}/{prog['max_freezes']}</b> ishlatilgan",
        f"⭐ Kunlik to'liq yopish bonusi: <b>+{ch['bonus_points']} ball</b>",
    ]
    rows = []
    tasks = db.q("SELECT * FROM challenge_tasks WHERE challenge_id = ? AND active = 1 ORDER BY id", (challenge_id,))
    t_ev_map = {ts["task"]["id"]: ts for ts in prog["today_eval"].get("tasks", [])}

    if tasks:
        lines.append("\n<b>Vazifalar:</b>")
        for t in tasks:
            ts = t_ev_map.get(t["id"])
            badge = ts["badge"] if ts else TYPE_LABELS.get(t["task_type"], t["task_type"])
            extra = ""
            if t["task_type"] == "schedule":
                extra = f" [{fmt_schedule_uz(t['schedule_days'])}]"
            elif t["task_type"] == "weekly_quota":
                extra = f" [haftada {int(t['target_value'])} kun]"
            elif t["task_type"] == "numeric":
                u_s = f" {t['unit']}" if t.get("unit") else ""
                extra = f" [maqsad: {_fmt_num(t['target_value'])}{u_s}]"
            lines.append(f"• {app.esc(t['title'])}{app.esc(extra)} — {badge}")

            if ch["status"] == "active" and not prog["not_started"] and ts and (ts["due"] or t["task_type"] == "weekly_quota"):
                if t["task_type"] == "numeric":
                    rows.append([btn(f"🔢 {t['title'][:22]} ({badge})", f"ch:num:{t['id']}:o")])
                else:
                    mark = "✅" if ts["done_today"] else "⬜"
                    rows.append([btn(f"{mark} {t['title'][:26]}", f"ch:tg:{t['id']}:o")])
    else:
        lines.append("\n<i>Hali vazifalar qo'shilmagan.</i>")

    if ch["status"] == "active":
        if not prog["not_started"] and prog["today_eval"]["day_status"] == "incomplete" and prog["freezes_used"] < prog["max_freezes"]:
            rem_f = prog["max_freezes"] - prog["freezes_used"]
            rows.append([btn(f"🧊 Muzlatish (Skip Day) [{rem_f}/{prog['max_freezes']}]", f"ch:frz:{ch['id']}:o")])
        rows.append([btn("➕ Vazifa qo'shish", f"ch:addt:{ch['id']}"), btn("✏️ Tahrirlash", f"ch:ed:{ch['id']}")])
        rows.append([btn("⏹ To'xtatish", f"ch:stop:{ch['id']}"), btn("⬅️ Chelenjlar", "ch:home")])
    else:
        rows.append([btn("⬅️ Chelenjlar", "ch:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ 21:45 Kechki xulosa integratsiyasi

def evening_challenges_section_and_kb(ref_today: date | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    21:45 kechki xulosa uchun faol va boshlangan (`start_date <= today`) chelenjlar holati.
    «Ertadan» deb ochilgan chelenjlar bugungi kechki xulosaga kirmaydi va uzilish yozilmaydi!
    """
    ref_today = ref_today or app.today()
    sync_lifecycle(ref_today)
    actives = db.q(
        "SELECT * FROM challenges WHERE status = 'active' AND start_date <= ? ORDER BY id",
        (ref_today.isoformat(),),
    )
    if not actives:
        return "", None
    lines = ["\n\n🏆 <b>Chelenjlar holati:</b>"]
    rows = []
    for ch in actives:
        prog = challenge_progress(ch["id"], ref_today)
        t_ev = prog["today_eval"]
        icon = (
            "✅" if t_ev["day_status"] == "complete"
            else ("🧊" if t_ev["day_status"] == "frozen" else ("⏸" if t_ev["day_status"] == "rest" else "⏳"))
        )
        lines.append(f"{icon} <b>{app.esc(ch['title'])}</b> ({prog['day_index']}/{prog['duration_days']}-kun)")
        for ts in t_ev["tasks"]:
            t = ts["task"]
            lines.append(f"  • {app.esc(t['title'])}: {ts['badge']}")
            if not ts["done_today"] and ts["due"] and t_ev["day_status"] != "frozen":
                if t["task_type"] == "numeric":
                    rows.append([btn(f"🔢 {t['title'][:22]}", f"ch:num:{t['id']}:d")])
                else:
                    rows.append([btn(f"⬜ {t['title'][:24]}", f"ch:tg:{t['id']}:d")])
        if t_ev["day_status"] == "incomplete" and prog["freezes_used"] < prog["max_freezes"]:
            rem_f = prog["max_freezes"] - prog["freezes_used"]
            rows.append([btn(f"🧊 Muzlatish ({ch['title'][:14]}) [{rem_f}/{prog['max_freezes']}]", f"ch:frz:{ch['id']}:d")])
    rows.append([btn("🏆 Chelenjlar paneli", "ch:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# ------------------------------------------------------------------ Kirish nuqtalari va Check-in tugmalari

async def entry(message: Message):
    text, kb = dashboard_view()
    await messenger.send(text, "info", kb)


@router.message(Command("chelenjlar"))
@router.message(Command("chelenj"))
async def cmd_challenges(message: Message, state: FSMContext):
    await state.clear()
    await entry(message)


@router.callback_query(F.data == "add:chl")
async def cb_from_add_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _start_new_challenge(cb.message, state)


@router.callback_query(F.data == "ch:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = dashboard_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ch:open:"))
async def cb_open(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    text, kb = detail_view(cid)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "ch:hist")
async def cb_history(cb: CallbackQuery):
    await cb.answer()
    sync_lifecycle()
    rows_db = db.q("SELECT * FROM challenges WHERE status != 'active' ORDER BY id DESC LIMIT 15")
    if not rows_db:
        text = "📂 <b>Chelenjlar tarixi</b>\n\nYakunlangan yoki to'xtatilgan chelenjlar yo'q."
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "ch:home")]])
        await _edit_or_send(cb.message, text, kb)
        return
    lines = ["📂 <b>Chelenjlar tarixi:</b>\n"]
    kb_rows = []
    for ch in rows_db:
        prog = challenge_progress(ch["id"])
        icon = "🏁" if ch["status"] == "completed" else "⏹"
        lines.append(
            f"{icon} <b>{app.esc(ch['title'])}</b> — {prog['complete_days']}/{ch['duration_days']} kun to'liq"
        )
        kb_rows.append([btn(f"{icon} {ch['title'][:24]}", f"ch:open:{ch['id']}")])
    kb_rows.append([btn("⬅️ Orqaga", "ch:home")])
    await _edit_or_send(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("ch:frz:"))
async def cb_freeze_day(cb: CallbackQuery):
    _, _, cid_s, origin = cb.data.split(":")
    cid = int(cid_s)
    res = freeze_day(cid, app.today())
    if not res.get("ok"):
        await cb.answer(res.get("error", "Xatolik"), show_alert=True)
        return
    await cb.answer(f"🧊 Bugun muzlatildi! Qoldi: {res['remaining']}/{res['max_freezes']}")
    if db.get_setting(f"journal_{app.today().isoformat()}"):
        await journal.sync(app.today())
    text, kb = detail_view(cid) if origin == "o" else dashboard_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ch:tg:"))
async def cb_toggle_task(cb: CallbackQuery):
    _, _, tid_s, origin = cb.data.split(":")
    res = log_task(int(tid_s), app.today(), toggle=True)
    if not res.get("ok"):
        await cb.answer("Topilmadi")
        return
    if res["bonus_awarded"] > 0:
        await cb.answer(f"🎉 Kun to'liq yopildi! +{res['bonus_awarded']} bonus ball!")
    else:
        await cb.answer("Saqlandi")
    if db.get_setting(f"journal_{app.today().isoformat()}"):
        await journal.sync(app.today())
    text, kb = detail_view(res["challenge_id"]) if origin == "o" else dashboard_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ch:num:"))
async def cb_numeric_prompt(cb: CallbackQuery, state: FSMContext):
    _, _, tid_s, origin = cb.data.split(":")
    tid = int(tid_s)
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ? AND active = 1", (tid,))
    if not t:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    lg = db.q1("SELECT value FROM challenge_logs WHERE task_id = ? AND log_date = ?", (tid, app.today().isoformat()))
    cur = float(lg["value"]) if lg else 0.0
    tgt = float(t["target_value"])
    u_s = f" {t['unit']}" if t.get("unit") else ""

    await state.set_state(ChFSM.numeric_input)
    await state.update_data(task_id=tid, origin=origin, panel_mid=cb.message.message_id)

    back_cb = f"ch:open:{t['challenge_id']}" if origin == "o" else "ch:home"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("+1", f"ch:nq:{tid}:+1:{origin}"), btn("+5", f"ch:nq:{tid}:+5:{origin}"),
         btn("+10", f"ch:nq:{tid}:+10:{origin}")],
        [btn(f"✅ Maqsad ({_fmt_num(tgt)})", f"ch:nq:{tid}:full:{origin}"),
         btn("🔄 0 qilish", f"ch:nq:{tid}:zero:{origin}")],
        [btn("⬅️ Orqaga", back_cb)],
    ])
    text = (
        f"🔢 <b>{app.esc(t['title'])}</b>\n"
        f"Bugungi natija: <b>{_fmt_num(cur)} / {_fmt_num(tgt)}{app.esc(u_s)}</b>\n\n"
        "Tugmani bosing yoki son yozib yuboring (masalan: <code>15</code> jami qiymat, <code>+5</code> qo'shish):"
    )
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("ch:nq:"))
async def cb_numeric_quick(cb: CallbackQuery, state: FSMContext):
    _, _, tid_s, act, origin = cb.data.split(":")
    tid = int(tid_s)
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (tid,))
    if not t:
        await cb.answer("Topilmadi")
        return
    if act == "full":
        res = log_task(tid, app.today(), value=float(t["target_value"]))
    elif act == "zero":
        res = log_task(tid, app.today(), value=0.0)
    else:
        res = log_task(tid, app.today(), delta=float(act))
    await state.clear()
    if res.get("bonus_awarded", 0) > 0:
        await cb.answer(f"🎉 Kun yopildi! +{res['bonus_awarded']} ball!")
    else:
        await cb.answer("Yangilandi")
    if db.get_setting(f"journal_{app.today().isoformat()}"):
        await journal.sync(app.today())
    text, kb = detail_view(t["challenge_id"]) if origin == "o" else dashboard_view()
    await _edit_or_send(cb.message, text, kb)


@router.message(ChFSM.numeric_input)
async def msg_numeric_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    tid = data["task_id"]
    origin = data.get("origin", "d")
    try:
        if raw.startswith("+"):
            res = log_task(tid, app.today(), delta=float(raw[1:]))
        else:
            res = log_task(tid, app.today(), value=float(raw.lstrip("=")))
    except ValueError:
        return
    await state.clear()
    if db.get_setting(f"journal_{app.today().isoformat()}"):
        await journal.sync(app.today())
    text, kb = detail_view(res["challenge_id"]) if origin == "o" else dashboard_view()
    await _edit_or_send(None, text, kb, state=None) if not data.get("panel_mid") else await _edit_by_mid(data["panel_mid"], text, kb)


async def _edit_by_mid(mid: int, text: str, kb: InlineKeyboardMarkup):
    try:
        await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=messenger.with_delete(kb))
        db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, mid))
    except Exception:
        await messenger.send(text, "info", kb, bind_trigger=False)


# ------------------------------------------------------------------ Dinamik CRUD (FSM): Yangi Chelenj & Vazifalar

async def _start_new_challenge(target_msg, state: FSMContext):
    await state.clear()
    await state.set_state(ChFSM.ch_title)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "ch:home")]])
    await _edit_or_send(
        target_msg,
        "🏆 <b>Yangi chelenj yaratish</b>\n\nChelenj nomini yozing:",
        kb,
        state,
    )


@router.callback_query(F.data == "ch:new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _start_new_challenge(cb.message, state)


@router.message(ChFSM.ch_title)
async def msg_ch_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not title:
        return
    await state.update_data(ch_title=title)
    await state.set_state(ChFSM.ch_duration)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("7 kun", "ch:dur:7"), btn("14 kun", "ch:dur:14"), btn("21 kun", "ch:dur:21")],
        [btn("30 kun", "ch:dur:30"), btn("60 kun", "ch:dur:60"), btn("90 kun", "ch:dur:90")],
        [btn("⬅️ Bekor", "ch:home")],
    ])
    data = await state.get_data()
    await _edit_by_mid(
        data["panel_mid"],
        f"🏆 <b>{app.esc(title)}</b>\n\nNecha kun davom etadi? Tugmadan tanlang yoki kun sonini yozing:",
        kb,
    )


async def _ask_start_date(target_msg, state: FSMContext, dur: int):
    await state.update_data(ch_dur=dur)
    await state.set_state(ChFSM.ch_start)
    data = await state.get_data()
    tom_s = (app.today() + timedelta(days=1)).strftime("%d.%m")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🟢 Bugundan", "ch:st:today"), btn(f"⏳ Ertadan ({tom_s})", "ch:st:tomorrow")],
        [btn("⬅️ Bekor", "ch:home")],
    ])
    text = f"🏆 <b>{app.esc(data['ch_title'])}</b> ({dur} kun)\n\nChelenj qachondan boshlansin?"
    if target_msg is not None:
        await _edit_or_send(target_msg, text, kb, state)
    else:
        await _edit_by_mid(data["panel_mid"], text, kb)


@router.callback_query(ChFSM.ch_duration, F.data.startswith("ch:dur:"))
async def cb_ch_dur(cb: CallbackQuery, state: FSMContext):
    dur = int(cb.data.split(":")[2])
    await cb.answer()
    await _ask_start_date(cb.message, state, dur)


@router.message(ChFSM.ch_duration)
async def msg_ch_dur(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not raw.isdigit() or int(raw) <= 0:
        return
    await _ask_start_date(None, state, int(raw))


@router.callback_query(ChFSM.ch_start, F.data.startswith("ch:st:"))
async def cb_ch_start(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":")[2]
    await cb.answer()
    data = await state.get_data()
    s_date = app.today() + timedelta(days=1) if choice == "tomorrow" else app.today()
    cid = create_challenge(data["ch_title"], int(data["ch_dur"]), start_date=s_date)
    await _prompt_task_title(cb.message, state, cid)


async def _prompt_task_title(target_msg, state: FSMContext, cid: int):
    await state.set_state(ChFSM.task_title)
    await state.update_data(challenge_id=cid)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", f"ch:open:{cid}")]])
    await _edit_or_send(
        target_msg,
        "➕ <b>Vazifa qo'shish</b>\n\nVazifa nomini yozing:",
        kb,
        state,
    )


@router.callback_query(F.data.startswith("ch:addt:"))
async def cb_add_task(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await _prompt_task_title(cb.message, state, cid)


@router.message(ChFSM.task_title)
async def msg_task_title(message: Message, state: FSMContext):
    t_title = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not t_title:
        return
    await state.update_data(t_title=t_title)
    await state.set_state(ChFSM.task_type)
    data = await state.get_data()
    cid = data["challenge_id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("☑️ Ha / Yo'q (binary)", "ch:tt:binary")],
        [btn("🔢 Sonli maqsad (numeric)", "ch:tt:numeric")],
        [btn("📅 Haftalik kvota (weekly_quota)", "ch:tt:weekly_quota")],
        [btn("🗓 Kunlar jadvali (schedule)", "ch:tt:schedule")],
        [btn("⬅️ Bekor", f"ch:open:{cid}")],
    ])
    await _edit_by_mid(
        data["panel_mid"],
        f"📌 Vazifa: <b>{app.esc(t_title)}</b>\n\nVazifa turini tanlang:",
        kb,
    )


@router.callback_query(ChFSM.task_type, F.data.startswith("ch:tt:"))
async def cb_task_type(cb: CallbackQuery, state: FSMContext):
    ttype = cb.data.split(":")[2]
    await cb.answer()
    data = await state.get_data()
    cid = data["challenge_id"]
    await state.update_data(t_type=ttype)

    if ttype == "binary":
        add_task(cid, data["t_title"], "binary", 1.0)
        await _after_task_added(cb.message, state, cid)
        return

    if ttype == "numeric":
        await state.set_state(ChFSM.task_target)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"ch:open:{cid}")]])
        await _edit_or_send(
            cb.message,
            f"🔢 <b>{app.esc(data['t_title'])}</b>\n\nKunlik maqsad sonini (va ixtiyoriy birligini) yozing "
            "(masalan: <code>20 bet</code> yoki <code>5 km</code> yoki <code>30</code>):",
            kb,
            state,
        )
        return

    if ttype == "weekly_quota":
        await state.set_state(ChFSM.task_target)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("2 kun", "ch:wq:2"), btn("3 kun", "ch:wq:3"), btn("4 kun", "ch:wq:4"), btn("5 kun", "ch:wq:5")],
            [btn("⬅️ Bekor", f"ch:open:{cid}")],
        ])
        await _edit_or_send(
            cb.message,
            f"📅 <b>{app.esc(data['t_title'])}</b>\n\nHaftasiga necha kun bajarilishi kerak (1..7)?",
            kb,
            state,
        )
        return

    # schedule
    await state.set_state(ChFSM.task_schedule)
    await state.update_data(sched_days=["mon", "wed", "fri"])
    await _render_schedule_picker(cb.message, state)


@router.callback_query(ChFSM.task_target, F.data.startswith("ch:wq:"))
async def cb_wq_pick(cb: CallbackQuery, state: FSMContext):
    q = int(cb.data.split(":")[2])
    await cb.answer()
    data = await state.get_data()
    cid = data["challenge_id"]
    add_task(cid, data["t_title"], "weekly_quota", float(q))
    await _after_task_added(cb.message, state, cid)


@router.message(ChFSM.task_target)
async def msg_task_target(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    cid = data["challenge_id"]
    ttype = data["t_type"]
    parts = raw.split(maxsplit=1)
    try:
        val = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        return
    if val <= 0:
        return
    unit = parts[1].strip() if len(parts) > 1 and ttype == "numeric" else ""
    add_task(cid, data["t_title"], ttype, val, unit=unit)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Yana vazifa qo'shish", f"ch:addt:{cid}")],
        [btn("✅ Tayyor (chelenjni ochish)", f"ch:open:{cid}")],
    ])
    await _edit_by_mid(data["panel_mid"], "✅ Vazifa qo'shildi! Yana vazifa qo'shasizmi?", kb)


async def _render_schedule_picker(target_msg, state: FSMContext):
    data = await state.get_data()
    cid = data["challenge_id"]
    sel = data.get("sched_days", [])
    r1, r2 = [], []
    for i, k in enumerate(DAY_KEYS):
        mark = "☑️" if k in sel else "⬜"
        b = btn(f"{mark} {DAY_UZ[k]}", f"ch:sd:{k}")
        (r1 if i < 4 else r2).append(b)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        r1, r2,
        [btn("✅ Saqlash", "ch:sdok"), btn("⬅️ Bekor", f"ch:open:{cid}")],
    ])
    await _edit_or_send(
        target_msg,
        f"🗓 <b>{app.esc(data['t_title'])}</b>\n\nHaftaning qaysi kunlarida bajariladi? "
        "(yoki <code>mon,wed,fri</code> deb yozing):",
        kb,
        state,
    )


@router.callback_query(ChFSM.task_schedule, F.data.startswith("ch:sd:"))
async def cb_toggle_sched_day(cb: CallbackQuery, state: FSMContext):
    k = cb.data.split(":")[2]
    data = await state.get_data()
    sel = list(data.get("sched_days", []))
    if k in sel:
        sel.remove(k)
    else:
        sel.append(k)
    await cb.answer()
    await state.update_data(sched_days=sel)
    await _render_schedule_picker(cb.message, state)


@router.callback_query(ChFSM.task_schedule, F.data == "ch:sdok")
async def cb_sched_ok(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = data.get("sched_days", [])
    if not sel:
        await cb.answer("Kamida 1 kunni tanlang", show_alert=True)
        return
    await cb.answer()
    cid = data["challenge_id"]
    add_task(cid, data["t_title"], "schedule", 1.0, schedule_days= normalize_schedule_days(sel))
    await _after_task_added(cb.message, state, cid)


@router.message(ChFSM.task_schedule)
async def msg_sched_days(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    sched = normalize_schedule_days(raw)
    if not sched:
        return
    data = await state.get_data()
    cid = data["challenge_id"]
    add_task(cid, data["t_title"], "schedule", 1.0, schedule_days=sched)
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Yana vazifa qo'shish", f"ch:addt:{cid}")],
        [btn("✅ Tayyor (chelenjni ochish)", f"ch:open:{cid}")],
    ])
    await _edit_by_mid(data["panel_mid"], "✅ Vazifa qo'shildi! Yana vazifa qo'shasizmi?", kb)


async def _after_task_added(target_msg, state: FSMContext, cid: int):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➕ Yana vazifa qo'shish", f"ch:addt:{cid}")],
        [btn("✅ Tayyor (chelenjni ochish)", f"ch:open:{cid}")],
    ])
    await _edit_or_send(target_msg, "✅ Vazifa saqlandi! Yana vazifa qo'shasizmi?", kb)


# ------------------------------------------------------------------ Tahrirlash va To'xtatish

@router.callback_query(F.data.startswith("ch:ed:"))
async def cb_edit_menu(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (cid,))
    if not ch:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.clear()
    tasks = db.q("SELECT * FROM challenge_tasks WHERE challenge_id = ? AND active = 1 ORDER BY id", (cid,))
    rows = [
        [btn("✏️ Nomni o'zgartirish", f"ch:ecn:{cid}"), btn("📅 Muddatni o'zgartirish", f"ch:ecd:{cid}")],
        [btn(f"⭐ Bonus ball ({ch['bonus_points']})", f"ch:ecb:{cid}")],
    ]
    for t in tasks:
        rows.append([
            btn(f"✏️ {t['title'][:18]}", f"ch:etn:{t['id']}"),
            btn(f"🗑 O'chirish", f"ch:dt:{t['id']}"),
        ])
    rows.append([btn("⬅️ Orqaga", f"ch:open:{cid}")])
    await _edit_or_send(
        cb.message,
        f"⚙️ <b>Tahrirlash: {app.esc(ch['title'])}</b>\nKerakli amalni tanlang:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ch:ecn:"))
async def cb_edit_ch_name(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.set_state(ChFSM.edit_ch_title)
    await state.update_data(challenge_id=cid, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        "✏️ Chelenjning yangi nomini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"ch:ed:{cid}")]]),
        state,
    )


@router.message(ChFSM.edit_ch_title)
async def msg_edit_ch_name(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    cid = data["challenge_id"]
    if val:
        update_challenge(cid, title=val)
    await state.clear()
    text, kb = detail_view(cid)
    await _edit_by_mid(data["panel_mid"], text, kb)


@router.callback_query(F.data.startswith("ch:ecd:"))
async def cb_edit_ch_dur(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.set_state(ChFSM.edit_ch_dur)
    await state.update_data(challenge_id=cid, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        "📅 Yangi davomiylikni (kunlarda) yozing (masalan: <code>30</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"ch:ed:{cid}")]]),
        state,
    )


@router.message(ChFSM.edit_ch_dur)
async def msg_edit_ch_dur(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    cid = data["challenge_id"]
    if raw.isdigit() and int(raw) > 0:
        update_challenge(cid, duration_days=int(raw))
    await state.clear()
    text, kb = detail_view(cid)
    await _edit_by_mid(data["panel_mid"], text, kb)


@router.callback_query(F.data.startswith("ch:ecb:"))
async def cb_edit_ch_bonus(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.set_state(ChFSM.edit_ch_bonus)
    await state.update_data(challenge_id=cid, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        "⭐ Kunlik to'liq yopilganda beriladigan bonus ballni yozing (masalan: <code>10</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"ch:ed:{cid}")]]),
        state,
    )


@router.message(ChFSM.edit_ch_bonus)
async def msg_edit_ch_bonus(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    cid = data["challenge_id"]
    if raw.isdigit() and int(raw) > 0:
        update_challenge(cid, bonus_points=int(raw))
    await state.clear()
    text, kb = detail_view(cid)
    await _edit_by_mid(data["panel_mid"], text, kb)


@router.callback_query(F.data.startswith("ch:etn:"))
async def cb_edit_task_name(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (tid,))
    if not t:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.set_state(ChFSM.edit_task_title)
    await state.update_data(task_id=tid, challenge_id=t["challenge_id"], panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        f"✏️ <b>{app.esc(t['title'])}</b> uchun yangi nomni yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"ch:ed:{t['challenge_id']}")]]),
        state,
    )


@router.message(ChFSM.edit_task_title)
async def msg_edit_task_name(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    if val:
        update_task(data["task_id"], title=val)
    await state.clear()
    text, kb = detail_view(data["challenge_id"])
    await _edit_by_mid(data["panel_mid"], text, kb)


@router.callback_query(F.data.startswith("ch:dt:"))
async def cb_del_task(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    t = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (tid,))
    if not t:
        await cb.answer("Topilmadi")
        return
    delete_task(tid)
    await cb.answer("Vazifa o'chirildi")
    text, kb = detail_view(t["challenge_id"])
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ch:stop:"))
async def cb_stop_confirm(cb: CallbackQuery):
    cid = int(cb.data.split(":")[2])
    ch = db.q1("SELECT * FROM challenges WHERE id = ?", (cid,))
    if not ch:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⏹ Ha, to'xtatish", f"ch:stopok:{cid}"), btn("⬅️ Yo'q, qaytish", f"ch:open:{cid}")],
    ])
    await _edit_or_send(
        cb.message,
        f"⚠️ <b>{app.esc(ch['title'])}</b> chelenjini muddatidan oldin to'xtatamizmi?",
        kb,
    )


@router.callback_query(F.data.startswith("ch:stopok:"))
async def cb_stop_ok(cb: CallbackQuery):
    cid = int(cb.data.split(":")[2])
    cancel_challenge(cid)
    await cb.answer("Chelenj to'xtatildi")
    text, kb = dashboard_view()
    await _edit_or_send(cb.message, text, kb)
