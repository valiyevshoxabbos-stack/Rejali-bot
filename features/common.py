"""Oqim (FSM) uchun umumiy yordamchilar: bekor tugmasi, savol berish, savollarni tozalash."""

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import messenger


def flow_kb(rows: list | None = None) -> InlineKeyboardMarkup:
    """rows: [[(matn, callback_data), ...], ...]. Oxirida doim 'Bekor' tugmasi."""
    kb = [[InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in (rows or [])]
    kb.append([InlineKeyboardButton(text="❌ Bekor", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def ask(state: FSMContext, text: str, rows: list | None = None):
    """Savol yuboradi va xabar id sini eslab qoladi (oqim tugagach o'chiriladi)."""
    msg = await messenger.send(text, "flow", flow_kb(rows), actionable=True)
    if msg:
        data = await state.get_data()
        await state.update_data(prompts=data.get("prompts", []) + [msg.message_id])
    return msg


async def clear_prompts(state: FSMContext):
    data = await state.get_data()
    for mid in data.get("prompts", []):
        await messenger.delete_quiet(mid)


# ------------------------------------------------------------------ asosiy menyu (doimiy tugmalar)

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup  # noqa: E402

T_TODAY, T_ADD = "📅 Bugun", "➕ Qo'shish"
T_CALC, T_WEATHER = "🧮 Hisob", "🌦 Ob-havo"
T_STATS, T_SETTINGS = "📊 Statistika", "⚙️ Sozlamalar"
MENU_TEXTS = {T_TODAY, T_ADD, T_CALC, T_WEATHER, T_STATS, T_SETTINGS}


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T_TODAY), KeyboardButton(text=T_ADD)],
                  [KeyboardButton(text=T_CALC), KeyboardButton(text=T_WEATHER)],
                  [KeyboardButton(text=T_STATS), KeyboardButton(text=T_SETTINGS)]],
        resize_keyboard=True, is_persistent=True)
