"""Asosiy menyu (doimiy tugmalar), /start, /menu, /bekor va umumiy 'Bekor' tugmasi."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import messenger
from features import common, settings, stats, summary

router = Router()

HELP = (
    "Assalomu alaykum, Umarkhan!\n\n<b>AltronBek " + app.VERSION + "</b>\n"
    "Pastdagi tugmalar orqali ishlang. Buyruqlar ham ishlaydi:\n"
    "/bugun /qoshish /royxat /vaqt /ball /sinf /qazolar /statistika /vazn /uyqu /tkunlar /tugilgan "
    "/izoh /shahar /sozlamalar /holat /bekor"
)


def add_menu_kb() -> InlineKeyboardMarkup:
    def b(t, d):
        return InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("📝 Eslatma", "add:rem"), b("🎂 Tug'ilgan kun", "add:bday")],
        [b("⚖️ Vazn", "add:weight"), b("📝 Izoh", "add:note")],
        [b("➕ Bajardim", "ql:list"), b("😴 Uyqu", "sl:menu")],
        [b("🎂 Ro'yxat", "bd:list")],
    ])


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await messenger.send(HELP, "system", common.main_kb(), deletable=False)


async def cancel_flow(state: FSMContext):
    await common.clear_prompts(state)
    await state.clear()


@router.message(Command("bekor"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await messenger.send("Bekor qilinadigan jarayon yo'q.", "confirm")
        return
    await cancel_flow(state)
    await messenger.send("Bekor qilindi.", "confirm")


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await cancel_flow(state)
    await messenger.delete_quiet(cb.message.message_id)
    await cb.answer("Bekor qilindi")


# ------------------------------------------------------------------ doimiy tugmalar (har qanday holatdan oqimni to'xtatadi)

@router.message(F.text == common.T_TODAY)
async def btn_today(message: Message, state: FSMContext):
    await cancel_flow(state)
    await summary.show_today(message)


@router.message(F.text == common.T_ADD)
async def btn_add(message: Message, state: FSMContext):
    await cancel_flow(state)
    await messenger.send("➕ <b>Nimani qo'shamiz?</b>", "info", add_menu_kb())


@router.message(F.text == common.T_STATS)
async def btn_stats(message: Message, state: FSMContext):
    await cancel_flow(state)
    await messenger.send("📊 <b>Statistika</b>: bo'limni tanlang", "info", stats.menu_kb())


@router.message(F.text == common.T_SETTINGS)
async def btn_settings(message: Message, state: FSMContext):
    await cancel_flow(state)
    await settings.send_home()


async def _optional_entry(message: Message, state: FSMContext, name: str, label: str, version: str):
    await cancel_flow(state)
    mod = app.OPTIONAL.get(name)
    if mod is None:
        await messenger.send(f"ℹ️ «{label}» keyingi versiyada ({version}) qo'shiladi.", "info")
        return
    await mod.entry(message)


@router.message(F.text == common.T_CALC)
async def btn_calc(message: Message, state: FSMContext):
    await _optional_entry(message, state, "calculator", "Hisob (kalkulyator)", "3.2")


@router.message(F.text == common.T_WEATHER)
async def btn_weather(message: Message, state: FSMContext):
    await _optional_entry(message, state, "weather", "Ob-havo", "3.1")
