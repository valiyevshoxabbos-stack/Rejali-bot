"""
AltronBek 3.8.5 — "Universitet va Akademik Ishlar Moduli" (feat_uni.py)

Quyidagi imkoniyatlarni taqdim etadi:
1. Dinamik Universitet Fanlari (CRUD) — fan nomi, o'qituvchi, xona
2. Universitet Dars Jadvali:
   - Dushanbadan Shanbagacha
   - 1-4 paralar (13:40–15:00, 15:10–16:30, 16:40–18:00, 18:10–19:30)
   - Hafta toqligi/juftligi (odd/even/all)
   - Dars turlari (Ma'ruza, Amaliy, Seminar, Laboratoriya)
3. Uy Vazifalari (Homeworks):
   - Fan va dars turiga bog'lash
   - Smart "[⏭ Keyingi darsgacha]" tugmasi orqali muddatni avtomatik topish
   - Vazifalar holatini belgilash (bajarildi/bajarilmadi)
4. Akademik Ishlar (BMI, Kurs ishi, Mustaqil ish, Custom):
   - Bosqichma-bosqich shablonlar (BMI: 7 bosqich, Kurs ishi: 4, Mustaqil ish: 3)
   - Progress bar ([████░░░░░░] 40%)
   - BMI bosqichi bajarilganda Chelenj moduli bilan avtomatik sinxronizatsiya
5. Aqlli Eslatmalar:
   - 12:45 Sumkani yig'ish eslatmasi
   - 13:15 Uydan chiqish eslatmasi
   - Har bir para boshlanishidan 10 daqiqa oldingi eslatmalar
   - 06:30 tonggi xulosada universitet bloki
"""

from datetime import date, datetime, timedelta
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import messenger

router = Router()

# Universitet standart qo'ng'iroqlar jadvali (para -> (boshlanish, tugash))
UNI_BELLS: dict[int, tuple[str, str]] = {
    1: ("13:40", "15:00"),
    2: ("15:10", "16:30"),
    3: ("16:40", "18:00"),
    4: ("18:10", "19:30"),
}

BAG_REMINDER_TIME = "12:45"
LEAVE_REMINDER_TIME = "13:15"
PRE_PARA_MINUTES = 10

WEEKDAY_NAMES_UZ = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
WEEKDAY_SHORT_UZ = ["Du", "Se", "Chor", "Pay", "Ju", "Sha", "Yak"]

LESSON_TYPES: dict[str, str] = {
    "lecture": "🎙 Ma'ruza",
    "practice": "✍️ Amaliy",
    "seminar": "🗣 Seminar",
    "lab": "🔬 Laboratoriya",
}

PARITY_LABELS: dict[str, str] = {
    "all": "Har hafta",
    "odd": "Toq hafta",
    "even": "Juft hafta",
}

WORK_TYPES: dict[str, str] = {
    "bmi": "🎓 Bitiruv Malakaviy Ishi (BMI)",
    "course_work": "📑 Kurs ishi",
    "independent": "📝 Mustaqil ish",
    "custom": "📌 Maxsus akademik ish",
}

WORK_TEMPLATES: dict[str, list[str]] = {
    "bmi": [
        "Mavzu va ilmiy rahbar tasdiqlash",
        "Adabiyotlar tahlili va reja tuzish",
        "1-bob: Nazariy qismni yozish",
        "2-bob: Tahliliy / Amaliy qismni ishlab chiqish",
        "3-bob: Natijalar, takliflar va xulosa",
        "Taqriz, xulosa va antiplagiat tekshiruvi",
        "Taqdimot (slayd) tayyorlash va himoya",
    ],
    "course_work": [
        "Reja va adabiyotlar ro'yxatini tuzish",
        "Nazariy qismni yozish",
        "Amaliy/hisob-kitob qismini bajarish",
        "Xulosa va himoyaga topshirish",
    ],
    "independent": [
        "Ma'lumot to'plash va konspekt qilish",
        "Asosiy mazmunni yozish / taqdimot",
        "Topshirish va baholanish",
    ],
}


class UniFSM(StatesGroup):
    add_subj_name = State()
    add_subj_teacher = State()
    add_subj_room = State()
    edit_subj_field = State()

    add_hw_task = State()
    add_hw_deadline = State()

    add_work_title = State()
    add_work_subject = State()
    add_work_deadline = State()
    add_work_step = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _edit_or_send(target_msg, text: str, kb: InlineKeyboardMarkup | None):
    markup = messenger.with_delete(kb)
    is_bot = getattr(getattr(target_msg, "from_user", None), "is_bot", False)
    mid = getattr(target_msg, "message_id", None)
    if is_bot and mid is not None:
        try:
            await app.bot.edit_message_text(
                text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=markup
            )
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return mid
        except Exception:
            pass
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    return sent.message_id if sent else None


# ------------------------------------------------------------------ Hafta va Sana hisob-kitoblari

def current_week_parity(d: date) -> str:
    """d sanasidagi haftaning toq yoki juftligini aniqlaydi: 'odd' yoki 'even'."""
    return "odd" if d.isocalendar().week % 2 == 1 else "even"


def parity_display_name(parity: str) -> str:
    return PARITY_LABELS.get(parity, parity)


def progress_bar(done: int, total: int) -> str:
    if total <= 0:
        return "[──────────] 0% (0/0)"
    pct = int(round(done / total * 100))
    filled = min(10, max(0, int(round(pct / 10))))
    bar = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {pct}% ({done}/{total})"


# ------------------------------------------------------------------ DB: Fanlar (Subjects)

def add_subject(name: str, teacher: str = "", room: str = "") -> int:
    return db.ins(
        "INSERT INTO uni_subjects (name, teacher, room, is_active) VALUES (?, ?, ?, 1)",
        (name.strip(), teacher.strip(), room.strip()),
    )


def get_subjects(only_active: bool = True) -> list[dict]:
    if only_active:
        return db.q("SELECT * FROM uni_subjects WHERE is_active = 1 ORDER BY name ASC")
    return db.q("SELECT * FROM uni_subjects ORDER BY name ASC")


def get_subject(subject_id: int) -> dict | None:
    return db.q1("SELECT * FROM uni_subjects WHERE id = ?", (subject_id,))


def update_subject(subject_id: int, name: str, teacher: str = "", room: str = ""):
    db.ex(
        "UPDATE uni_subjects SET name = ?, teacher = ?, room = ? WHERE id = ?",
        (name.strip(), teacher.strip(), room.strip(), subject_id),
    )


def delete_subject(subject_id: int):
    db.ex("DELETE FROM uni_subjects WHERE id = ?", (subject_id,))


# ------------------------------------------------------------------ DB: Dars Jadvali (Timetable)

def set_timetable_slot(
    weekday: int,
    para_number: int,
    subject_id: int | None,
    lesson_type: str = "lecture",
    week_parity: str = "all",
    custom_room: str = "",
) -> int | None:
    if subject_id is None:
        db.ex(
            "DELETE FROM uni_timetable WHERE weekday = ? AND para_number = ? AND week_parity = ?",
            (weekday, para_number, week_parity),
        )
        return None
    return db.ins(
        "INSERT INTO uni_timetable (weekday, para_number, subject_id, lesson_type, week_parity, custom_room) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(weekday, para_number, week_parity) DO UPDATE SET "
        "subject_id = excluded.subject_id, lesson_type = excluded.lesson_type, custom_room = excluded.custom_room",
        (weekday, para_number, subject_id, lesson_type, week_parity, custom_room.strip()),
    )


def delete_timetable_slot(weekday: int, para_number: int, week_parity: str = "all"):
    db.ex(
        "DELETE FROM uni_timetable WHERE weekday = ? AND para_number = ? AND week_parity = ?",
        (weekday, para_number, week_parity),
    )


def get_timetable_for_day(weekday: int, parity: str | None = None) -> list[dict]:
    if parity:
        return db.q(
            "SELECT t.*, s.name AS subject_name, s.teacher, s.room AS default_room "
            "FROM uni_timetable t "
            "JOIN uni_subjects s ON t.subject_id = s.id "
            "WHERE t.weekday = ? AND t.week_parity IN ('all', ?) "
            "ORDER BY t.para_number ASC",
            (weekday, parity),
        )
    return db.q(
        "SELECT t.*, s.name AS subject_name, s.teacher, s.room AS default_room "
        "FROM uni_timetable t "
        "JOIN uni_subjects s ON t.subject_id = s.id "
        "WHERE t.weekday = ? "
        "ORDER BY t.para_number ASC, t.week_parity ASC",
        (weekday,),
    )


def get_today_lessons(d: date | None = None) -> list[dict]:
    d = d or app.today()
    weekday = d.weekday()
    parity = current_week_parity(d)
    return get_timetable_for_day(weekday, parity)


# ------------------------------------------------------------------ DB: Uy Vazifalari (Homeworks)

def add_homework(
    subject_id: int,
    target_lesson_type: str | None,
    task_text: str,
    due_date: str | None = None,
) -> int:
    return db.ins(
        "INSERT INTO uni_homeworks (subject_id, target_lesson_type, task_text, due_date, is_done) "
        "VALUES (?, ?, ?, ?, 0)",
        (subject_id, target_lesson_type, task_text.strip(), due_date),
    )


def get_homeworks(
    only_pending: bool = False,
    subject_id: int | None = None,
    target_lesson_type: str | None = None,
) -> list[dict]:
    sql = (
        "SELECT h.*, s.name AS subject_name "
        "FROM uni_homeworks h "
        "JOIN uni_subjects s ON h.subject_id = s.id "
        "WHERE 1=1 "
    )
    params = []
    if only_pending:
        sql += "AND h.is_done = 0 "
    if subject_id is not None:
        sql += "AND h.subject_id = ? "
        params.append(subject_id)
    if target_lesson_type is not None:
        sql += "AND (h.target_lesson_type IS NULL OR h.target_lesson_type = ?) "
        params.append(target_lesson_type)
    sql += "ORDER BY h.is_done ASC, h.due_date ASC, h.id DESC"
    return db.q(sql, tuple(params))


def toggle_homework(homework_id: int) -> bool:
    h = db.q1("SELECT is_done FROM uni_homeworks WHERE id = ?", (homework_id,))
    if not h:
        return False
    new_state = 0 if h["is_done"] else 1
    db.ex("UPDATE uni_homeworks SET is_done = ? WHERE id = ?", (new_state, homework_id))
    return bool(new_state)


def delete_homework(homework_id: int):
    db.ex("DELETE FROM uni_homeworks WHERE id = ?", (homework_id,))


def find_next_lesson_date(
    subject_id: int,
    lesson_type: str | None = None,
    start_date: date | None = None,
) -> date | None:
    """
    Belgilangan fan (va dars turi) uchun keyingi dars bo'ladigan eng yaqin sanani topadi (14 kun ichida).
    Hafta kuni va toq/juftlik inobatga olinadi.
    """
    cur = (start_date or app.today()) + timedelta(days=1)
    for _ in range(14):
        wday = cur.weekday()
        if wday < 6:  # Du..Sha
            parity = current_week_parity(cur)
            sql = (
                "SELECT 1 FROM uni_timetable "
                "WHERE weekday = ? AND subject_id = ? AND week_parity IN ('all', ?) "
            )
            params = [wday, subject_id, parity]
            if lesson_type:
                sql += "AND (lesson_type = ? OR lesson_type IS NULL) "
                params.append(lesson_type)
            sql += "LIMIT 1"
            if db.q1(sql, tuple(params)):
                return cur
        cur += timedelta(days=1)
    return None


# ------------------------------------------------------------------ DB: Akademik Ishlar (Works & Steps)

def create_work(
    work_type: str,
    title: str,
    subject_name: str = "",
    deadline: str | None = None,
    custom_steps: list[str] | None = None,
) -> int:
    work_id = db.ins(
        "INSERT INTO uni_works (work_type, title, subject_name, deadline, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (work_type, title.strip(), subject_name.strip(), deadline),
    )
    steps = custom_steps if custom_steps is not None else WORK_TEMPLATES.get(work_type, [])
    for order, step_title in enumerate(steps, start=1):
        db.ins(
            "INSERT INTO uni_work_steps (work_id, step_order, step_title, is_done) VALUES (?, ?, ?, 0)",
            (work_id, order, step_title.strip()),
        )
    return work_id


def get_works(status: str = "active") -> list[dict]:
    if status == "all":
        return db.q("SELECT * FROM uni_works ORDER BY id DESC")
    return db.q("SELECT * FROM uni_works WHERE status = ? ORDER BY id DESC", (status,))


def get_work_details(work_id: int) -> tuple[dict | None, list[dict]]:
    work = db.q1("SELECT * FROM uni_works WHERE id = ?", (work_id,))
    if not work:
        return None, []
    steps = db.q("SELECT * FROM uni_work_steps WHERE work_id = ? ORDER BY step_order ASC", (work_id,))
    return work, steps


def add_work_step(work_id: int, step_title: str) -> int:
    max_order = db.q1("SELECT MAX(step_order) AS m FROM uni_work_steps WHERE work_id = ?", (work_id,))
    next_order = (max_order["m"] or 0) + 1 if max_order else 1
    return db.ins(
        "INSERT INTO uni_work_steps (work_id, step_order, step_title, is_done) VALUES (?, ?, ?, 0)",
        (work_id, next_order, step_title.strip()),
    )


def toggle_work_step(step_id: int) -> tuple[bool, int]:
    """
    Bosqichni toggle qiladi.
    Agar BMI bo'lsa va is_done=1 bo'lsa, tegishli faol chelenjlarni log qiladi.
    Qaytaradi: (yangi_holat, work_id)
    """
    s = db.q1("SELECT * FROM uni_work_steps WHERE id = ?", (step_id,))
    if not s:
        return False, 0
    work = db.q1("SELECT * FROM uni_works WHERE id = ?", (s["work_id"],))
    new_state = 0 if s["is_done"] else 1
    comp_at = app.now().isoformat() if new_state else None
    db.ex(
        "UPDATE uni_work_steps SET is_done = ?, completed_at = ? WHERE id = ?",
        (new_state, comp_at, step_id),
    )

    # Agar BMI bosqichi bajarilgan bo'lsa, chelenj sinxronizatsiyasi
    if new_state == 1 and work and work["work_type"] == "bmi":
        _sync_bmi_challenge_progress()

    # Barcha bosqichlar bitgan bo'lsa ish statusini 'completed' ga o'tkazish
    all_steps = db.q("SELECT is_done FROM uni_work_steps WHERE work_id = ?", (s["work_id"],))
    if all_steps and all(st["is_done"] for st in all_steps):
        db.ex("UPDATE uni_works SET status = 'completed' WHERE id = ?", (s["work_id"],))
    elif work and work["status"] == "completed":
        db.ex("UPDATE uni_works SET status = 'active' WHERE id = ?", (s["work_id"],))

    return bool(new_state), s["work_id"]


def _sync_bmi_challenge_progress():
    """BMI bosqichi bajarilganda faol chelenjdagi BMI/bitiruv vazifasini log qiladi."""
    try:
        import feat_challenges
        rows = db.q(
            "SELECT ct.id FROM challenge_tasks ct "
            "JOIN challenges c ON ct.challenge_id = c.id "
            "WHERE ct.active = 1 AND c.status = 'active' "
            "AND (LOWER(ct.title) LIKE '%bmi%' OR LOWER(ct.title) LIKE '%bitiruv%' "
            "     OR LOWER(c.title) LIKE '%bmi%' OR LOWER(c.title) LIKE '%bitiruv%')"
        )
        for r in rows:
            feat_challenges.log_task(r["id"], d=app.today(), value=1.0)
    except Exception as e:
        app.log.warning(f"BMI chelenjini sinxronlashda xatolik: {e}")


def delete_work(work_id: int):
    db.ex("DELETE FROM uni_works WHERE id = ?", (work_id,))


# ------------------------------------------------------------------ Scheduler va Eslatmalar

async def schedule_today_uni():
    """Bugungi universitet eslatmalarini rejalashtiradi."""
    today = app.today()
    now = app.now()

    # Avvalgi bugungi uni joblarni tozalash
    for job in app.scheduler.get_jobs():
        if job.id.startswith(("uni_pre_", "uni_bag_", "uni_leave_")):
            job.remove()

    lessons = get_today_lessons(today)
    if not lessons:
        return

    # 1) 12:45 Sumka yig'ish eslatmasi
    bag_dt = app.at(today, BAG_REMINDER_TIME)
    if now < bag_dt:
        app.schedule_job(bag_dt, send_bag_reminder, [today.isoformat()], f"uni_bag_{today}")

    # 2) 13:15 Uydan chiqish eslatmasi
    leave_dt = app.at(today, LEAVE_REMINDER_TIME)
    if now < leave_dt:
        app.schedule_job(leave_dt, send_leave_reminder, [today.isoformat()], f"uni_leave_{today}")

    # 3) Har bir para boshlanishidan 10 daqiqa oldin eslatma
    for les in lessons:
        p_num = les["para_number"]
        if p_num in UNI_BELLS:
            start_str = UNI_BELLS[p_num][0]
            para_dt = app.at(today, start_str)
            pre_dt = para_dt - timedelta(minutes=PRE_PARA_MINUTES)
            if now < pre_dt:
                app.schedule_job(
                    pre_dt,
                    send_pre_para_reminder,
                    [les["id"], today.isoformat()],
                    f"uni_pre_{les['id']}_{today}",
                )


async def send_bag_reminder(date_iso: str):
    d = date.fromisoformat(date_iso)
    lessons = get_today_lessons(d)
    if not lessons:
        return
    text = (
        "🎒 <b>Sumkani yig'ish vaqti! (12:45)</b>\n\n"
        "Bugungi darslaringiz uchun kerakli narsalarni, daftarlarni va topshiriqlarni sumkaga solishni unutmang.\n\n"
        "📋 <b>Bugungi paralar:</b>\n"
    )
    for les in lessons:
        p_num = les["para_number"]
        start_t, end_t = UNI_BELLS.get(p_num, ("??", "??"))
        l_type = LESSON_TYPES.get(les["lesson_type"], les["lesson_type"])
        room = les["custom_room"] or les["default_room"] or "Xona belgilanmagan"
        text += f"• <b>{p_num}-para</b> ({start_t}–{end_t}): {app.esc(les['subject_name'])} ({l_type}) — 📍 {app.esc(room)}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🏛 Universitet", "uni:home")]])
    await messenger.send(text, "reminder", kb, bind_trigger=False)


async def send_leave_reminder(date_iso: str):
    d = date.fromisoformat(date_iso)
    lessons = get_today_lessons(d)
    if not lessons:
        return
    text = (
        "🚶‍♂️ <b>Uydan chiqish vaqti! (13:15)</b>\n\n"
        "Universitetda 1-para soat <b>13:40</b> da boshlanadi.\n"
        "Yo'lda tirbandlik bo'lishi mumkin, vaqtida yetib boring!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🏛 Universitet", "uni:home")]])
    await messenger.send(text, "reminder", kb, bind_trigger=False)


async def send_pre_para_reminder(slot_id: int, date_iso: str):
    slot = db.q1(
        "SELECT t.*, s.name AS subject_name, s.teacher, s.room AS default_room "
        "FROM uni_timetable t JOIN uni_subjects s ON t.subject_id = s.id "
        "WHERE t.id = ?",
        (slot_id,),
    )
    if not slot:
        return
    p_num = slot["para_number"]
    start_t, end_t = UNI_BELLS.get(p_num, ("??", "??"))
    l_type = LESSON_TYPES.get(slot["lesson_type"], slot["lesson_type"])
    room = slot["custom_room"] or slot["default_room"] or "Xona belgilanmagan"
    teacher = slot["teacher"] or "O'qituvchi belgilanmagan"

    # Mavjud uy vazifalari
    hws = get_homeworks(only_pending=True, subject_id=slot["subject_id"], target_lesson_type=slot["lesson_type"])

    text = (
        f"⏰ <b>10 daqiqadan so'ng {p_num}-para boshlanadi!</b> ({start_t}–{end_t})\n\n"
        f"📚 <b>Fan:</b> {app.esc(slot['subject_name'])}\n"
        f"🏷 <b>Mashg'ulot turi:</b> {l_type}\n"
        f"👨‍🏫 <b>O'qituvchi:</b> {app.esc(teacher)}\n"
        f"📍 <b>Auditoriya:</b> {app.esc(room)}\n"
    )
    if hws:
        text += "\n📝 <b>Ushbu mashg'ulot uchun topshiriqlar:</b>\n"
        for hw in hws:
            text += f"• {app.esc(hw['task_text'])}\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📝 Vazifalarni ko'rish", f"uni:hw_subj:{slot['subject_id']}")],
            [btn("🏛 Universitet", "uni:home")],
        ]
    )
    await messenger.send(text, "reminder", kb, bind_trigger=False)


# ------------------------------------------------------------------ Tonggi Xulosa Bloki

def summary_today_section(d: date) -> str:
    """Tonggi xulosaga (feat_summary) qo'shiladigan Universitet bloki."""
    lessons = get_today_lessons(d)
    wday_name = WEEKDAY_NAMES_UZ[d.weekday()]
    parity = current_week_parity(d)
    parity_str = parity_display_name(parity)

    if not lessons:
        return ""

    lines = [f"🏛 <b>Universitet ({wday_name}, {parity_str}):</b>"]
    for les in lessons:
        p_num = les["para_number"]
        start_t, end_t = UNI_BELLS.get(p_num, ("??", "??"))
        l_type = LESSON_TYPES.get(les["lesson_type"], les["lesson_type"])
        room = les["custom_room"] or les["default_room"] or ""
        room_str = f" [📍 {app.esc(room)}]" if room else ""
        lines.append(f"  • {p_num}-para ({start_t}–{end_t}): {app.esc(les['subject_name'])} ({l_type}){room_str}")

    # Bugungi topshirilishi kerak bo'lgan vazifalar
    today_iso = d.isoformat()
    due_hws = db.q(
        "SELECT h.*, s.name AS subject_name FROM uni_homeworks h "
        "JOIN uni_subjects s ON h.subject_id = s.id "
        "WHERE h.is_done = 0 AND h.due_date = ?",
        (today_iso,),
    )
    if due_hws:
        lines.append("  ⚠️ <b>Bugun topshiriladigan vazifalar:</b>")
        for hw in due_hws:
            lines.append(f"    - {app.esc(hw['subject_name'])}: {app.esc(hw['task_text'])}")

    return "\n".join(lines)


# ------------------------------------------------------------------ IN-PLACE UI: Asosiy Universitet Ko'rinishi

async def render_uni_home(target_msg):
    today = app.today()
    wday = today.weekday()
    wday_name = WEEKDAY_NAMES_UZ[wday]
    parity = current_week_parity(today)
    parity_str = parity_display_name(parity)

    lessons = get_today_lessons(today)
    pending_hw_count = db.q1("SELECT COUNT(*) AS c FROM uni_homeworks WHERE is_done = 0")["c"]
    active_works = get_works(status="active")

    text = (
        f"🏛 <b>Universitet va Akademik Ishlar</b>\n\n"
        f"📅 <b>Bugun:</b> {today.strftime('%d.%m.%Y')} ({wday_name})\n"
        f"🔄 <b>Hafta turi:</b> {parity_str}\n\n"
    )

    if lessons:
        text += "📚 <b>Bugungi paralar:</b>\n"
        for les in lessons:
            p_num = les["para_number"]
            start_t, end_t = UNI_BELLS.get(p_num, ("??", "??"))
            l_type = LESSON_TYPES.get(les["lesson_type"], les["lesson_type"])
            room = les["custom_room"] or les["default_room"] or "Ko'rsatilmagan"
            text += f"• <b>{p_num}-para</b> ({start_t}–{end_t}): {app.esc(les['subject_name'])} ({l_type}) [📍 {app.esc(room)}]\n"
    else:
        text += "🌴 <i>Bugun darslar jadvalda yo'q yoki dam olish kuni.</i>\n"

    text += f"\n📝 <b>Bajarilmagan uy vazifalari:</b> {pending_hw_count} ta\n"
    text += f"🎓 <b>Faol akademik ishlar:</b> {len(active_works)} ta\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📅 Dars jadvali", f"uni:tt_day:{wday if wday < 6 else 0}")],
            [btn("📚 Fanlar", "uni:sub_list"), btn("📝 Vazifalar", "uni:hw_list")],
            [btn("🎓 BMI & Akademik ishlar", "uni:works_list")],
            [btn("🔙 Ta'lim bo'limi", "edu:hub")],
        ]
    )
    return await _edit_or_send(target_msg, text, kb)


# ------------------------------------------------------------------ UI: Fanlar (Subjects CRUD)

async def render_subjects_list(target_msg):
    subs = get_subjects(only_active=True)
    text = "📚 <b>Universitet Fanlari</b>\n\n"
    if not subs:
        text += "<i>Hozircha hech qanday fan kiritilmagan. Quyidagi tugma orqali yangi fan qo'shing.</i>"
    else:
        for idx, s in enumerate(subs, 1):
            teacher = s["teacher"] or "Belgilanmagan"
            room = s["room"] or "Yo'q"
            text += f"{idx}. <b>{app.esc(s['name'])}</b>\n   👨‍🏫 {app.esc(teacher)} | 📍 Xona: {app.esc(room)}\n"

    buttons = []
    for s in subs:
        buttons.append([btn(f"✏️ {s['name'][:24]}", f"uni:sub_view:{s['id']}")])
    buttons.append([btn("➕ Yangi fan qo'shish", "uni:sub_add")])
    buttons.append([btn("🔙 Universitet menyusi", "uni:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


async def render_subject_view(target_msg, subject_id: int):
    s = get_subject(subject_id)
    if not s:
        return await render_subjects_list(target_msg)

    teacher = s["teacher"] or "Belgilanmagan"
    room = s["room"] or "Belgilanmagan"
    hws = get_homeworks(only_pending=True, subject_id=subject_id)

    text = (
        f"📚 <b>Fan:</b> {app.esc(s['name'])}\n\n"
        f"👨‍🏫 <b>O'qituvchi:</b> {app.esc(teacher)}\n"
        f"📍 <b>Standart xona:</b> {app.esc(room)}\n\n"
        f"📝 <b>Kutilayotgan vazifalar:</b> {len(hws)} ta\n"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("➕ Shu fanga vazifa qo'shish", f"uni:hw_add:{subject_id}")],
            [btn("🗑 Fanni o'chirish", f"uni:sub_del:{subject_id}")],
            [btn("🔙 Fanlar ro'yxati", "uni:sub_list")],
        ]
    )
    return await _edit_or_send(target_msg, text, kb)


# ------------------------------------------------------------------ UI: Dars Jadvali (Timetable)

async def render_timetable_day(target_msg, weekday: int):
    wday_name = WEEKDAY_NAMES_UZ[weekday]
    today = app.today()
    cur_parity = current_week_parity(today)

    lessons = db.q(
        "SELECT t.*, s.name AS subject_name, s.room AS default_room "
        "FROM uni_timetable t JOIN uni_subjects s ON t.subject_id = s.id "
        "WHERE t.weekday = ? "
        "ORDER BY t.para_number ASC, t.week_parity ASC",
        (weekday,),
    )

    text = f"📅 <b>Universitet Dars Jadvali — {wday_name}</b>\n"
    text += f"<i>(Hozirgi hafta: {parity_display_name(cur_parity)})</i>\n\n"

    lessons_by_para: dict[int, list[dict]] = {1: [], 2: [], 3: [], 4: []}
    for l in lessons:
        lessons_by_para[l["para_number"]].append(l)

    buttons = []
    for p_num in (1, 2, 3, 4):
        start_t, end_t = UNI_BELLS[p_num]
        items = lessons_by_para[p_num]
        text += f"⏰ <b>{p_num}-para ({start_t}–{end_t}):</b>\n"
        if not items:
            text += "   <i>Bo'sh</i>\n"
            buttons.append([btn(f"➕ {p_num}-paraga dars qo'yish", f"uni:tt_edit:{weekday}:{p_num}:all")])
        else:
            for it in items:
                parity_badge = f" [{parity_display_name(it['week_parity'])}]" if it['week_parity'] != 'all' else ""
                l_type = LESSON_TYPES.get(it["lesson_type"], it["lesson_type"])
                room = it["custom_room"] or it["default_room"] or ""
                room_str = f" (📍 {room})" if room else ""
                text += f"   • {app.esc(it['subject_name'])} ({l_type}){parity_badge}{room_str}\n"
                buttons.append([
                    btn(f"✏️ {p_num}-para ({parity_display_name(it['week_parity'])})", f"uni:tt_edit:{weekday}:{p_num}:{it['week_parity']}"),
                    btn("🗑", f"uni:tt_del:{weekday}:{p_num}:{it['week_parity']}"),
                ])
            # Agar toq yoki juft bo'lsa, narigi haftani ham qo'yish imkoni
            has_all = any(it["week_parity"] == "all" for it in items)
            if not has_all and len(items) < 2:
                missing = "even" if items[0]["week_parity"] == "odd" else "odd"
                buttons.append([btn(f"➕ {p_num}-para ({parity_display_name(missing)})", f"uni:tt_edit:{weekday}:{p_num}:{missing}")])
        text += "\n"

    # Hafta kunlari navigatsiyasi
    nav_row = []
    for d_idx in range(6):  # Du..Sha
        label = f"•{WEEKDAY_SHORT_UZ[d_idx]}•" if d_idx == weekday else WEEKDAY_SHORT_UZ[d_idx]
        nav_row.append(btn(label, f"uni:tt_day:{d_idx}"))
    buttons.append(nav_row)
    buttons.append([btn("🔙 Universitet menyusi", "uni:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


async def render_timetable_slot_editor(target_msg, weekday: int, para_number: int, week_parity: str):
    subs = get_subjects(only_active=True)
    wday_name = WEEKDAY_NAMES_UZ[weekday]
    start_t, end_t = UNI_BELLS[para_number]

    if not subs:
        text = (
            "⚠️ <b>Avval fanlarni kiriting!</b>\n\n"
            "Dars jadvaliga dars qo'yishdan oldin «📚 Fanlar» bo'limida kamida 1 ta fan qo'shilgan bo'lishi kerak."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("➕ Fan qo'shish", "uni:sub_add")],
                [btn("🔙 Orqaga", f"uni:tt_day:{weekday}")],
            ]
        )
        return await _edit_or_send(target_msg, text, kb)

    text = (
        f"📅 <b>Dars biriktirish:</b> {wday_name}, {para_number}-para ({start_t}–{end_t})\n"
        f"🔄 <b>Hafta turi:</b> {parity_display_name(week_parity)}\n\n"
        "Fanni tanlang:"
    )

    buttons = []
    for s in subs:
        buttons.append([btn(s["name"][:26], f"uni:tt_set_sub:{weekday}:{para_number}:{week_parity}:{s['id']}")])
    buttons.append([btn("🔙 Bekor qilish", f"uni:tt_day:{weekday}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


# ------------------------------------------------------------------ UI: Uy Vazifalari (Homeworks)

async def render_homeworks_list(target_msg, subject_filter: int | None = None):
    hws = get_homeworks(only_pending=False, subject_id=subject_filter)
    pending = [h for h in hws if not h["is_done"]]
    done = [h for h in hws if h["is_done"]]

    text = "📝 <b>Universitet Uy Vazifalari</b>\n\n"

    if not hws:
        text += "<i>Hozircha hech qanday vazifa kiritilmagan.</i>\n"
    else:
        if pending:
            text += "⏳ <b>Bajarilishi kerak:</b>\n"
            for h in pending:
                l_type = LESSON_TYPES.get(h["target_lesson_type"], "") if h["target_lesson_type"] else ""
                type_badge = f" ({l_type})" if l_type else ""
                due = f" [📅 {h['due_date']}]" if h["due_date"] else ""
                text += f"• <b>{app.esc(h['subject_name'])}</b>{type_badge}: {app.esc(h['task_text'])}{due}\n"
            text += "\n"
        if done:
            text += "✅ <b>Bajarilganlar (oxirgi 5 ta):</b>\n"
            for h in done[:5]:
                text += f"• <s>{app.esc(h['subject_name'])}: {app.esc(h['task_text'])}</s>\n"
            text += "\n"

    buttons = []
    # Pending vazifalarni toggle qilish tugmalari
    for h in pending[:8]:
        mark = "⭕️"
        buttons.append([
            btn(f"{mark} {h['subject_name'][:10]}: {h['task_text'][:16]}", f"uni:hw_toggle:{h['id']}"),
            btn("🗑", f"uni:hw_del:{h['id']}"),
        ])
    for h in done[:3]:
        buttons.append([
            btn(f"✅ {h['subject_name'][:10]}: {h['task_text'][:16]}", f"uni:hw_toggle:{h['id']}"),
            btn("🗑", f"uni:hw_del:{h['id']}"),
        ])

    buttons.append([btn("➕ Yangi vazifa qo'shish", "uni:hw_add_select")])
    buttons.append([btn("🔙 Universitet menyusi", "uni:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


# ------------------------------------------------------------------ UI: BMI va Akademik Ishlar (Works)

async def render_works_list(target_msg):
    works = get_works(status="all")
    text = "🎓 <b>Universitet Akademik Ishlar Paneli</b>\n\n"

    if not works:
        text += (
            "<i>Hozircha hech qanday akademik ish (BMI, kurs ishi yoki mustaqil ish) ochilmagan.\n"
            "Quyidagi tugma orqali shablon asosida yangi ish boshlashingiz mumkin.</i>"
        )
    else:
        for idx, w in enumerate(works, 1):
            w_type_name = WORK_TYPES.get(w["work_type"], w["work_type"])
            _, steps = get_work_details(w["id"])
            done_cnt = sum(1 for st in steps if st["is_done"])
            p_bar = progress_bar(done_cnt, len(steps))
            st_badge = "✅ Yakunlangan" if w["status"] == "completed" else "⏳ Jarayonda"
            dl_str = f" | Muddat: {w['deadline']}" if w["deadline"] else ""
            text += (
                f"{idx}. <b>{app.esc(w['title'])}</b> ({w_type_name})\n"
                f"   {p_bar}\n"
                f"   Holat: {st_badge}{dl_str}\n\n"
            )

    buttons = []
    for w in works:
        buttons.append([btn(f"📂 {w['title'][:25]}", f"uni:work_view:{w['id']}")])
    buttons.append([btn("➕ Yangi akademik ish qo'shish", "uni:work_add_type")])
    buttons.append([btn("🔙 Universitet menyusi", "uni:home")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


async def render_work_detail(target_msg, work_id: int):
    work, steps = get_work_details(work_id)
    if not work:
        return await render_works_list(target_msg)

    w_type_name = WORK_TYPES.get(work["work_type"], work["work_type"])
    done_cnt = sum(1 for st in steps if st["is_done"])
    p_bar = progress_bar(done_cnt, len(steps))

    text = (
        f"🎓 <b>{app.esc(work['title'])}</b>\n"
        f"🏷 <b>Turi:</b> {w_type_name}\n"
    )
    if work["subject_name"]:
        text += f"📚 <b>Fan:</b> {app.esc(work['subject_name'])}\n"
    if work["deadline"]:
        text += f"📅 <b>Muddat:</b> {work['deadline']}\n"
    text += f"\n📊 <b>Bajarilish progressi:</b>\n{p_bar}\n\n"
    text += "📋 <b>Bosqichlar ro'yxati:</b>\n"

    buttons = []
    for st in steps:
        mark = "✅" if st["is_done"] else "⭕️"
        text += f"{st['step_order']}. {mark} {app.esc(st['step_title'])}\n"
        buttons.append([btn(f"{mark} {st['step_order']}. {st['step_title'][:28]}", f"uni:step_toggle:{st['id']}")])

    buttons.append([btn("➕ Bosqich qo'shish", f"uni:step_add:{work_id}")])
    buttons.append([btn("🗑 Ushbu ishni o'chirish", f"uni:work_del:{work_id}")])
    buttons.append([btn("🔙 Akademik ishlar", "uni:works_list")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    return await _edit_or_send(target_msg, text, kb)


# ------------------------------------------------------------------ Entry va Buyruqlar

@router.message(Command("universitet"))
@router.message(Command("uni"))
async def cmd_universitet(message: Message, state: FSMContext):
    await state.clear()
    await render_uni_home(message)


async def entry(target):
    return await render_uni_home(target)


# ------------------------------------------------------------------ Callback Handlers: Navigatsiya

@router.callback_query(F.data == "uni:home")
async def cb_uni_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await render_uni_home(call.message)


@router.callback_query(F.data == "uni:sub_list")
async def cb_uni_sub_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await render_subjects_list(call.message)


@router.callback_query(F.data.startswith("uni:sub_view:"))
async def cb_uni_sub_view(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(call.data.split(":")[2])
    await call.answer()
    await render_subject_view(call.message, sid)


@router.callback_query(F.data.startswith("uni:sub_del:"))
async def cb_uni_sub_del(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(call.data.split(":")[2])
    delete_subject(sid)
    await call.answer("Fan o'chirildi")
    await render_subjects_list(call.message)


# ------------------------------------------------------------------ Callback & FSM: Fan Qo'shish

@router.callback_query(F.data == "uni:sub_add")
async def cb_uni_sub_add(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(UniFSM.add_subj_name)
    text = (
        "📚 <b>Yangi fan qo'shish</b>\n\n"
        "Fan nomini yuboring (masalan: <i>Algoritmlar va ma'lumotlar tuzilmasi</i>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Bekor qilish", "uni:sub_list")]])
    await _edit_or_send(call.message, text, kb)


@router.message(UniFSM.add_subj_name)
async def fsm_add_subj_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        return await message.answer("Iltimos, fanning nomini matn ko'rinishida yuboring:")
    await state.update_data(name=name)
    await state.set_state(UniFSM.add_subj_teacher)
    text = (
        f"Fan: <b>{app.esc(name)}</b>\n\n"
        "O'qituvchining ism-sharifini yuboring (yoki bo'sh qoldirish uchun «-» deb yuboring):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("O'tkazib yuborish (—)", "uni:sub_teacher_skip")]])
    await message.answer(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "uni:sub_teacher_skip", UniFSM.add_subj_teacher)
async def cb_uni_sub_teacher_skip(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(teacher="")
    await state.set_state(UniFSM.add_subj_room)
    text = "Standart auditoriya/xona raqamini yuboring (yoki bo'sh qoldirish uchun «-» deb yuboring):"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("O'tkazib yuborish (—)", "uni:sub_room_skip")]])
    await _edit_or_send(call.message, text, kb)


@router.message(UniFSM.add_subj_teacher)
async def fsm_add_subj_teacher(message: Message, state: FSMContext):
    teacher = (message.text or "").strip()
    if teacher == "-":
        teacher = ""
    await state.update_data(teacher=teacher)
    await state.set_state(UniFSM.add_subj_room)
    text = "Standart auditoriya/xona raqamini yuboring (yoki bo'sh qoldirish uchun «-» deb yuboring):"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("O'tkazib yuborish (—)", "uni:sub_room_skip")]])
    await message.answer(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "uni:sub_room_skip", UniFSM.add_subj_room)
async def cb_uni_sub_room_skip(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    await state.clear()
    sid = add_subject(data["name"], data.get("teacher", ""), "")
    await render_subject_view(call.message, sid)


@router.message(UniFSM.add_subj_room)
async def fsm_add_subj_room(message: Message, state: FSMContext):
    room = (message.text or "").strip()
    if room == "-":
        room = ""
    data = await state.get_data()
    await state.clear()
    sid = add_subject(data["name"], data.get("teacher", ""), room)
    await render_subject_view(message, sid)


# ------------------------------------------------------------------ Callback Handlers: Dars Jadvali

@router.callback_query(F.data.startswith("uni:tt_day:"))
async def cb_uni_tt_day(call: CallbackQuery, state: FSMContext):
    await state.clear()
    wday = int(call.data.split(":")[2])
    await call.answer()
    await render_timetable_day(call.message, wday)


@router.callback_query(F.data.startswith("uni:tt_edit:"))
async def cb_uni_tt_edit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    wday = int(parts[2])
    p_num = int(parts[3])
    parity = parts[4]
    await call.answer()
    await render_timetable_slot_editor(call.message, wday, p_num, parity)


@router.callback_query(F.data.startswith("uni:tt_del:"))
async def cb_uni_tt_del(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    wday = int(parts[2])
    p_num = int(parts[3])
    parity = parts[4]
    delete_timetable_slot(wday, p_num, parity)
    await schedule_today_uni()
    await call.answer("Dars jadvaldan o'chirildi")
    await render_timetable_day(call.message, wday)


@router.callback_query(F.data.startswith("uni:tt_set_sub:"))
async def cb_uni_tt_set_sub(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    wday = int(parts[2])
    p_num = int(parts[3])
    parity = parts[4]
    sid = int(parts[5])

    # Dars turini tanlash
    text = (
        "🏷 <b>Dars turini tanlang:</b>"
    )
    buttons = []
    for l_key, l_name in LESSON_TYPES.items():
        buttons.append([btn(l_name, f"uni:tt_set_type:{wday}:{p_num}:{parity}:{sid}:{l_key}")])
    buttons.append([btn("🔙 Bekor qilish", f"uni:tt_day:{wday}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.callback_query(F.data.startswith("uni:tt_set_type:"))
async def cb_uni_tt_set_type(call: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = call.data.split(":")
    wday = int(parts[2])
    p_num = int(parts[3])
    parity = parts[4]
    sid = int(parts[5])
    l_type = parts[6]

    set_timetable_slot(wday, p_num, sid, lesson_type=l_type, week_parity=parity)
    await schedule_today_uni()
    await call.answer("Dars muvaffaqiyatli saqlandi")
    await render_timetable_day(call.message, wday)


# ------------------------------------------------------------------ Callback Handlers: Uy Vazifalari

@router.callback_query(F.data == "uni:hw_list")
async def cb_uni_hw_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await render_homeworks_list(call.message)


@router.callback_query(F.data.startswith("uni:hw_subj:"))
async def cb_uni_hw_subj(call: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(call.data.split(":")[2])
    await call.answer()
    await render_homeworks_list(call.message, subject_filter=sid)


@router.callback_query(F.data.startswith("uni:hw_toggle:"))
async def cb_uni_hw_toggle(call: CallbackQuery, state: FSMContext):
    await state.clear()
    hw_id = int(call.data.split(":")[2])
    toggle_homework(hw_id)
    await call.answer("Vazifa holati yangilandi")
    await render_homeworks_list(call.message)


@router.callback_query(F.data.startswith("uni:hw_del:"))
async def cb_uni_hw_del(call: CallbackQuery, state: FSMContext):
    await state.clear()
    hw_id = int(call.data.split(":")[2])
    delete_homework(hw_id)
    await call.answer("Vazifa o'chirildi")
    await render_homeworks_list(call.message)


@router.callback_query(F.data == "uni:hw_add_select")
async def cb_uni_hw_add_select(call: CallbackQuery, state: FSMContext):
    await state.clear()
    subs = get_subjects(only_active=True)
    if not subs:
        await call.answer("Avval fan qo'shing!", show_alert=True)
        return await render_subjects_list(call.message)

    text = "📝 <b>Qaysi fan uchun vazifa kiritmoqchisiz?</b>"
    buttons = []
    for s in subs:
        buttons.append([btn(s["name"][:28], f"uni:hw_add:{s['id']}")])
    buttons.append([btn("🔙 Bekor qilish", "uni:hw_list")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.callback_query(F.data.startswith("uni:hw_add:"))
async def cb_uni_hw_add(call: CallbackQuery, state: FSMContext):
    sid = int(call.data.split(":")[2])
    sub = get_subject(sid)
    if not sub:
        await call.answer("Fan topilmadi")
        return await render_homeworks_list(call.message)

    await state.set_state(UniFSM.add_hw_task)
    await state.update_data(subject_id=sid, subject_name=sub["name"])

    # Dars turi tanlash
    text = (
        f"📝 <b>Uy vazifasi qo'shish — {app.esc(sub['name'])}</b>\n\n"
        "Qaysi mashg'ulot turi uchun topshiriq ekanini tanlang yoki «Umumiy» deb qoldiring:"
    )
    buttons = [
        [btn("Umumiy / Har qanday", f"uni:hw_choose_type:{sid}:none")],
        [btn("🎙 Ma'ruza", f"uni:hw_choose_type:{sid}:lecture"), btn("✍️ Amaliy", f"uni:hw_choose_type:{sid}:practice")],
        [btn("🗣 Seminar", f"uni:hw_choose_type:{sid}:seminar"), btn("🔬 Laboratoriya", f"uni:hw_choose_type:{sid}:lab")],
        [btn("🔙 Bekor qilish", "uni:hw_list")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.callback_query(F.data.startswith("uni:hw_choose_type:"))
async def cb_uni_hw_choose_type(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    sid = int(parts[2])
    l_type = None if parts[3] == "none" else parts[3]
    await state.set_state(UniFSM.add_hw_task)
    await state.update_data(subject_id=sid, target_lesson_type=l_type)

    sub = get_subject(sid)
    sub_name = sub["name"] if sub else ""
    l_type_str = f" ({LESSON_TYPES.get(l_type, l_type)})" if l_type else ""

    text = (
        f"📝 <b>Uy vazifasi — {app.esc(sub_name)}{l_type_str}</b>\n\n"
        "Vazifa mazmunini yuboring (masalan: <i>5-laboratoriya hisoboti va grafiklarni tayyorlash</i>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Bekor qilish", "uni:hw_list")]])
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.message(UniFSM.add_hw_task)
async def fsm_add_hw_task(message: Message, state: FSMContext):
    task_text = (message.text or "").strip()
    if not task_text:
        return await message.answer("Iltimos, vazifa matnini yuboring:")

    await state.update_data(task_text=task_text)
    await state.set_state(UniFSM.add_hw_deadline)
    data = await state.get_data()
    sid = data["subject_id"]
    l_type = data.get("target_lesson_type")

    # Keyingi darsni topish
    next_d = find_next_lesson_date(sid, l_type)
    buttons = []
    if next_d:
        w_name = WEEKDAY_NAMES_UZ[next_d.weekday()]
        buttons.append([btn(f"⏭ Keyingi darsgacha ({next_d.strftime('%d.%m')}, {w_name})", f"uni:hw_dl_auto:{next_d.isoformat()}")])

    buttons.append([btn("⏳ Ertagacha", f"uni:hw_dl_auto:{(app.today() + timedelta(days=1)).isoformat()}")])
    buttons.append([btn("🚫 Muddatsiz", "uni:hw_dl_auto:none")])
    buttons.append([btn("🔙 Bekor qilish", "uni:hw_list")])

    text = (
        f"📝 Vazifa: <b>{app.esc(task_text)}</b>\n\n"
        "Topshirish muddatini tanlang yoki sanani <code>YYYY-MM-DD</code> formatida yozib yuboring:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("uni:hw_dl_auto:"), UniFSM.add_hw_deadline)
async def cb_uni_hw_dl_auto(call: CallbackQuery, state: FSMContext):
    raw_dl = call.data.split(":")[2]
    deadline = None if raw_dl == "none" else raw_dl
    data = await state.get_data()
    await state.clear()
    add_homework(
        subject_id=data["subject_id"],
        target_lesson_type=data.get("target_lesson_type"),
        task_text=data["task_text"],
        due_date=deadline,
    )
    await call.answer("Vazifa muvaffaqiyatli qo'shildi!")
    await render_homeworks_list(call.message)


@router.message(UniFSM.add_hw_deadline)
async def fsm_add_hw_deadline(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    dl = None
    if txt not in ("-", "yo'q", "none"):
        try:
            dl = date.fromisoformat(txt).isoformat()
        except Exception:
            return await message.answer("Sana formati noto'g'ri. <code>YYYY-MM-DD</code> formatida yuboring (yoki «-» deb yozing):")
    data = await state.get_data()
    await state.clear()
    add_homework(
        subject_id=data["subject_id"],
        target_lesson_type=data.get("target_lesson_type"),
        task_text=data["task_text"],
        due_date=dl,
    )
    await message.answer("✅ Uy vazifasi muvaffaqiyatli saqlandi!")
    await render_homeworks_list(message)


# ------------------------------------------------------------------ Callback Handlers: Akademik Ishlar

@router.callback_query(F.data == "uni:works_list")
async def cb_uni_works_list(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await render_works_list(call.message)


@router.callback_query(F.data.startswith("uni:work_view:"))
async def cb_uni_work_view(call: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(call.data.split(":")[2])
    await call.answer()
    await render_work_detail(call.message, wid)


@router.callback_query(F.data.startswith("uni:step_toggle:"))
async def cb_uni_step_toggle(call: CallbackQuery, state: FSMContext):
    await state.clear()
    step_id = int(call.data.split(":")[2])
    new_st, wid = toggle_work_step(step_id)
    await call.answer("Bosqich holati yangilandi")
    await render_work_detail(call.message, wid)


@router.callback_query(F.data.startswith("uni:work_del:"))
async def cb_uni_work_del(call: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(call.data.split(":")[2])
    delete_work(wid)
    await call.answer("Akademik ish o'chirildi")
    await render_works_list(call.message)


@router.callback_query(F.data == "uni:work_add_type")
async def cb_uni_work_add_type(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "🎓 <b>Qanday akademik ish qo'shmoqchisiz?</b>\n\n"
        "Shablonni tanlang:"
    )
    buttons = [
        [btn("🎓 BMI (Bitiruv Malakaviy Ishi — 7 bosqich)", "uni:work_start:bmi")],
        [btn("📑 Kurs ishi (4 bosqich)", "uni:work_start:course_work")],
        [btn("📝 Mustaqil ish (3 bosqich)", "uni:work_start:independent")],
        [btn("📌 Boshqa / Maxsus ish", "uni:work_start:custom")],
        [btn("🔙 Bekor qilish", "uni:works_list")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.callback_query(F.data.startswith("uni:work_start:"))
async def cb_uni_work_start(call: CallbackQuery, state: FSMContext):
    w_type = call.data.split(":")[2]
    await state.set_state(UniFSM.add_work_title)
    await state.update_data(work_type=w_type)

    w_name = WORK_TYPES.get(w_type, w_type)
    text = (
        f"<b>{w_name}</b>\n\n"
        "Mavzu yoki ish sarlavhasini yuboring (masalan: <i>Mashinali o'rganish usullari yordamida ma'lumotlarni tahlil qilish</i>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Bekor qilish", "uni:works_list")]])
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.message(UniFSM.add_work_title)
async def fsm_add_work_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        return await message.answer("Iltimos, mavzu nomini yuboring:")
    await state.update_data(title=title)
    await state.set_state(UniFSM.add_work_deadline)

    text = (
        f"Mavzu: <b>{app.esc(title)}</b>\n\n"
        "Himoya / topshirish muddatini yuboring (<code>YYYY-MM-DD</code> formatida yoki muddatsiz bo'lsa «-» deb yuboring):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Muddatsiz (—)", "uni:work_dl_skip")]])
    await message.answer(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "uni:work_dl_skip", UniFSM.add_work_deadline)
async def cb_uni_work_dl_skip(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    wid = create_work(data["work_type"], data["title"], deadline=None)
    await call.answer("Akademik ish yaratildi!")
    await render_work_detail(call.message, wid)


@router.message(UniFSM.add_work_deadline)
async def fsm_add_work_deadline(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    dl = None
    if txt not in ("-", "yo'q", "none"):
        try:
            dl = date.fromisoformat(txt).isoformat()
        except Exception:
            return await message.answer("Sana formati noto'g'ri. <code>YYYY-MM-DD</code> formatida yuboring (yoki «-» deb yozing):")
    data = await state.get_data()
    await state.clear()
    wid = create_work(data["work_type"], data["title"], deadline=dl)
    await message.answer("✅ Akademik ish muvaffaqiyatli saqlandi!")
    await render_work_detail(message, wid)


@router.callback_query(F.data.startswith("uni:step_add:"))
async def cb_uni_step_add(call: CallbackQuery, state: FSMContext):
    wid = int(call.data.split(":")[2])
    await state.set_state(UniFSM.add_work_step)
    await state.update_data(work_id=wid)
    text = (
        "➕ <b>Yangi bosqich qo'shish</b>\n\n"
        "Bosqich nomini yuboring (masalan: <i>Mavzuga doir xorijiy maqolalar referatini tuzish</i>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔙 Bekor qilish", f"uni:work_view:{wid}")]])
    await call.answer()
    await _edit_or_send(call.message, text, kb)


@router.message(UniFSM.add_work_step)
async def fsm_add_work_step(message: Message, state: FSMContext):
    step_title = (message.text or "").strip()
    if not step_title:
        return await message.answer("Iltimos, bosqich nomini matn ko'rinishida yuboring:")
    data = await state.get_data()
    wid = data["work_id"]
    await state.clear()
    add_work_step(wid, step_title)
    await message.answer("✅ Yangi bosqich qo'shildi!")
    await render_work_detail(message, wid)
