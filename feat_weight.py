"""
Vazn kuzatuvi.
- Istalgan vaqtda: /vazn 72,4 (yoki "Qo'shish" menyusi).
- Har dushanba 11:00 da avtomatik so'rov: shu hafta "asosiy o'lchov" bo'ladi (trend shundan chiziladi).
- Vergulli kasr tushuniladi; oldingisidan 3 kg dan ko'p farq bo'lsa tasdiq so'raladi.
"""

import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import messenger

router = Router()
NUM_RE = re.compile(r"^\s*(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:kg)?\s*$", re.IGNORECASE)
MIN_KG, MAX_KG, JUMP_KG = 30.0, 250.0, 3.0
PROMPT_HOURS = 36


def parse_kg(text: str) -> float | None:
    m = NUM_RE.match(text or "")
    if not m:
        return None
    kg = float(m.group(1).replace(",", "."))
    return kg if MIN_KG <= kg <= MAX_KG else None


def open_prompt(primary: bool, hours: int = PROMPT_HOURS):
    db.set_setting("weight_prompt_until", (app.now() + timedelta(hours=hours)).isoformat())
    db.set_setting("weight_prompt_primary", "1" if primary else "0")


def prompt_is_open() -> bool:
    raw = db.get_setting("weight_prompt_until")
    return bool(raw) and app.now().isoformat() <= raw


def last_weight() -> dict | None:
    return db.q1("SELECT * FROM weight_log ORDER BY at DESC, id DESC")


def save(kg: float, primary: bool):
    db.ins("INSERT INTO weight_log (at, kg, is_primary) VALUES (?,?,?)", (app.now().isoformat(), kg, int(primary)))
    db.del_setting("weight_prompt_until")
    db.del_setting("weight_pending")


def _delta_line(kg: float, prev: dict | None) -> str:
    if not prev:
        return ""
    diff = round(kg - prev["kg"], 1)
    sign = "+" if diff > 0 else ""
    return f"\nOldingisidan: {sign}{diff} kg"


async def handle_value(kg: float):
    primary = db.get_setting("weight_prompt_primary", "0") == "1"
    prev = last_weight()
    if prev and abs(kg - prev["kg"]) > JUMP_KG:
        db.set_setting("weight_pending", f"{kg}|{int(primary)}")
        await messenger.send(
            f"⚖️ {kg} kg. Oldingisi {prev['kg']} kg edi, farq katta. To'g'rimi?", "flow",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ To'g'ri", callback_data="wt:ok"),
                InlineKeyboardButton(text="✏️ Qayta yozaman", callback_data="wt:redo")]]),
            actionable=True,
        )
        return
    save(kg, primary)
    tag = " (haftalik asosiy o'lchov)" if primary else ""
    await messenger.send(f"⚖️ Saqlandi: <b>{kg} kg</b>{tag}{_delta_line(kg, prev)}", "confirm")


@router.message(Command("vazn"))
async def cmd_weight(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        kg = parse_kg(parts[1])
        if kg is None:
            await messenger.send("Vazn 30–250 kg oralig'ida bo'lishi kerak. Masalan: <code>/vazn 72,4</code>", "confirm")
            return
        open_prompt(primary=False, hours=1)
        await handle_value(kg)
        return
    open_prompt(primary=False)
    await messenger.send("⚖️ Vaznni yozing (masalan <code>72,4</code>):", "confirm")


@router.callback_query(F.data == "add:weight")
async def cb_add(cb: CallbackQuery):
    open_prompt(primary=False)
    await cb.answer()
    await messenger.send("⚖️ Vaznni yozing (masalan <code>72,4</code>):", "confirm", bind_trigger=False)


@router.message(StateFilter(default_state), F.text.regexp(NUM_RE))
async def numeric_reply(message: Message):
    """Faqat vazn so'rovi ochiq bo'lganda (ochiq bo'lmasa jim)."""
    if not prompt_is_open():
        return
    kg = parse_kg(message.text)
    if kg is None:
        await messenger.send("Vazn 30–250 kg oralig'ida bo'lishi kerak.", "confirm")
        return
    await handle_value(kg)


@router.callback_query(F.data == "wt:ok")
async def cb_ok(cb: CallbackQuery):
    raw = db.get_setting("weight_pending")
    if not raw:
        await cb.answer("Eskirgan")
        return
    kg_s, primary = raw.split("|")
    prev = last_weight()
    save(float(kg_s), primary == "1")
    await cb.answer()
    await messenger.edit_result(cb.message, f"⚖️ Saqlandi: <b>{kg_s} kg</b>{_delta_line(float(kg_s), prev)}", "confirm")


@router.callback_query(F.data == "wt:redo")
async def cb_redo(cb: CallbackQuery):
    db.del_setting("weight_pending")
    await cb.answer()
    await messenger.edit_result(cb.message, "✏️ Vaznni qayta yozing.", "confirm")


# ------------------------------------------------------------------ haftalik so'rov va statistika

def weekly_enabled() -> bool:
    return db.get_setting("weight_weekly", "1") == "1"


async def weekly_prompt():
    if not weekly_enabled():
        return
    open_prompt(primary=True)
    await messenger.send("⚖️ <b>Haftalik o'lchov.</b> Vaznni yozing (masalan <code>72,4</code>):",
                         "weight_prompt", actionable=True, bind_trigger=False)


def stats(weeks: int = 8) -> dict:
    """Haftalik asosiy o'lchovlar (yo'q bo'lsa o'sha haftaning o'rtachasi)."""
    since = (app.now() - timedelta(weeks=weeks)).isoformat()
    rows = db.q("SELECT * FROM weight_log WHERE at >= ? ORDER BY at", (since,))
    weeks_map: dict = {}
    for r in rows:
        d = r["at"][:10]
        monday = (app.today().fromisoformat(d) - timedelta(days=app.today().fromisoformat(d).weekday())).isoformat()
        weeks_map.setdefault(monday, []).append(r)
    series = []
    for monday in sorted(weeks_map):
        items = weeks_map[monday]
        primary = [i for i in items if i["is_primary"]]
        pick = primary or items
        series.append((monday, round(sum(i["kg"] for i in pick) / len(pick), 1), bool(primary), len(items)))
    return {"series": series, "total": len(rows)}


def register_jobs(scheduler):
    scheduler.add_job(weekly_prompt, CronTrigger(day_of_week="mon", hour=11, minute=0),
                      id="weight_weekly", replace_existing=True)
