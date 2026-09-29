"""
RejaliBot 2.0
Python 3.10+, aiogram 3.x, APScheduler, SQLite.
"""

import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402
import qazo as qz  # noqa: E402
import scoring  # noqa: E402
import services as sv  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
COUNTRY = os.environ.get("COUNTRY", "Uzbekistan")
TZ = sv.TZ

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)


def get_city() -> str:
    return db.get_setting("city", os.environ.get("CITY", "Chirchiq"))


def esc(s: str) -> str:
    return html.escape(s or "")


# ------------------------------------------------------------------ xavfsizlik

class OwnerOnlyMiddleware(BaseMiddleware):
    """Faqat CHAT_ID egasidan kelgan update'lar handlerlarga o'tadi."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None or user.id != CHAT_ID:
            logger.warning(f"Ruxsatsiz urinish: id={getattr(user, 'id', None)}")
            return
        return await handler(event, data)


dp.message.middleware(OwnerOnlyMiddleware())
dp.callback_query.middleware(OwnerOnlyMiddleware())


@dp.errors()
async def global_error_handler(event):
    logger.exception(f"Kutilmagan xatolik: {event.exception}")
    return True


# ------------------------------------------------------------------ eslatma yuborish

PRIORITY_ICON = {1: "", 2: "❗", 3: "🔥"}


def get_checkpoints(occ_id: int) -> list[dict]:
    """Eslatmaning chek-pointlari va ularning bugungi holati."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title,
                   COALESCE((SELECT done FROM checkpoint_status s
                             WHERE s.checkpoint_id = c.id AND s.occurrence_id = ?), 0) AS done
            FROM checkpoints c
            WHERE c.reminder_id = (SELECT reminder_id FROM occurrences WHERE id = ?)
            ORDER BY c.position
            """,
            (occ_id, occ_id),
        ).fetchall()
    return [dict(r) for r in rows]


def action_keyboard(occ_id: int) -> InlineKeyboardMarkup:
    rows = []
    for cp in get_checkpoints(occ_id):
        mark = "☑️" if cp["done"] else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {cp['title']}", callback_data=f"cp:{occ_id}:{cp['id']}")])
    rows.append([
        InlineKeyboardButton(text="✅ Bajardim", callback_data=f"done:{occ_id}"),
        InlineKeyboardButton(text="❌ Bajarmadim", callback_data=f"skip:{occ_id}"),
    ])
    rows.append([InlineKeyboardButton(text="⏳ Kechiktirish", callback_data=f"post:{occ_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_line() -> str:
    """Har eslatmada ko'rinadigan joriy hafta ballari."""
    monday, _ = scoring.week_bounds(now_date())
    s = scoring.week_summary(monday)
    return f"⭐ Bu hafta: {s['points']} ball ({s['done']}/{s['total']})"


def now_date() -> date:
    return sv.now().date()


async def send_reminder(occ_id: int):
    occ = sv.get_occurrence(occ_id)
    if occ is None or occ["status"] != "pending":
        return
    icon = PRIORITY_ICON.get(sv.get_priority(occ), "")
    text = f"⏰ {icon} <b>{esc(occ['title'])}</b>"
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    text += f"\n🕒 {scheduled.strftime('%H:%M')}"
    text += f"\n\n{status_line()}"
    try:
        await bot.send_message(CHAT_ID, text, reply_markup=action_keyboard(occ_id))
    except Exception as e:
        logger.error(f"Eslatma yuborishda xatolik ({occ_id}): {e}")


async def send_info(text: str):
    """Sub-task: faqat ma'lumot, tugmasiz, ballsiz."""
    try:
        await bot.send_message(CHAT_ID, f"ℹ️ {esc(text)}")
    except Exception as e:
        logger.error(f"Ma'lumot yuborishda xatolik: {e}")


# ------------------------------------------------------------------ kunlik jadval tuzish

def schedule_job(run_at: datetime, func, args: list, job_id: str):
    if run_at <= sv.now():
        return
    scheduler.add_job(func, trigger="date", run_date=run_at, args=args, id=job_id, replace_existing=True)


async def schedule_today():
    """Bugungi barcha eslatmalarni schedulerga qo'yadi. Har kuni 00:05 va ishga tushganda."""
    today = now_date()
    city = get_city()
    # 1) Standart va maxsus eslatmalar
    for r in sv.reminders_for_day(today):
        occ_id = sv.ensure_occurrence(r, today)
        schedule_job(sv.at(today, r["start"]), send_reminder, [occ_id], f"rem_{occ_id}")

    # 2) Namoz vaqtlari
    try:
        times = sv.fetch_prayer_times(city, COUNTRY, today)
        for name in ("Bomdod", "Peshin", "Asr", "Shom", "Xufton"):
            occ_id = sv.ensure_prayer_occurrence(name, today, times[name])
            schedule_job(sv.at(today, times[name]), send_reminder, [occ_id], f"rem_{occ_id}")
    except Exception as e:
        logger.error(f"Namoz vaqtlarini olishda xatolik: {e}")

    # 3) Maktab (sub-task: ballsiz, tugmasiz)
    for item in sv.school_lesson_reminders(today):
        schedule_job(item["at"], send_info, [item["text"]], f"school_{item['at'].strftime('%H%M')}")

    # 4) Universitet
    uni = sv.university_reminder(today)
    if uni:
        schedule_job(uni["at"], send_info, [uni["text"]], "university")

    logger.info(f"{today} uchun eslatmalar rejalashtirildi.")


# ------------------------------------------------------------------ qazo avtomatik tekshiruv

async def check_qazo():
    try:
        created = sv.find_new_qazo(sv.now(), get_city(), COUNTRY)
    except Exception as e:
        logger.error(f"Qazo tekshiruvida xatolik: {e}")
        return
    for q in created:
        try:
            await bot.send_message(
                CHAT_ID,
                f"⚠️ <b>{esc(q['title'])}</b> qazo bo'ldi.\n"
                f"Qazosini o'qigach, shu kodni yuboring: /{q['code']}",
            )
        except Exception as e:
            logger.error(f"Qazo xabarida xatolik: {e}")


# ------------------------------------------------------------------ tugmalar

def fmt_late(minutes: int) -> str:
    if minutes < 1:
        return "vaqtida"
    if minutes < 60:
        return f"{minutes} daqiqa kechikib"
    h, m = divmod(minutes, 60)
    return f"{h} soat {m} daqiqa kechikib" if m else f"{h} soat kechikib"


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    occ_id = int(callback.data.split(":")[1])
    result = sv.mark_done(occ_id)
    if "error" in result:
        await callback.answer("Allaqachon belgilangan", show_alert=False)
        return
    line = f"✅ Bajarildi ({fmt_late(result['late_minutes'])})"
    if result["points"]:
        line += f"\n⭐ +{result['points']} ball"
        if result["was_postponed"]:
            line += " (kechiktirilgan, yarim ball)"
    await callback.message.edit_text(f"{callback.message.html_text}\n\n{line}", reply_markup=None)
    await callback.answer()


@dp.callback_query(F.data.startswith("cp:"))
async def cb_checkpoint(callback: CallbackQuery):
    _, occ_id_s, cp_id_s = callback.data.split(":")
    occ_id, cp_id = int(occ_id_s), int(cp_id_s)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT done FROM checkpoint_status WHERE checkpoint_id = ? AND occurrence_id = ?", (cp_id, occ_id)
        ).fetchone()
        new_val = 0 if (row and row["done"]) else 1
        conn.execute(
            "INSERT INTO checkpoint_status (checkpoint_id, occurrence_id, done) VALUES (?, ?, ?) "
            "ON CONFLICT(checkpoint_id, occurrence_id) DO UPDATE SET done = excluded.done",
            (cp_id, occ_id, new_val),
        )
    await callback.message.edit_reply_markup(reply_markup=action_keyboard(occ_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(callback: CallbackQuery):
    occ_id = int(callback.data.split(":")[1])
    if sv.mark_skipped(occ_id):
        await callback.message.edit_text(f"{callback.message.html_text}\n\n❌ Bajarilmadi", reply_markup=None)
    await callback.answer()


class Postpone(StatesGroup):
    waiting_date = State()


def postpone_keyboard(occ_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="+15 daqiqa", callback_data=f"pq:{occ_id}:15"),
                InlineKeyboardButton(text="+1 soat", callback_data=f"pq:{occ_id}:60"),
            ],
            [
                InlineKeyboardButton(text="Ertaga", callback_data=f"pq:{occ_id}:tom"),
                InlineKeyboardButton(text="Boshqa sana", callback_data=f"pq:{occ_id}:other"),
            ],
        ]
    )


@dp.callback_query(F.data.startswith("post:"))
async def cb_postpone(callback: CallbackQuery):
    occ_id = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=postpone_keyboard(occ_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("pq:"))
async def cb_postpone_choice(callback: CallbackQuery, state: FSMContext):
    _, occ_id_s, choice = callback.data.split(":")
    occ_id = int(occ_id_s)
    occ = sv.get_occurrence(occ_id)
    if occ is None:
        await callback.answer("Topilmadi")
        return
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    current = sv.now()

    if choice == "other":
        await state.set_state(Postpone.waiting_date)
        await state.update_data(occ_id=occ_id)
        await callback.message.answer(
            "Qachonga ko'chiramiz? Sana va vaqtni yozing: <code>2026-10-05 09:30</code>\n"
            "Faqat sana yozsangiz (<code>2026-10-05</code>), asl vaqti saqlanadi. Bekor qilish: /bekor"
        )
        await callback.answer()
        return

    if choice == "tom":
        target = scheduled + timedelta(days=1)
    else:
        target = current + timedelta(minutes=int(choice))

    await finish_postpone(callback.message, occ_id, target)
    await callback.answer()


async def finish_postpone(message: Message, occ_id: int, target: datetime):
    if sv.postpone(occ_id, target):
        db_target = target.astimezone(TZ)
        # Bugungi bo'lsa, darhol schedulerga qo'yamiz
        if db_target.date() == now_date():
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT id FROM occurrences WHERE scheduled_at = ? AND status = 'pending' "
                    "AND reminder_id = (SELECT reminder_id FROM occurrences WHERE id = ?)",
                    (target.isoformat(), occ_id),
                ).fetchone()
            if row:
                schedule_job(target, send_reminder, [row["id"]], f"rem_{row['id']}")
        await message.answer(f"⏳ Kechiktirildi: {db_target.strftime('%d.%m.%Y %H:%M')}")
    else:
        await message.answer("Kechiktirib bo'lmadi (allaqachon bajarilgan yoki topilmadi).")


@dp.message(Postpone.waiting_date)
async def process_postpone_date(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Sanani matn sifatida yozing.")
        return
    data = await state.get_data()
    occ = sv.get_occurrence(data["occ_id"])
    text = message.text.strip()
    target = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d" and occ:
                asl = datetime.fromisoformat(occ["scheduled_at"])
                parsed = parsed.replace(hour=asl.hour, minute=asl.minute)
            target = parsed.replace(tzinfo=TZ)
            break
        except ValueError:
            continue
    if target is None:
        await message.answer("Format noto'g'ri. Masalan: <code>2026-10-05 09:30</code>")
        return
    if target <= sv.now():
        await message.answer("Bu vaqt allaqachon o'tgan. Kelajakdagi vaqtni yozing.")
        return
    await state.clear()
    await finish_postpone(message, data["occ_id"], target)


# ------------------------------------------------------------------ /qazolar va qazo kodlari

@dp.message(Command("qazolar"))
async def cmd_qazolar(message: Message):
    items = qz.open_qazo()
    if not items:
        await message.answer("✅ Ochiq qazo yo'q.")
        return
    lines = ["<b>Ochiq qazolar:</b>\n"]
    for q in items:
        lines.append(f"• {esc(q['title'])} ({q['date']}): /{q['code']}")
    lines.append("\nO'qigan bo'lsangiz, tegishli kodni yuboring.")
    await message.answer("\n".join(lines))


@dp.message(F.text.regexp(qz.CODE_RE))
async def qazo_code_handler(message: Message):
    parsed = qz.parse_code(message.text)
    if parsed is None:
        return
    code = message.text.strip().lstrip("/").lower()
    if qz.mark_qazo_done(code, sv.now().isoformat()):
        await message.answer(f"✅ Qazo chiqarildi: {code}. Alloh qabul qilsin.")
    else:
        await message.answer("Bunday ochiq qazo topilmadi (kod noto'g'ri yoki allaqachon chiqarilgan).")


# ------------------------------------------------------------------ /bugun va ertalabki xulosa

async def build_today_summary() -> str:
    today = now_date()
    weekday = today.weekday()
    lines = [f"<b>{sv.WEEKDAY_NAMES[weekday]} kuni rejasi:</b>\n"]
    items = []
    for r in sv.reminders_for_day(today):
        icon = PRIORITY_ICON.get(r["priority"], "")
        items.append((r["start"], f"{icon} {esc(r['title'])}".strip()))
    try:
        times = sv.fetch_prayer_times(get_city(), COUNTRY, today)
        for name in ("Bomdod", "Peshin", "Asr", "Shom", "Xufton"):
            items.append((times[name], f"{name} namozi"))
    except Exception as e:
        logger.error(f"Namoz vaqtlarini olishda xatolik: {e}")
    items.sort(key=lambda x: x[0])
    for t, label in items:
        lines.append(f"🕒 {t} — {label}")
    return "\n".join(lines)


def open_qazo_block() -> str:
    items = qz.open_qazo()
    if not items:
        return ""
    lines = ["\n\n⚠️ <b>Ochiq qazolar:</b>"]
    for q in items:
        lines.append(f"• {esc(q['title'])} ({q['date']}): /{q['code']}")
    return "\n".join(lines)


@dp.message(Command("bugun"))
async def cmd_today(message: Message):
    await message.answer(await build_today_summary() + open_qazo_block())


async def send_morning_summary():
    text = await build_today_summary() + open_qazo_block()
    try:
        await bot.send_message(CHAT_ID, f"🌅 <b>Xayrli tong!</b>\n\n{text}")
    except Exception as e:
        logger.error(f"Ertalabki xulosada xatolik: {e}")


async def send_evening_report():
    """Kun yakuni: qoldirilgan eslatmalar."""
    today = now_date()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT title, status FROM occurrences WHERE date = ? AND is_subtask = 0 "
            "AND status IN ('skipped', 'pending', 'postponed', 'qazo')",
            (today.isoformat(),),
        ).fetchall()
    if not rows:
        return
    names = {"skipped": "bajarilmadi", "pending": "javobsiz qoldi", "postponed": "kechiktirildi", "qazo": "qazo"}
    lines = ["🌙 <b>Qoldirilgan eslatmalar:</b>\n"]
    for r in rows:
        lines.append(f"• {esc(r['title'])} — {names.get(r['status'], r['status'])}")
    lines.append(open_qazo_block())
    try:
        await bot.send_message(CHAT_ID, "\n".join(lines))
    except Exception as e:
        logger.error(f"Kechki xulosada xatolik: {e}")


# ------------------------------------------------------------------ /qoshish (title, note, chek-point, ustuvorlik, ball)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AddReminder(StatesGroup):
    kind = State()
    weekday = State()
    date = State()
    time = State()
    title = State()
    note = State()
    checkpoints = State()
    priority = State()
    scored = State()
    points = State()


def yes_no_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Ha", callback_data=f"{prefix}:yes"),
            InlineKeyboardButton(text="Yo'q", callback_data=f"{prefix}:no"),
        ]]
    )


@dp.message(Command("qoshish"))
async def cmd_add(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddReminder.kind)
    await message.answer(
        "Yangi eslatma.\n\n1 — Har kuni\n2 — Haftaning bitta kunida\n3 — Aniq sanada (bir marta)\n\n"
        "Raqamni yozing. Bekor qilish: /bekor"
    )


@dp.message(Command("bekor"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Bekor qilinadigan jarayon yo'q.")
        return
    await state.clear()
    await message.answer("Bekor qilindi.")


@dp.message(AddReminder.kind)
async def add_kind(message: Message, state: FSMContext):
    c = (message.text or "").strip()
    if c == "1":
        await state.update_data(kind="daily")
        await state.set_state(AddReminder.time)
        await message.answer("Soatini yozing (masalan 22:00):")
    elif c == "2":
        await state.update_data(kind="weekly")
        await state.set_state(AddReminder.weekday)
        await message.answer("Qaysi kuni? (dushanba, seshanba, ... yakshanba)")
    elif c == "3":
        await state.update_data(kind="once")
        await state.set_state(AddReminder.date)
        await message.answer("Sanani yozing (YYYY-MM-DD, masalan 2026-10-05):")
    else:
        await message.answer("1, 2 yoki 3 ni yozing.")


@dp.message(AddReminder.weekday)
async def add_weekday(message: Message, state: FSMContext):
    w = (message.text or "").strip().lower()
    if w not in sv.WEEKDAY_LOOKUP:
        await message.answer("Kunni to'g'ri yozing: dushanba, seshanba, ... yakshanba")
        return
    await state.update_data(weekday=sv.WEEKDAY_LOOKUP[w])
    await state.set_state(AddReminder.time)
    await message.answer("Soatini yozing (masalan 18:00):")


@dp.message(AddReminder.date)
async def add_date(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    try:
        d = datetime.strptime(t, "%Y-%m-%d").date()
    except ValueError:
        await message.answer("Format: YYYY-MM-DD (masalan 2026-10-05)")
        return
    await state.update_data(date=d.isoformat())
    await state.set_state(AddReminder.time)
    await message.answer("Soatini yozing (masalan 09:00):")


@dp.message(AddReminder.time)
async def add_time(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not TIME_RE.match(t):
        await message.answer("Format: HH:MM (masalan 09:30)")
        return
    h, m = sv.parse_hhmm(t)
    await state.update_data(time=f"{h:02d}:{m:02d}")
    await state.set_state(AddReminder.title)
    await message.answer("Sarlavha (Title): qisqa nom, masalan <i>Seminarga tayyorlanish</i>")


@dp.message(AddReminder.title)
async def add_title(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not t:
        await message.answer("Sarlavhani matn sifatida yozing.")
        return
    await state.update_data(title=t)
    await state.set_state(AddReminder.note)
    await message.answer("Izoh (Note): mavzu, havola, qo'shimcha ma'lumot. Kerak bo'lmasa <code>-</code> yozing.")


@dp.message(AddReminder.note)
async def add_note(message: Message, state: FSMContext):
    n = (message.text or "").strip()
    await state.update_data(note="" if n == "-" else n)
    await state.set_state(AddReminder.checkpoints)
    await message.answer(
        "Ichida chek-pointlar (kichik tekshiruvlar) bormi?\n"
        "Bo'lsa, har birini yangi qatordan yozing, bo'lmasa <code>-</code> yozing.\n"
        "Masalan:\n<code>Tish yuvish\nYuzni sovunlash</code>"
    )


@dp.message(AddReminder.checkpoints)
async def add_checkpoints(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    items = [] if raw == "-" else [line.strip() for line in raw.splitlines() if line.strip()]
    await state.update_data(checkpoints=items)
    await state.set_state(AddReminder.priority)
    await message.answer(
        "Ustuvorlik:\n1 — oddiy\n2 — muhim ❗\n3 — juda muhim 🔥\n\nRaqamni yozing."
    )


@dp.message(AddReminder.priority)
async def add_priority(message: Message, state: FSMContext):
    p = (message.text or "").strip()
    if p not in ("1", "2", "3"):
        await message.answer("1, 2 yoki 3 ni yozing.")
        return
    await state.update_data(priority=int(p))
    await state.set_state(AddReminder.scored)
    await message.answer("Bu eslatma ballga ta'sir qilsinmi?", reply_markup=yes_no_keyboard("addscore"))


@dp.callback_query(AddReminder.scored, F.data.startswith("addscore:"))
async def add_scored(callback: CallbackQuery, state: FSMContext):
    scored = callback.data.endswith("yes")
    await state.update_data(scored=scored)
    if scored:
        await state.set_state(AddReminder.points)
        await callback.message.edit_text("Necha ball beriladi? (butun son, masalan 10)")
        await callback.answer()
        return
    await finalize_reminder(callback.message, state, points=0, is_callback=True)
    await callback.answer()


@dp.message(AddReminder.points)
async def add_points(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not t.isdigit() or int(t) <= 0:
        await message.answer("Musbat butun son yozing, masalan 10")
        return
    await finalize_reminder(message, state, points=int(t), is_callback=False)


async def finalize_reminder(message: Message, state: FSMContext, points: int, is_callback: bool):
    data = await state.get_data()
    kind = data["kind"]
    scored = data["scored"]
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (source, kind, weekday, date, start, title, note, priority, scored, points) "
            "VALUES ('custom', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, data.get("weekday"), data.get("date"), data["time"], data["title"],
             data.get("note", ""), data["priority"], int(scored), points),
        )
        rid = cur.lastrowid
        for i, cp in enumerate(data.get("checkpoints", [])):
            conn.execute(
                "INSERT INTO checkpoints (reminder_id, title, position) VALUES (?, ?, ?)", (rid, cp, i)
            )
    await state.clear()

    # Bugungi bo'lsa, darhol rejalashtiramiz
    today = now_date()
    is_today = (
        kind == "daily"
        or (kind == "weekly" and data.get("weekday") == today.weekday())
        or (kind == "once" and data.get("date") == today.isoformat())
    )
    if is_today:
        with db.connect() as conn:
            r = dict(conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())
        occ_id = sv.ensure_occurrence(r, today)
        schedule_job(sv.at(today, r["start"]), send_reminder, [occ_id], f"rem_{occ_id}")

    cps = data.get("checkpoints", [])
    extra = f"\nChek-pointlar: {len(cps)} ta" if cps else ""
    ball_line = f", ball: {points}" if scored else ", ball: yo‘q"
    text = (
        f"✅ Eslatma qo'shildi (#{rid}): <b>{esc(data['title'])}</b>{extra}\n"
        f"Ustuvorlik: {scoring.PRIORITY_NAMES[data['priority']]}{ball_line}"
    )
    if is_callback:
        await message.edit_text(text)
    else:
        await message.answer(text)


# ------------------------------------------------------------------ /royxat, /ochirish

@dp.message(Command("royxat"))
async def cmd_list(message: Message):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE source = 'custom' AND active = 1 ORDER BY id").fetchall()
    if not rows:
        await message.answer("Maxsus eslatmalar yo'q. Qo'shish: /qoshish")
        return
    lines = ["<b>Maxsus eslatmalar:</b>\n"]
    for r in rows:
        if r["kind"] == "daily":
            when = f"har kuni {r['start']}"
        elif r["kind"] == "weekly":
            when = f"har {sv.WEEKDAY_NAMES[r['weekday']]} {r['start']}"
        else:
            when = f"{r['date']} {r['start']}"
        lines.append(f"#{r['id']} {esc(r['title'])} ({when})")
    lines.append(f"\nO'chirish: /ochirish {rows[0]['id']}")
    await message.answer("\n".join(lines))


@dp.message(Command("ochirish"))
async def cmd_delete(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("#").isdigit():
        await message.answer("Format: /ochirish 3 (ID ni /royxat dan ko'ring)")
        return
    rid = int(parts[1].strip().lstrip("#"))
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET active = 0 WHERE id = ? AND source = 'custom' AND active = 1", (rid,)
        )
    await message.answer(f"✅ #{rid} o'chirildi." if cur.rowcount else f"#{rid} topilmadi.")


# ------------------------------------------------------------------ /vaqt: standart eslatma vaqtini o'zgartirish

@dp.message(Command("vaqt"))
async def cmd_time(message: Message):
    """
    /vaqt zal 09:30          -> bugun uchun (bir kunlik)
    /vaqt zal 09:30 doimiy   -> bu kun turi uchun doimiy
    /vaqt                    -> mavjud slug'lar ro'yxati
    """
    parts = (message.text or "").split()
    if len(parts) < 3:
        with db.connect() as conn:
            slugs = [r["slug"] for r in conn.execute(
                "SELECT DISTINCT slug FROM reminders WHERE source = 'standard' AND slug IS NOT NULL ORDER BY slug"
            ).fetchall()]
        await message.answer(
            "Format:\n<code>/vaqt zal 09:30</code> — faqat bugun\n"
            "<code>/vaqt zal 09:30 doimiy</code> — doimiy\n\n"
            f"Mavjud nomlar: {', '.join(slugs)}"
        )
        return
    slug, t = parts[1].lower(), parts[2]
    permanent = len(parts) > 3 and parts[3].lower() in ("doimiy", "d")
    if not TIME_RE.match(t):
        await message.answer("Vaqt formati: HH:MM (masalan 09:30)")
        return
    h, m = sv.parse_hhmm(t)
    t = f"{h:02d}:{m:02d}"
    today = now_date()
    with db.connect() as conn:
        if permanent:
            cur = conn.execute(
                "UPDATE reminders SET start = ? WHERE source = 'standard' AND slug = ? AND active = 1 "
                "AND (kind = 'daily' OR weekday = ?)", (t, slug, today.weekday())
            )
            ok = cur.rowcount > 0
        else:
            row = conn.execute(
                "SELECT id FROM reminders WHERE source = 'standard' AND slug = ? AND active = 1 "
                "AND (kind = 'daily' OR weekday = ?)", (slug, today.weekday())
            ).fetchone()
            ok = row is not None
            if ok:
                conn.execute(
                    "INSERT INTO overrides (reminder_id, date, start) VALUES (?, ?, ?) "
                    "ON CONFLICT(reminder_id, date) DO UPDATE SET start = excluded.start",
                    (row["id"], today.isoformat(), t),
                )
    if not ok:
        await message.answer(f"Bugun uchun '{esc(slug)}' topilmadi. Nomlar: /vaqt")
        return
    await schedule_today()
    await message.answer(f"✅ {esc(slug)} → {t} ({'doimiy' if permanent else 'faqat bugun'})")


# ------------------------------------------------------------------ /sinf: maktab sinflar tartibini o'zgartirish

@dp.message(Command("sinf"))
async def cmd_school(message: Message):
    """/sinf juma 7b 8b 11b 9b 10b"""
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[1].lower() not in sv.WEEKDAY_LOOKUP:
        await message.answer("Format: <code>/sinf juma 7b 8b 11b 9b 10b</code>")
        return
    wd = sv.WEEKDAY_LOOKUP[parts[1].lower()]
    classes = parts[2:]
    if len(classes) > 7:
        await message.answer("Ko'pi bilan 7 ta dars bo'lishi mumkin.")
        return
    sv.set_school_classes(wd, classes)
    await message.answer(f"✅ {sv.WEEKDAY_NAMES[wd]}: {', '.join(classes)}")


# ------------------------------------------------------------------ /izoh

@dp.message(Command("izoh"))
async def cmd_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Format: /izoh matn")
        return
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO notes (date, text, created_at) VALUES (?, ?, ?)",
            (now_date().isoformat(), parts[1].strip(), sv.now().isoformat()),
        )
    await message.answer("📝 Izoh saqlandi.")


# ------------------------------------------------------------------ ball qiymatlarini o'zgartirish

@dp.message(Command("ball"))
async def cmd_set_points(message: Message):
    """
    Standart eslatmaning ball qiymatini o'zgartirish.
    /ball -> ro'yxat (nom: ball)
    /ball zal 12 -> zal eslatmasi ballini 12 ga o'zgartiradi
    """
    parts = (message.text or "").split()
    if len(parts) < 3:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT slug, title, points FROM reminders "
                "WHERE source = 'standard' AND scored = 1 AND active = 1 "
                "GROUP BY slug ORDER BY slug"
            ).fetchall()
        lines = ["<b>Ballli standart eslatmalar:</b>\n"]
        for r in rows:
            lines.append(f"{r['slug']}: {r['points']} ball ({esc(r['title'])})")
        lines.append("\nO'zgartirish: <code>/ball zal 12</code>")
        await message.answer("\n".join(lines))
        return
    slug, val = parts[1].lower(), parts[2]
    if not val.isdigit():
        await message.answer("Ball musbat butun son bo'lishi kerak.")
        return
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET points = ?, scored = 1 WHERE source = 'standard' AND slug = ? AND active = 1",
            (int(val), slug),
        )
    if cur.rowcount:
        await message.answer(f"✅ {esc(slug)} → {val} ball")
    else:
        await message.answer(f"'{esc(slug)}' topilmadi. Ro'yxat uchun: /ball")


@dp.message(Command("ballqoshish"))
async def cmd_set_custom_points(message: Message):
    """Maxsus (/qoshish orqali qo'shilgan) eslatma ballini o'zgartirish: /ballqoshish 24 12"""
    parts = (message.text or "").split()
    if len(parts) != 3 or not all(x.isdigit() for x in parts[1:]):
        await message.answer("Format: <code>/ballqoshish 24 12</code> (ID, yangi ball)")
        return
    rid, val = int(parts[1]), int(parts[2])
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET points = ?, scored = 1 WHERE id = ? AND source = 'custom' AND active = 1",
            (val, rid),
        )
    if cur.rowcount:
        await message.answer(f"✅ #{rid} → {val} ball")
    else:
        await message.answer(f"#{rid} topilmadi.")


# ------------------------------------------------------------------ /statistika

@dp.message(Command("statistika"))
async def cmd_stats(message: Message):
    today = now_date()
    hist = scoring.weekly_history(4, today)
    lines = ["<b>Haftalik natijalar:</b>\n"]
    for i, h in enumerate(hist):
        label = "Bu hafta" if i == 0 else f"{h['monday'].strftime('%d.%m')} haftasi"
        lines.append(f"{label}: {h['points']} ball, {h['done']}/{h['total']} ({h['percent']}%)")
    if len(hist) >= 2:
        lines.append(f"\nO'tgan haftaga nisbatan: {scoring.format_delta(hist[0]['points'], hist[1]['points'])}")
    rec = scoring.record_week()
    if rec and rec["points"] > 0:
        lines.append(f"🏆 Rekord: {rec['points']} ball ({rec['monday'].strftime('%d.%m')} haftasi)")
    m = scoring.month_summary(today.year, today.month)
    lines.append(f"\n<b>Bu oy:</b> {m['points']} ball, {m['done']}/{m['total']} ({m['percent']}%)")
    await message.answer("\n".join(lines))


# ------------------------------------------------------------------ /start, /shahar

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Assalomu alaykum, Umarkhan!\n\n"
        "<b>RejaliBot 2.0</b>\n\n"
        "/bugun — bugungi reja\n"
        "/qoshish — yangi eslatma\n"
        "/royxat, /ochirish — maxsus eslatmalar\n"
        "/vaqt — standart eslatma vaqtini o'zgartirish\n"
        "/sinf — maktab sinflar tartibi\n"
        "/qazolar — ochiq qazolar\n"
        "/statistika — haftalik va oylik natija\n"
        "/ball — standart eslatma ballini o'zgartirish\n"
        "/ballqoshish — maxsus eslatma ballini o'zgartirish\n"
        "/izoh — izoh qoldirish\n"
        "/shahar — namoz vaqtlari shahri\n"
        "/bekor — jarayonni bekor qilish"
    )


@dp.message(Command("shahar"))
async def cmd_city(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(f"Hozirgi shahar: <b>{esc(get_city())}</b>\nO'zgartirish: /shahar Toshkent")
        return
    db.set_setting("city", parts[1].strip())
    await message.answer(f"Shahar <b>{esc(parts[1].strip())}</b> ga o'zgartirildi.")


# ------------------------------------------------------------------ ishga tushirish

async def main():
    db.init_db()
    sv.seed_standard_reminders()

    scheduler.add_job(schedule_today, CronTrigger(hour=0, minute=5), id="daily_scheduler", replace_existing=True)
    scheduler.add_job(send_morning_summary, CronTrigger(hour=6, minute=30), id="morning", replace_existing=True)
    scheduler.add_job(send_evening_report, CronTrigger(hour=21, minute=45), id="evening", replace_existing=True)
    scheduler.add_job(check_qazo, IntervalTrigger(minutes=1), id="qazo_check", replace_existing=True)
    scheduler.start()

    await schedule_today()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
