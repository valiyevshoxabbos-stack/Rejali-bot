"""
AltronBek 4.0.1: 🤖 AI Suhbat bo'limi, Multimodal Ovozli Xabarlar,
Human-in-the-Loop Tasdiqlash Kartochkalari va ⚙️ AI Sozlamalari.
"""

import io
import traceback

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_common as common
import messenger
import feat_ai_core as ai_core
import feat_ai_router as ai_router
import feat_ai_tools as ai_tools

router = Router()


class AIChatState(StatesGroup):
    active = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _safe_edit(message, text: str, kb: InlineKeyboardMarkup | None = None, add_delete: bool = True):
    markup = messenger.with_delete(kb) if (add_delete and kb is not None) else kb
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            app.log.warning(f"ai_chat edit_text xatoligi: {e}")


def chat_controls_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🆕 Yangi suhbat", "ai:new"), btn("⚙️ AI Sozlamalari", "ai:settings")],
        [btn("🔙 Asosiy menyu", "ai:exit")],
    ])


def pending_action_kb(action_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            btn("✅ Tasdiqlayman", f"ai:ok:{action_id}"),
            btn("❌ Bekor qilish", f"ai:no:{action_id}"),
        ],
    ])


def render_ai_home_text() -> str:
    cfg = ai_router.get_routing_config()
    st = ai_router.verify_ai_client_init()
    mode_s = "🔄 Avto-rejim (Smart Fallback)" if cfg["routing_mode"] == "auto" else "⭐ Tanlangan model (Paid)"
    if st["ok"]:
        key_s = f"✅ Ulangan (<code>{app.esc(st['key_masked'])}</code>)"
    elif st["reason_code"] == "missing_key":
        key_s = "⚠️ Ulanmagan (GEMINI_API_KEY kerak)"
    else:
        key_s = f"⚠️ Izolyatsiya ({app.esc(st['reason'])})"
    return (
        "🤖 <b>AltronBek AI Suhbat (v4.0.1)</b>\n\n"
        f"• 🔑 <b>Holat:</b> {key_s}\n"
        f"• 🧭 <b>Rejim:</b> {mode_s}\n\n"
        "Matn yoki 🎤 ovozli xabar yuboring — barcha 9 modul bo'yicha savol bering yoki buyruq bering.\n"
        "<i>Eslatma: Baza va jadvalga o'zgartirish kiritishdan oldin doimo sizdan tasdiq so'raladi.</i>"
    )


def render_ai_settings() -> tuple[str, InlineKeyboardMarkup]:
    cfg = ai_router.get_routing_config()
    is_auto = cfg["routing_mode"] == "auto"
    text = "⚙️ <b>AI Sozlamalari</b>\n" + ai_router.format_ai_diagnostics_block()
    rows = [
        [btn(f"{'✅ ' if is_auto else ''}🔄 Avto-rejim (Free / Smart Fallback)", "ai:mode:auto")],
        [btn(f"{'✅ ' if not is_auto else ''}⭐ Tanlangan asosiy model (Paid)", "ai:mode:fixed")],
        [
            btn(f"⚡ Light: {cfg['preferred_light_model']}", "ai:pick:light"),
            btn(f"🧠 Heavy: {cfg['preferred_heavy_model']}", "ai:pick:heavy"),
        ],
        [btn("♻️ Cooldownlarni tozalash", "ai:clearcd")],
        [btn("🤖 AI Suhbat", "ai:home"), btn("⬅️ Sozlamalar", "se:home")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def entry(message: Message, state: FSMContext | None = None):
    """Asosiy menyudagi `🤖 AI Suhbat` tugmasi kirish nuqtasi."""
    if state is not None:
        await state.set_state(AIChatState.active)
    await messenger.send(render_ai_home_text(), "info", chat_controls_kb())


@router.message(Command("ai"))
async def cmd_ai(message: Message, state: FSMContext):
    await state.set_state(AIChatState.active)
    await messenger.send(render_ai_home_text(), "info", chat_controls_kb())


@router.callback_query(F.data == "ai:home")
async def cb_ai_home(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AIChatState.active)
    await cb.answer()
    await _safe_edit(cb.message, render_ai_home_text(), chat_controls_kb())


@router.callback_query(F.data == "ai:new")
async def cb_ai_new(cb: CallbackQuery, state: FSMContext):
    ai_core.start_new_session()
    await state.set_state(AIChatState.active)
    await cb.answer("🆕 Yangi suhbat boshlandi!")
    await _safe_edit(
        cb.message,
        "🆕 <b>Yangi AI suhbat boshlandi.</b>\n\n" + render_ai_home_text(),
        chat_controls_kb(),
    )


@router.callback_query(F.data == "ai:exit")
async def cb_ai_exit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("Asosiy menyu")
    await _safe_edit(cb.message, "✅ AI Suhbat rejimidan chiqildi. Asosiy menyudan foydalanishingiz mumkin.", None)


@router.callback_query(F.data == "ai:settings")
async def cb_ai_settings(cb: CallbackQuery):
    await cb.answer()
    text, kb = render_ai_settings()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("ai:mode:"))
async def cb_ai_mode(cb: CallbackQuery):
    mode = cb.data.split(":")[2]
    ai_router.set_routing_mode(mode)
    await cb.answer("Rejim saqlandi!")
    text, kb = render_ai_settings()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("ai:pick:"))
async def cb_ai_pick_tier(cb: CallbackQuery):
    tier = cb.data.split(":")[2]
    await cb.answer()
    lbl = "🧠 Heavy (Murakkab tahlil)" if tier == "heavy" else "⚡ Light (Tezkor suhbat)"
    rows = [[btn(m, f"ai:setm:{tier}:{m}")] for m in ai_router.ALL_MODELS]
    rows.append([btn("⬅️ AI Sozlamalari", "ai:settings")])
    await _safe_edit(
        cb.message,
        f"<b>{lbl}</b> uchun asosiy modelni tanlang:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ai:setm:"))
async def cb_ai_set_model(cb: CallbackQuery):
    parts = cb.data.split(":", 3)
    tier, model_name = parts[2], parts[3]
    ai_router.set_preferred_model(tier, model_name)
    await cb.answer(f"Tanlandi: {model_name}")
    text, kb = render_ai_settings()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data == "ai:clearcd")
async def cb_ai_clear_cooldown(cb: CallbackQuery):
    ai_router.clear_all_cooldowns()
    await cb.answer("Cooldownlar tozalandi!")
    text, kb = render_ai_settings()
    await _safe_edit(cb.message, text, kb)


# ------------------------------------------------------------------ Human-in-the-Loop Confirmation Callbacks

@router.callback_query(F.data.startswith("ai:ok:"))
async def cb_ai_confirm_action(cb: CallbackQuery):
    action_id = cb.data.split(":")[2]
    res = await ai_tools.execute_pending_action(action_id)
    if res.get("ok"):
        await cb.answer("✅ Amal bajarildi!", show_alert=False)
        msg_txt = "✅ <b>Tasdiqlandi va bajarildi:</b>\n\n" + app.esc(res.get("summary") or "Muvaffaqiyatli.")
    else:
        await cb.answer("Xatolik yuz berdi", show_alert=True)
        err_body = res.get("summary") or res.get("error") or "Noma'lum xatolik"
        msg_txt = "⚠️ <b>Amalni bajarishda muammo:</b>\n\n" + app.esc(err_body)
    await _safe_edit(cb.message, msg_txt, chat_controls_kb())


@router.callback_query(F.data.startswith("ai:no:"))
async def cb_ai_cancel_action(cb: CallbackQuery):
    action_id = cb.data.split(":")[2]
    cancelled = ai_tools.cancel_pending_action(action_id)
    if cancelled:
        await cb.answer("❌ Bekor qilindi")
        await _safe_edit(cb.message, "❌ <b>Amal bekor qilindi.</b> Baza va jadvallarga hech qanday o'zgartirish kiritilmadi.", chat_controls_kb())
    else:
        await cb.answer("Bu amal allaqachon yakunlangan.", show_alert=True)


# ------------------------------------------------------------------ Turn Execution Helper

async def _send_ai_result(res: dict):
    pending = res.get("pending_action")
    if pending:
        aid = pending["pending_action_id"]
        summary_s = pending.get("summary_text") or res.get("reply_text") or ""
        card = (
            "📝 <b>Tasdiqlash kerak (AltronBek AI):</b>\n\n"
            f"{app.esc(summary_s)}\n\n"
            "<i>Tasdiqlasangizgina baza/jadvalga yoziladi:</i>"
        )
        await messenger.send(card, "info", pending_action_kb(aid), actionable=True, bind_trigger=False)
        return

    reply = res.get("reply_text") or "Javob olinmadi."
    # HTML xavfsizligi: agar model HTML teg bermagan bo'lsa ham xavfsiz yuborish
    model_footer = ""
    if res.get("ok") and res.get("model_used"):
        model_footer = f"\n\n<i>🤖 {app.esc(res['model_used'])} · {int(round(res.get('latency_ms', 0)))} ms</i>"
    sent = await messenger.send(reply + model_footer, "info", chat_controls_kb(), bind_trigger=False)
    if sent is None:
        # Agar HTML parse xatosi bo'lsa, escape qilib qayta yuboramiz
        await messenger.send(app.esc(reply) + model_footer, "info", chat_controls_kb(), bind_trigger=False)


# ------------------------------------------------------------------ Text & Voice Handlers

@router.message(
    AIChatState.active,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.startswith("!"),
    ~F.text.in_(common.MENU_TEXTS),
)
async def msg_ai_chat_text(message: Message, state: FSMContext):
    res = await ai_core.process_user_turn(user_text=message.text)
    await _send_ai_result(res)


async def _download_voice_bytes(message: Message) -> tuple[bytes | None, str]:
    voice = getattr(message, "voice", None)
    if not voice:
        return None, "audio/ogg"
    mime = getattr(voice, "mime_type", None) or "audio/ogg"
    try:
        tg_file = await app.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await app.bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue(), mime
    except Exception as exc:
        db.log_error("AIVoiceDownloadError", str(exc), traceback.format_exc(), "features.ai_chat")
        app.log.error(f"Ovozli xabarni yuklashda xatolik: {exc}")
        return None, mime


@router.message(StateFilter(AIChatState.active, default_state), F.voice)
async def msg_ai_voice(message: Message, state: FSMContext):
    audio_bytes, mime = await _download_voice_bytes(message)
    if not audio_bytes:
        await messenger.send("⚠️ Ovozli xabarni yuklab bo'lmadi. Iltimos, qayta yuboring yoki matn yozing.", "info", bind_trigger=False)
        return
    res = await ai_core.process_user_turn(
        user_text=message.caption or None,
        audio_bytes=audio_bytes,
        audio_mime=mime,
    )
    await _send_ai_result(res)


def register_jobs(scheduler):
    """Har kuni 00:10 da AI Startup Snapshot (Tizim Pasporti) ni yangilaydi."""
    scheduler.add_job(
        ai_core.refresh_startup_snapshot,
        CronTrigger(hour=0, minute=10),
        id="ai_daily_passport_refresh",
        replace_existing=True,
    )
