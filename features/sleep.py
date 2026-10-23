"""
Uyqu ritmi (qo'lbola usul):
- "Yotish vaqti" eslatmasi tasdiqlangach, uyqu boshi = tasdiqlangan payt + kechikish (standart 20 daq).
- Bomdod vaqtida alohida "uyg'ondim" xabari: hozir / -15 / -30 / -1 soat / vaqtni yozaman.
- Yotishni tasdiqlamagan kun uchun taxmin qilinmaydi: "ma'lumot yo'q".
"""

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import messenger

router = Router()
MAX_SLEEP_MIN = 16 * 60


class WakeTime(StatesGroup):
    waiting = State()


def latency() -> int:
    return int(db.get_setting("sleep_latency", "20"))


def fmt_min(m: int | None) -> str:
    if m is None:
        return "ma'lumot yo'q"
    return f"{m // 60} s {m % 60:02d} daq"


def on_bedtime_confirmed(confirmed_at: datetime | None = None) -> datetime:
    """Yotish tasdiqlandi. Uyqu boshlanish vaqtini qaytaradi."""
    confirmed_at = confirmed_at or app.now()
    start = confirmed_at + timedelta(minutes=latency())
    db.ins("INSERT INTO sleep_log (bed_confirmed_at, sleep_start) VALUES (?,?)",
           (confirmed_at.isoformat(), start.isoformat()))
    return start


def finish_sleep(wake_at: datetime, source: str) -> dict:
    """Uyg'onish vaqtini qayd etadi. {'duration': daq|None} yoki {'error': matn}"""
    open_row = db.q1(
        "SELECT * FROM sleep_log WHERE wake_at IS NULL AND sleep_start IS NOT NULL ORDER BY id DESC")
    if open_row:
        start = datetime.fromisoformat(open_row["sleep_start"])
        minutes = int((wake_at - start).total_seconds() // 60)
        if 0 < minutes <= MAX_SLEEP_MIN:
            db.ex("UPDATE sleep_log SET wake_at=?, duration_min=?, wake_source=? WHERE id=?",
                  (wake_at.isoformat(), minutes, source, open_row["id"]))
            return {"duration": minutes, "start": start}
        if minutes <= 0:
            return {"error": "Uyg'onish vaqti uyqu boshlanishidan oldin chiqdi. Vaqtni qayta tekshiring."}
    db.ins("INSERT INTO sleep_log (wake_at, wake_source) VALUES (?,?)", (wake_at.isoformat(), source))
    return {"duration": None}


def wake_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☀️ Hozir uyg'ondim", callback_data="wk:0")],
        [InlineKeyboardButton(text="−15 daq", callback_data="wk:15"),
         InlineKeyboardButton(text="−30 daq", callback_data="wk:30"),
         InlineKeyboardButton(text="−1 soat", callback_data="wk:60")],
        [InlineKeyboardButton(text="🕐 Vaqtni yozaman", callback_data="wk:manual")],
    ])


async def send_wake_prompt():
    await messenger.send("☀️ <b>Xayrli tong!</b> Qachon uyg'ondingiz?", "wake", wake_keyboard(), actionable=True)


def _result_text(res: dict, wake_at: datetime) -> str:
    if "error" in res:
        return "⚠️ " + res["error"]
    if res["duration"] is None:
        return f"☀️ Uyg'onish: {wake_at.strftime('%H:%M')}. Kecha yotish belgilanmagan, uyqu hisoblanmadi."
    return (f"☀️ Uyg'onish: {wake_at.strftime('%H:%M')}\n😴 Uyqu: <b>{fmt_min(res['duration'])}</b> "
            f"({res['start'].strftime('%H:%M')} dan)")


@router.callback_query(F.data.startswith("wk:"))
async def cb_wake(cb: CallbackQuery, state: FSMContext):
    arg = cb.data.split(":")[1]
    if arg == "manual":
        await state.set_state(WakeTime.waiting)
        prompt = await messenger.send(
            "Uyg'ongan vaqtni yozing (masalan <code>05:40</code>):", "flow",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Bekor", callback_data="cancel")]]), actionable=True)
        await state.update_data(wake_msg=cb.message.message_id, prompts=[prompt.message_id] if prompt else [])
        await cb.answer()
        return
    wake_at = app.now() - timedelta(minutes=int(arg))
    res = finish_sleep(wake_at, "tap" if arg == "0" else f"tap-{arg}")
    if "error" in res:
        await cb.answer(res["error"], show_alert=True)
        return
    await messenger.edit_result(cb.message, _result_text(res, wake_at), "reminder_done")
    await cb.answer()


@router.message(WakeTime.waiting)
async def process_wake_time(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    try:
        h, m = map(int, t.split(":"))
        wake_at = app.now().replace(hour=h, minute=m, second=0, microsecond=0)
    except ValueError:
        await messenger.send("Format: <code>HH:MM</code> (masalan 05:40)", "confirm")
        return
    if wake_at > app.now():
        await messenger.send("Bu vaqt hali kelmagan. Uyg'ongan vaqtni yozing.", "confirm")
        return
    res = finish_sleep(wake_at, "manual")
    if "error" in res:
        await messenger.send("⚠️ " + res["error"], "confirm")
        return
    data = await state.get_data()
    await state.clear()
    for mid in data.get("prompts", []):
        await messenger.delete_quiet(mid)
    text = _result_text(res, wake_at)
    if data.get("wake_msg"):
        await messenger.edit_result_by_id(data["wake_msg"], text)
    else:
        await messenger.send(text, "reminder_done")


# ------------------------------------------------------------------ qo'lda yotish va menyu

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="😴 Yotdim (hozir)", callback_data="sl:bed"),
        InlineKeyboardButton(text="☀️ Uyg'ondim", callback_data="sl:wake"),
    ]])


@router.callback_query(F.data == "sl:menu")
async def cb_menu(cb: CallbackQuery):
    last = db.q1("SELECT * FROM sleep_log WHERE duration_min IS NOT NULL ORDER BY id DESC")
    text = "😴 <b>Uyqu</b>\n"
    text += f"Oxirgi uyqu: {fmt_min(last['duration_min'])}" if last else "Hali ma'lumot yo'q."
    await messenger.send(text, "info", menu_kb())
    await cb.answer()


@router.message(Command("uyqu"))
async def cmd_sleep(message: Message):
    await messenger.send("😴 <b>Uyqu</b>", "info", menu_kb())


@router.callback_query(F.data == "sl:bed")
async def cb_bed(cb: CallbackQuery):
    start = on_bedtime_confirmed()
    await messenger.edit_result(cb.message, f"😴 Yotish qayd etildi. Uyqu {start.strftime('%H:%M')} dan hisoblanadi.", "confirm")
    await cb.answer()


@router.callback_query(F.data == "sl:wake")
async def cb_wake_menu(cb: CallbackQuery):
    await messenger.send("☀️ Qachon uyg'ondingiz?", "wake", wake_keyboard(), actionable=True)
    await cb.answer()


# ------------------------------------------------------------------ statistika

def stats(days: int) -> dict:
    since = (app.now() - timedelta(days=days)).isoformat()
    rows = db.q("SELECT * FROM sleep_log WHERE duration_min IS NOT NULL AND wake_at >= ? ORDER BY wake_at", (since,))
    if not rows:
        return {"nights": 0}
    avg = sum(r["duration_min"] for r in rows) // len(rows)
    starts = []
    for r in rows:
        t = datetime.fromisoformat(r["sleep_start"])
        mins = t.hour * 60 + t.minute
        starts.append(mins if mins >= 12 * 60 else mins + 24 * 60)  # yarim tundan keyin: ketma-ketlik uchun
    avg_start = (sum(starts) // len(starts)) % (24 * 60)
    return {"nights": len(rows), "avg": avg, "avg_start": f"{avg_start // 60:02d}:{avg_start % 60:02d}",
            "series": [r["duration_min"] for r in rows]}
