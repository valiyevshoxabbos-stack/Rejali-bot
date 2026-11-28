"""
AltronBek 4.0.1: 🤖 AI Suhbat bo'limi, Multimodal Ovozli Xabarlar,
Human-in-the-Loop Tasdiqlash Kartochkalari va ⚙️ AI Sozlamalari.
"""

import asyncio
import io
import traceback
import uuid

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
        "Matn, 🎤 ovozli xabar yoki 🖼 rasm/chek (OCR) yuboring — barcha 9 modul bo'yicha savol bering yoki buyruq bering.\n"
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
    allowed_models = ai_router.filter_allowed_models(ai_router.ALL_MODELS)
    rows = [[btn(m, f"ai:setm:{tier}:{m}")] for m in allowed_models]
    rows.append([btn("⬅️ AI Sozlamalari", "ai:settings")])
    await _safe_edit(
        cb.message,
        f"<b>{lbl}</b> uchun asosiy modelni tanlang (faqat Gemini &gt; 3):",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("ai:setm:"))
async def cb_ai_set_model(cb: CallbackQuery):
    parts = cb.data.split(":", 3)
    tier, model_name = parts[2], parts[3]
    if not ai_router.set_preferred_model(tier, model_name):
        await cb.answer("Faqat 3-avloddan katta (> 3) Gemini modellari ruxsat etiladi!", show_alert=True)
        return
    await cb.answer(f"Tanlandi: {model_name}")
    text, kb = render_ai_settings()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data == "ai:clearcd")
async def cb_ai_clear_cooldown(cb: CallbackQuery):
    ai_router.refresh_models(clear_cooldowns=True)
    await cb.answer("Cooldownlar tozalandi va > 3 modellar yangilandi!")
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
    if res.get("has_health_action") and "103" not in msg_txt:
        msg_txt += ai_tools.MEDICAL_DISCLAIMER
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


# ------------------------------------------------------------------ Progress Animation & Turn Execution Helper

PROGRESS_INTERVAL_SEC = 0.75
_PROGRESS_ICONS = ("⏳", "🔄", "✨")
_PROGRESS_DOTS = (".", "..", "...")

_STAGE_LABELS: dict[str, str] = {
    "voice": "🎤 Ovozli xabar yuklanmoqda",
    "ocr": "🖼 Rasm / chek tahlil qilinmoqda",
    "thinking": "🤖 AI o'ylamoqda",
    "finalizing": "🧠 Javob shakllantirilmoqda",
    "tool:get_finance_summary": "💰 Moliya ma'lumotlari tekshirilmoqda",
    "tool:get_market_info": "🛒 Bozorlik ma'lumotlari tekshirilmoqda",
    "tool:get_school_schedule": "🏫 Maktab jadvali tekshirilmoqda",
    "tool:get_uni_schedule": "🏛 Universitet jadvali tekshirilmoqda",
    "tool:get_health_status": "💊 Salomatlik ma'lumotlari tekshirilmoqda",
    "tool:get_challenges_status": "🏆 Chelenjlar tekshirilmoqda",
    "tool:get_daily_status": "📋 Kunlik holat tekshirilmoqda",
    "tool:get_schedules_and_reminders": "⏰ Jadvallar va eslatmalar tekshirilmoqda",
    "tool:propose_actions": "📝 Amal tekshirilmoqda",
}


def _stage_label(stage: str) -> str:
    if stage in _STAGE_LABELS:
        return _STAGE_LABELS[stage]
    if stage.startswith("tool:"):
        return "⚙️ Asbob bajarilmoqda"
    return "🤖 AI ishlamoqda"


class _AIProgressAnimator:
    """
    AI so'rovini qayta ishlash va tool chaqiruvlari davomida bitta xabarni `edit_text`
    orqali yangilab turuvchi jarayon animatsiyasi.
    `"message is not modified"` xatosi e'tiborsiz qoldiriladi.
    """

    def __init__(self, interval_sec: float | None = None):
        self.interval_sec = PROGRESS_INTERVAL_SEC if interval_sec is None else float(interval_sec)
        self.message: Message | None = None
        self._stage = "thinking"
        self._frame = 0
        self._last_text = ""
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _format_frame(self) -> str:
        icon = _PROGRESS_ICONS[self._frame % len(_PROGRESS_ICONS)]
        dots = _PROGRESS_DOTS[self._frame % len(_PROGRESS_DOTS)]
        label = _stage_label(self._stage)
        return f"{icon} <i>{label}{dots}</i>"

    async def start(self, initial_stage: str = "thinking"):
        self._stage = initial_stage
        self._frame = 0
        self._stopped = False
        initial_text = self._format_frame()
        self._last_text = initial_text
        self.message = await messenger.send(
            initial_text,
            "info",
            kb=None,
            actionable=False,
            deletable=False,
            bind_trigger=False,
        )
        if self.message is not None and self.interval_sec > 0:
            self._task = asyncio.create_task(self._tick_loop())

    async def _do_edit_text(self, text: str, reply_markup: InlineKeyboardMarkup | None = None):
        if self.message is None:
            return
        if getattr(self.message, "bot", None) is not None:
            await self.message.edit_text(text, reply_markup=reply_markup)
        else:
            await app.bot.edit_message_text(
                text,
                chat_id=app.CHAT_ID,
                message_id=self.message.message_id,
                reply_markup=reply_markup,
            )

    async def _edit_progress(self, text: str):
        if self.message is None or self._stopped:
            return
        try:
            await self._do_edit_text(text)
            self._last_text = text
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            app.log.debug(f"AI progress edit_text e'tiborsiz qoldirildi: {exc}")
        except Exception as exc:
            app.log.debug(f"AI progress edit xatoligi: {exc}")

    async def _tick_loop(self):
        try:
            while not self._stopped:
                await asyncio.sleep(self.interval_sec)
                async with self._lock:
                    if self._stopped or self.message is None:
                        return
                    self._frame += 1
                    await self._edit_progress(self._format_frame())
        except asyncio.CancelledError:
            return

    async def on_progress(self, stage: str):
        async with self._lock:
            if self._stopped or self.message is None:
                return
            self._stage = stage
            self._frame += 1
            await self._edit_progress(self._format_frame())

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None
        async with self._lock:
            pass

    async def replace_with_final(
        self,
        text: str,
        kb: InlineKeyboardMarkup | None,
        *,
        actionable: bool = False,
        fallback_text: str | None = None,
    ):
        await self.stop()
        markup = kb if actionable else messenger.with_delete(kb)
        if self.message is not None:
            final_saved = text
            try:
                await self._do_edit_text(text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    if fallback_text is not None:
                        try:
                            await self._do_edit_text(fallback_text, reply_markup=markup)
                            final_saved = fallback_text
                        except TelegramBadRequest as exc2:
                            if "message is not modified" not in str(exc2).lower():
                                await messenger.send(fallback_text, "info", kb, actionable=actionable, bind_trigger=False)
                                return
                        except Exception:
                            await messenger.send(fallback_text, "info", kb, actionable=actionable, bind_trigger=False)
                            return
                    else:
                        await messenger.send(text, "info", kb, actionable=actionable, bind_trigger=False)
                        return
            except Exception:
                target = fallback_text if fallback_text is not None else text
                await messenger.send(target, "info", kb, actionable=actionable, bind_trigger=False)
                return

            try:
                db.ex(
                    "UPDATE msg_log SET category = 'info', actionable = ?, text = ? "
                    "WHERE message_id = ? AND deleted_at IS NULL",
                    (int(actionable), final_saved, self.message.message_id),
                )
            except Exception:
                pass
            return

        sent = await messenger.send(text, "info", kb, actionable=actionable, bind_trigger=False)
        if sent is None and fallback_text is not None:
            await messenger.send(fallback_text, "info", kb, actionable=actionable, bind_trigger=False)


_OCR_RECEIPT_CACHE: dict[str, dict] = {}
_OCR_CACHE_LIMIT = 30


def _store_ocr_receipt(receipt: dict) -> str:
    ocr_id = uuid.uuid4().hex[:8]
    _OCR_RECEIPT_CACHE[ocr_id] = receipt
    while len(_OCR_RECEIPT_CACHE) > _OCR_CACHE_LIMIT:
        oldest_key = next(iter(_OCR_RECEIPT_CACHE))
        _OCR_RECEIPT_CACHE.pop(oldest_key, None)
    return ocr_id


def ocr_receipt_actions_kb(ocr_id: str, receipt: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if int(receipt.get("total_amount") or 0) > 0:
        rows.append([btn("💸 Moliya chiqimi sifatida yozish", f"ai:ocr:fin:{ocr_id}")])
    rows.append([btn("🛒 Bozorlikka qo'shish", f"ai:ocr:mkt:{ocr_id}")])
    if receipt.get("is_pharmacy"):
        rows.append([btn("💊 Dorixonaga qo'shish", f"ai:ocr:hl:{ocr_id}")])
    rows.append([btn("🆕 Yangi suhbat", "ai:new"), btn("⚙️ AI Sozlamalari", "ai:settings")])
    rows.append([btn("🔙 Asosiy menyu", "ai:exit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _active_accounts_for_picker() -> list[dict]:
    return db.q("SELECT id, name, last_4, type FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")


def _account_btn_label(acc: dict) -> str:
    icon = "💵" if (acc.get("type") or "") == "cash" else "💳"
    last4 = f" *{acc['last_4']}" if acc.get("last_4") else ""
    return f"{icon} {acc['name']}{last4}"


def _format_confirmation_card(pending: dict, has_health: bool = False) -> str:
    summary_s = pending.get("summary_text") or ""
    card = (
        "📝 <b>Tasdiqlash kerak (AltronBek AI):</b>\n\n"
        f"{app.esc(summary_s)}\n\n"
        "<i>Tasdiqlasangizgina baza/jadvalga yoziladi:</i>"
    )
    if (has_health or pending.get("has_health_action")) and "103" not in card:
        card += ai_tools.MEDICAL_DISCLAIMER
    return card


@router.callback_query(F.data.startswith("ai:ocr:fin:"))
async def cb_ocr_pick_finance_account(cb: CallbackQuery):
    ocr_id = cb.data.split(":")[3]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan. Rasmni qayta yuboring.", show_alert=True)
        return
    accs = _active_accounts_for_picker()
    if not accs:
        await cb.answer("Faol moliya hisobi topilmadi.", show_alert=True)
        return
    await cb.answer()
    amt_s = f"{int(rec.get('total_amount') or 0):,}".replace(",", " ")
    rows = [[btn(_account_btn_label(a), f"ai:ocr:finacc:{ocr_id}:{a['id']}")] for a in accs]
    rows.append([btn("❌ Bekor qilish", "ai:home")])
    await _safe_edit(
        cb.message,
        f"💸 <b>Chek summasi ({amt_s} so'm) qaysi hisobdan yechilsin?</b>\n"
        "<i>Hisobni tanlaganingizdan so'ng yakuniy tasdiq so'raladi:</i>",
        InlineKeyboardMarkup(inline_keyboard=rows),
        add_delete=False,
    )


@router.callback_query(F.data.startswith("ai:ocr:finacc:"))
async def cb_ocr_propose_finance(cb: CallbackQuery):
    parts = cb.data.split(":")
    ocr_id, acc_id = parts[3], parts[4]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan.", show_alert=True)
        return
    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ? AND is_active = 1", (int(acc_id),))
    if not acc:
        await cb.answer("Hisob topilmadi.", show_alert=True)
        return
    amt = int(rec.get("total_amount") or 0)
    cat = "💊 Sport / Sog'liq" if rec.get("is_pharmacy") else "🛒 Oziq-ovqat/Bozorlik"
    store = rec.get("store_name") or ("Dorixona cheki" if rec.get("is_pharmacy") else "Chek (OCR)")
    items_txt = ", ".join(it["item_name"] for it in (rec.get("items") or [])[:5])
    note = f"{store}: {items_txt}" if items_txt else store
    amt_s = f"{amt:,}".replace(",", " ")
    summary = f"{acc['name']} hisobidan {amt_s} so'm chiqim ({cat} — {note})"
    prop = ai_tools.propose_actions(
        summary,
        [{
            "type": "finance_expense",
            "params": {
                "amount": amt,
                "account_query": str(acc["id"]),
                "category": cat,
                "note": note,
            },
        }],
    )
    if "pending_action_id" not in prop:
        await cb.answer(prop.get("error", "Xatolik"), show_alert=True)
        return
    await cb.answer()
    card = _format_confirmation_card(prop, has_health=bool(rec.get("is_pharmacy")))
    await _safe_edit(cb.message, card, pending_action_kb(prop["pending_action_id"]), add_delete=False)


@router.callback_query(F.data.startswith("ai:ocr:mkt:"))
async def cb_ocr_market_mode(cb: CallbackQuery):
    ocr_id = cb.data.split(":")[3]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan.", show_alert=True)
        return
    await cb.answer()
    rows = [[btn("📝 Rejalashtirilgan bozorlikka qo'shish", f"ai:ocr:mktplan:{ocr_id}")]]
    accs = _active_accounts_for_picker()
    if int(rec.get("total_amount") or 0) > 0 and accs:
        for a in accs:
            rows.append([btn(f"🏁 Xaridni yakunlash ({_account_btn_label(a)})", f"ai:ocr:mktacc:{ocr_id}:{a['id']}")])
    rows.append([btn("❌ Bekor qilish", "ai:home")])
    await _safe_edit(
        cb.message,
        "🛒 <b>Chekdagi mahsulotlarni bozorlikka qanday yozaylik?</b>\n"
        "<i>Xaridni yakunlash uchun aniq hisobni tanlang:</i>",
        InlineKeyboardMarkup(inline_keyboard=rows),
        add_delete=False,
    )


@router.callback_query(F.data.startswith("ai:ocr:mktplan:"))
async def cb_ocr_propose_market_planned(cb: CallbackQuery):
    ocr_id = cb.data.split(":")[3]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan.", show_alert=True)
        return
    items = rec.get("items") or [{"item_name": rec.get("store_name") or "Chek xaridi", "quantity": 1, "unit": "dona"}]
    actions = [
        {
            "type": "market_add_planned",
            "params": {
                "item_name": it["item_name"],
                "quantity": it.get("quantity", 1),
                "unit": it.get("unit", "dona"),
                "location": rec.get("store_name") or None,
            },
        }
        for it in items
    ]
    names_s = ", ".join(it["item_name"] for it in items)
    prop = ai_tools.propose_actions(f"Rejalashtirilgan bozorlikka qo'shish: {names_s}", actions)
    if "pending_action_id" not in prop:
        await cb.answer(prop.get("error", "Xatolik"), show_alert=True)
        return
    await cb.answer()
    await _safe_edit(cb.message, _format_confirmation_card(prop), pending_action_kb(prop["pending_action_id"]), add_delete=False)


@router.callback_query(F.data.startswith("ai:ocr:mktacc:"))
async def cb_ocr_propose_market_checkout(cb: CallbackQuery):
    parts = cb.data.split(":")
    ocr_id, acc_id = parts[3], parts[4]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan.", show_alert=True)
        return
    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ? AND is_active = 1", (int(acc_id),))
    if not acc:
        await cb.answer("Hisob topilmadi.", show_alert=True)
        return
    items = rec.get("items") or [{
        "item_name": rec.get("store_name") or "Chek xaridi",
        "quantity": 1,
        "unit": "dona",
        "total_price": int(rec.get("total_amount") or 0),
    }]
    amt_s = f"{int(rec.get('total_amount') or 0):,}".replace(",", " ")
    names_s = ", ".join(it["item_name"] for it in items)
    prop = ai_tools.propose_actions(
        f"Bozorlik xaridini yakunlash ({acc['name']}, {amt_s} so'm): {names_s}",
        [{
            "type": "market_checkout",
            "params": {
                "account_query": str(acc["id"]),
                "location": rec.get("store_name") or None,
                "items": items,
            },
        }],
    )
    if "pending_action_id" not in prop:
        await cb.answer(prop.get("error", "Xatolik"), show_alert=True)
        return
    await cb.answer()
    await _safe_edit(cb.message, _format_confirmation_card(prop), pending_action_kb(prop["pending_action_id"]), add_delete=False)


@router.callback_query(F.data.startswith("ai:ocr:hl:"))
async def cb_ocr_propose_health(cb: CallbackQuery):
    ocr_id = cb.data.split(":")[3]
    rec = _OCR_RECEIPT_CACHE.get(ocr_id)
    if not rec:
        await cb.answer("Chek ma'lumoti muddati tugagan.", show_alert=True)
        return
    items = rec.get("items") or [{"item_name": rec.get("store_name") or "Dorixona vositasi", "quantity": 1, "unit": "dona"}]
    actions = [
        {
            "type": "health_add_item",
            "params": {
                "name": it["item_name"],
                "quantity": it.get("quantity", 1),
                "unit": it.get("unit", "dona"),
                "location": "home",
            },
        }
        for it in items
    ]
    names_s = ", ".join(it["item_name"] for it in items)
    prop = ai_tools.propose_actions(f"Dorixonaga (aptechkaga) qo'shish: {names_s}", actions)
    if "pending_action_id" not in prop:
        await cb.answer(prop.get("error", "Xatolik"), show_alert=True)
        return
    await cb.answer()
    await _safe_edit(cb.message, _format_confirmation_card(prop, has_health=True), pending_action_kb(prop["pending_action_id"]), add_delete=False)


async def _send_ai_result(res: dict, animator: _AIProgressAnimator | None = None):
    pending = res.get("pending_action")
    if pending:
        aid = pending["pending_action_id"]
        card = _format_confirmation_card(pending, has_health=bool(res.get("has_health_context")))
        if animator is not None:
            await animator.replace_with_final(card, pending_action_kb(aid), actionable=True)
        else:
            await messenger.send(card, "info", pending_action_kb(aid), actionable=True, bind_trigger=False)
        return

    reply = res.get("reply_text") or "Javob olinmadi."
    # HTML xavfsizligi: agar model HTML teg bermagan bo'lsa ham xavfsiz yuborish
    model_footer = ""
    if res.get("ok") and res.get("model_used"):
        model_footer = f"\n\n<i>🤖 {app.esc(res['model_used'])} · {int(round(res.get('latency_ms', 0)))} ms</i>"
    full_text = reply + model_footer
    safe_fallback = app.esc(reply) + model_footer

    ocr_receipt = res.get("ocr_receipt")
    if ocr_receipt:
        ocr_id = _store_ocr_receipt(ocr_receipt)
        kb = ocr_receipt_actions_kb(ocr_id, ocr_receipt)
    else:
        kb = chat_controls_kb()

    if animator is not None:
        await animator.replace_with_final(full_text, kb, actionable=False, fallback_text=safe_fallback)
        return

    sent = await messenger.send(full_text, "info", kb, bind_trigger=False)
    if sent is None:
        # Agar HTML parse xatosi bo'lsa, escape qilib qayta yuboramiz
        await messenger.send(safe_fallback, "info", kb, bind_trigger=False)


# ------------------------------------------------------------------ Text, Voice & Photo (OCR) Handlers

@router.message(
    AIChatState.active,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.startswith("!"),
    ~F.text.in_(common.MENU_TEXTS),
)
async def msg_ai_chat_text(message: Message, state: FSMContext):
    animator = _AIProgressAnimator()
    await animator.start("thinking")
    try:
        res = await ai_core.process_user_turn(
            user_text=message.text,
            on_progress=animator.on_progress,
        )
    finally:
        await animator.stop()
    await _send_ai_result(res, animator=animator)


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


async def _download_photo_bytes(message: Message) -> tuple[bytes | None, str]:
    photos = getattr(message, "photo", None)
    doc = getattr(message, "document", None)
    file_id = None
    mime = "image/jpeg"
    if photos:
        file_id = photos[-1].file_id
    elif doc and (getattr(doc, "mime_type", None) or "").startswith("image/"):
        file_id = doc.file_id
        mime = doc.mime_type or "image/jpeg"
    if not file_id:
        return None, mime
    try:
        tg_file = await app.bot.get_file(file_id)
        buf = io.BytesIO()
        await app.bot.download_file(tg_file.file_path, destination=buf)
        return buf.getvalue(), mime
    except Exception as exc:
        db.log_error("AIPhotoDownloadError", str(exc), traceback.format_exc(), "features.ai_chat")
        app.log.error(f"Rasmni yuklashda xatolik: {exc}")
        return None, mime


@router.message(StateFilter(AIChatState.active, default_state), F.voice)
async def msg_ai_voice(message: Message, state: FSMContext):
    animator = _AIProgressAnimator()
    await animator.start("voice")
    try:
        audio_bytes, mime = await _download_voice_bytes(message)
        if not audio_bytes:
            await animator.replace_with_final(
                "⚠️ Ovozli xabarni yuklab bo'lmadi. Iltimos, qayta yuboring yoki matn yozing.",
                None,
                actionable=False,
            )
            return
        res = await ai_core.process_user_turn(
            user_text=message.caption or None,
            audio_bytes=audio_bytes,
            audio_mime=mime,
            on_progress=animator.on_progress,
        )
    finally:
        await animator.stop()
    await _send_ai_result(res, animator=animator)


@router.message(StateFilter(AIChatState.active, default_state), F.photo)
async def msg_ai_photo(message: Message, state: FSMContext):
    animator = _AIProgressAnimator()
    await animator.start("ocr")
    try:
        image_bytes, mime = await _download_photo_bytes(message)
        if not image_bytes:
            await animator.replace_with_final(
                "⚠️ Rasmni yuklab bo'lmadi. Iltimos, qayta yuboring — botning qolgan barcha funksiyalari normal ishlamoqda.",
                chat_controls_kb(),
                actionable=False,
            )
            return
        res = await ai_core.process_user_turn(
            user_text=message.caption or None,
            image_bytes=image_bytes,
            image_mime=mime,
            on_progress=animator.on_progress,
        )
    finally:
        await animator.stop()
    await _send_ai_result(res, animator=animator)


@router.message(
    StateFilter(AIChatState.active, default_state),
    F.document,
    F.document.mime_type.startswith("image/"),
)
async def msg_ai_image_doc(message: Message, state: FSMContext):
    await msg_ai_photo(message, state)


def register_jobs(scheduler):
    """Har kuni 00:10 da AI Startup Snapshot (Tizim Pasporti) ni yangilaydi."""
    scheduler.add_job(
        ai_core.refresh_startup_snapshot,
        CronTrigger(hour=0, minute=10),
        id="ai_daily_passport_refresh",
        replace_existing=True,
    )
