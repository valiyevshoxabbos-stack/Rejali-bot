"""Tug'ilgan kunlar: 1 hafta oldin (oddiy), 3 va 1 kun oldin (muhimroq), o'sha kuni tongi xabarda + eksport."""

import re
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_common as common
import messenger

router = Router()
LINE_RE = re.compile(r"^(.+?)\s+(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?$")
MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
          "sentabr", "oktabr", "noyabr", "dekabr"]
NOTICE_OFFSETS = (7, 3, 1)


class AddBirthday(StatesGroup):
    waiting = State()


def next_occurrence(day: int, month: int, today: date) -> date:
    """Keyingi tug'ilgan kun sanasi. 29-fevral kabisa bo'lmagan yilda 28-fevralda nishonlanadi."""
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            d = date(year, 2, 28)
        if d >= today:
            return d
    return date(today.year + 1, month, min(day, 28))


def parse_line(line: str):
    """'Ali 12.05.1998' -> (ism, kun, oy, yil|None) yoki None."""
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    name, day, month = m.group(1).strip(), int(m.group(2)), int(m.group(3))
    year = int(m.group(4)) if m.group(4) else None
    try:
        date(year or 2000, month, day)
    except ValueError:
        return None
    if year is not None and not (1900 <= year <= app.today().year):
        return None
    return name, day, month, year


def age_text(year: int | None, event: date) -> str:
    return f" ({event.year - year} yoshga to'ladi)" if year else ""


def today_lines(today: date) -> list[str]:
    out = []
    for b in db.q("SELECT * FROM birthdays"):
        if next_occurrence(b["day"], b["month"], today) == today:
            out.append(f"🎂 Bugun tug'ilgan kun: <b>{app.esc(b['name'])}</b>{age_text(b['year'], today)}")
    return out


async def notice_job():
    """Har kuni 09:00: 7, 3 va 1 kun qolgan tug'ilgan kunlar haqida bir marta xabar."""
    today = app.today()
    for b in db.q("SELECT * FROM birthdays"):
        event = next_occurrence(b["day"], b["month"], today)
        left = (event - today).days
        if left not in NOTICE_OFFSETS:
            continue
        if db.q1("SELECT 1 FROM birthday_notices WHERE birthday_id=? AND event_date=? AND offset=?",
                 (b["id"], event.isoformat(), left)):
            continue
        when = f"{event.day}-{MONTHS[event.month - 1]}"
        info = app.esc(b["name"]) + age_text(b["year"], event)
        if left == 7:
            text = f"🎂 1 haftadan keyin tug'ilgan kun: <b>{info}</b> ({when})"
        else:
            text = f"❗🔔 <b>{left} kun qoldi</b>: tug'ilgan kun, <b>{info}</b> ({when})"
        db.ex("INSERT INTO birthday_notices (birthday_id, event_date, offset) VALUES (?,?,?)",
              (b["id"], event.isoformat(), left))
        await messenger.send(text, "birthday", bind_trigger=False)


# ------------------------------------------------------------------ qo'shish, ro'yxat va eksport

async def start_add(state: FSMContext):
    await state.clear()
    await state.set_state(AddBirthday.waiting)
    await common.ask(
        state,
        "🎂 Ism va sanani yozing: <code>Ali 12.05.1998</code>\n"
        "Yil ixtiyoriy (<code>Ali 12.05</code>). Bir nechtasini har qatordan yozsangiz bo'ladi.",
    )


@router.callback_query(F.data == "add:bday")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await start_add(state)


@router.message(Command("tugilgan"))
async def cmd_add(message: Message, state: FSMContext):
    await start_add(state)


@router.message(AddBirthday.waiting)
async def process_input(message: Message, state: FSMContext):
    await common.track_user(state, message)
    if not message.text:
        await messenger.send("Matn sifatida yozing.", "confirm", bind_trigger=False)
        return
    ok, bad = [], []
    for line in message.text.splitlines():
        if not line.strip():
            continue
        parsed = parse_line(line)
        (ok if parsed else bad).append(parsed or line.strip())
    if not ok:
        await messenger.send("Tushunmadim. Format: <code>Ali 12.05.1998</code>", "confirm", bind_trigger=False)
        return
    for name, day, month, year in ok:
        db.ins("INSERT INTO birthdays (name, day, month, year) VALUES (?,?,?,?)", (name, day, month, year))
    await common.clear_prompts(state)
    await state.clear()
    text = "✅ Qo'shildi: " + ", ".join(f"{app.esc(n)} ({d:02d}.{m:02d})" for n, d, m, _ in ok)
    if bad:
        text += "\n⚠️ Tushunilmadi: " + "; ".join(app.esc(b) for b in bad)
    await messenger.send(text, "confirm", bind_trigger=False)


def export_text() -> str:
    rows = db.q("SELECT name, day, month, year FROM birthdays ORDER BY month, day, name")
    if not rows:
        return "Tug'ilgan kunlar yo'q."
    lines = [
        f"{r['name']} {r['day']:02d}.{r['month']:02d}" + (f".{r['year']}" if r["year"] else "")
        for r in rows
    ]
    return "📋 <b>Tug'ilgan kunlar (nusxalash uchun):</b>\n\n<code>" + app.esc("\n".join(lines)) + "</code>"


def list_text_kb():
    today = app.today()
    rows = []
    for b in db.q("SELECT * FROM birthdays"):
        ev = next_occurrence(b["day"], b["month"], today)
        rows.append(((ev - today).days, b, ev))
    rows.sort(key=lambda x: x[0])
    if not rows:
        return "Tug'ilgan kunlar yo'q. Qo'shish: /tugilgan", None
    lines = ["<b>Tug'ilgan kunlar:</b>\n"]
    kb = [[InlineKeyboardButton(text="📋 Matn qilib olish", callback_data="bd:export")]]
    for left, b, ev in rows:
        when = "bugun" if left == 0 else f"{left} kun qoldi"
        lines.append(f"• {app.esc(b['name'])}: {ev.day}-{MONTHS[ev.month - 1]}{age_text(b['year'], ev)} — {when}")
        kb.append([InlineKeyboardButton(text=f"🗑 {b['name']}", callback_data=f"bd:del:{b['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb[:21])


@router.message(Command("tkunlar"))
async def cmd_list(message: Message):
    text, kb = list_text_kb()
    await messenger.send(text, "info", kb)


@router.callback_query(F.data == "bd:list")
async def cb_list(cb: CallbackQuery):
    await cb.answer()
    text, kb = list_text_kb()
    await messenger.send(text, "info", kb, bind_trigger=False)


@router.callback_query(F.data == "bd:export")
async def cb_export(cb: CallbackQuery):
    await cb.answer()
    await messenger.send(export_text(), "info", bind_trigger=False)


@router.callback_query(F.data.startswith("bd:del:"))
async def cb_delete(cb: CallbackQuery):
    bid = int(cb.data.split(":")[2])
    db.ex("DELETE FROM birthdays WHERE id = ?", (bid,))
    db.ex("DELETE FROM birthday_notices WHERE birthday_id = ?", (bid,))
    await cb.answer("O'chirildi")
    text, kb = list_text_kb()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


def register_jobs(scheduler):
    scheduler.add_job(notice_job, CronTrigger(hour=9, minute=0), id="birthdays", replace_existing=True)
