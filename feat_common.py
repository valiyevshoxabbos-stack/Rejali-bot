"""Oqim (FSM) uchun umumiy yordamchilar: bekor tugmasi, savol berish, savollar va javoblarni tozalash."""

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

import messenger


def flow_kb(rows: list | None = None) -> InlineKeyboardMarkup:
    """rows: [[(matn, callback_data), ...], ...]. Oxirida doim 'Bekor' tugmasi."""
    kb = [[InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in (rows or [])]
    kb.append([InlineKeyboardButton(text="❌ Bekor", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def track_user(state: FSMContext, message):
    """FSM oqimida foydalanuvchi yuborgan matnli xabarni keyinchalik tozalash uchun eslab qoladi."""
    if message is None or getattr(message, "document", None) is not None:
        return
    mid = getattr(message, "message_id", None)
    if mid is None:
        return
    data = await state.get_data()
    umids = data.get("user_prompts", [])
    if mid not in umids:
        await state.update_data(user_prompts=umids + [mid])


async def ask(state: FSMContext, text: str, rows: list | None = None):
    """Savol yuboradi va xabar id sini eslab qoladi (oqim tugagach o'chiriladi)."""
    trig = messenger.current_trigger.get()
    if trig is not None:
        data = await state.get_data()
        umids = data.get("user_prompts", [])
        if trig not in umids:
            await state.update_data(user_prompts=umids + [trig])
        messenger.current_trigger.set(None)
    msg = await messenger.send(text, "flow", flow_kb(rows), actionable=True, bind_trigger=False)
    if msg:
        data = await state.get_data()
        await state.update_data(prompts=data.get("prompts", []) + [msg.message_id])
    return msg


async def clear_prompts(state: FSMContext):
    data = await state.get_data()
    for mid in data.get("prompts", []):
        await messenger.delete_quiet(mid)
    for umid in data.get("user_prompts", []):
        await messenger.delete_user_quiet(umid)


# ------------------------------------------------------------------ asosiy menyu (doimiy tugmalar)

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
