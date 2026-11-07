"""
Xabarlarni tozalash (3.5 — In-place Edit Message UX):
- 🗑 tugmasi, avto-o'chirish sozlamalari, foydalanuvchi xabarini o'chirish, tiklash.
- Barcha menyu va tiklash amallari yangi xabar yubormasdan, mavjud xabarni tahrirlash (edit_text) orqali bajariladi.
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.triggers.interval import IntervalTrigger

import app
import db
import messenger

router = Router()
HOME = InlineKeyboardButton(text="⬅️ Sozlamalar", callback_data="se:home")


async def _edit_inplace(message, text: str, kb: InlineKeyboardMarkup | None):
    markup = messenger.with_delete(kb)
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            app.log.warning(f"Tozalash oynasini tahrirlashda xato: {e}")
    db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, message.message_id))


@router.callback_query(F.data == "del")
async def cb_delete(cb: CallbackQuery):
    res = await messenger.user_delete(cb.message.message_id)
    if not res["ok"]:
        await cb.answer("Xabar 48 soatdan eski, Telegram o'chirishga ruxsat bermaydi.", show_alert=True)
        return
    if res.get("switched"):
        label = messenger.LABELS.get(res["category"], res["category"])
        pref = messenger.get_pref(res["category"])
        await cb.answer(
            f"ℹ️ «{label}» ko'p o'chirilgani uchun endi {messenger.delay_text(pref['delay_min'])}dan keyin avto-o'chadi.",
            show_alert=True,
        )
        return
    await cb.answer()


def menu_text_kb():
    user_del = messenger.user_msgs_enabled()
    lines = [
        "🧹 <b>Xabarlarni tozalash</b>\n",
        "Toifa 7 kunda 5 marta o'chirilsa, avtomatik o'chadigan bo'ladi. "
        "Harakat talab qiladigan xabarlar (javobsiz eslatma, qazo, jurnal) hech qachon o'chmaydi.\n",
        f"• Sizning bog'langan xabarlaringiz: <b>{'qo‘shib o‘chiriladi' if user_del else 'o‘chirilmaydi'}</b>",
    ]
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢' if user_del else '⚪'} Mening xabarlarimni ham o'chirish",
            callback_data="cl:u",
        )]
    ]
    for cat, label in messenger.LABELS.items():
        p = messenger.get_pref(cat)
        state = f"avto, {messenger.delay_text(p['delay_min'])}" if p["auto_delete"] else "qo'lda"
        lines.append(f"• {label}: {state}")
        rows.append([
            InlineKeyboardButton(text=f"{'🟢' if p['auto_delete'] else '⚪'} {label}", callback_data=f"cl:t:{cat}"),
            InlineKeyboardButton(text="⏱", callback_data=f"cl:d:{cat}"),
        ])
    rows.append([InlineKeyboardButton(text="♻️ O'chirilganlarni tiklash", callback_data="cl:r")])
    rows.append([HOME])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(cb: CallbackQuery):
    text, kb = menu_text_kb()
    await _edit_inplace(cb.message, text, kb)


@router.callback_query(F.data == "se:clean")
async def cb_menu(cb: CallbackQuery):
    await cb.answer()
    await show_menu(cb)


@router.callback_query(F.data == "cl:u")
async def cb_toggle_user_msgs(cb: CallbackQuery):
    new_val = not messenger.user_msgs_enabled()
    messenger.set_user_msgs_enabled(new_val)
    await cb.answer("Yoqildi" if new_val else "O'chirildi")
    await show_menu(cb)


@router.callback_query(F.data.startswith("cl:t:"))
async def cb_toggle(cb: CallbackQuery):
    cat = cb.data.split(":")[2]
    messenger.set_pref(cat, auto=not messenger.get_pref(cat)["auto_delete"])
    await cb.answer()
    await show_menu(cb)


@router.callback_query(F.data.startswith("cl:d:"))
async def cb_delay(cb: CallbackQuery):
    cat = cb.data.split(":")[2]
    cur = messenger.get_pref(cat)["delay_min"]
    steps = messenger.DELAY_STEPS
    nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else steps[0]
    messenger.set_pref(cat, delay=nxt)
    await cb.answer(f"Kechikish: {messenger.delay_text(nxt)}")
    await show_menu(cb)


def _restore_list_view():
    items = messenger.recent_deleted()
    if not items:
        return (
            "♻️ <b>Tiklash</b>\n\nOxirgi 7 kunda o'chirilgan xabar yo'q.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="se:clean")]]),
        )
    rows = []
    lines = ["♻️ <b>Tiklash</b> (tanlangan xabar shu oynaning o'zida ko'rsatiladi)\n"]
    for i, it in enumerate(items, 1):
        snippet = " ".join((it["text"] or "").replace("<b>", "").replace("</b>", "").split())[:40]
        lines.append(f"{i}. {app.esc(snippet)}")
        rows.append([InlineKeyboardButton(text=f"↩️ {i}", callback_data=f"cl:x:{it['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="se:clean")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "cl:r")
async def cb_restore_list(cb: CallbackQuery):
    await cb.answer()
    text, kb = _restore_list_view()
    await _edit_inplace(cb.message, text, kb)


@router.callback_query(F.data.startswith("cl:x:"))
async def cb_restore(cb: CallbackQuery):
    restored_text = messenger.restore_inplace_data(int(cb.data.split(":")[2]))
    if restored_text is None:
        await cb.answer("Tiklab bo'lmadi", show_alert=True)
        return
    await cb.answer("Tiklandi")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Tiklash ro'yxati", callback_data="cl:r"),
         InlineKeyboardButton(text="🧹 Tozalash", callback_data="se:clean")],
    ])
    await _edit_inplace(cb.message, f"♻️ <b>Tiklangan xabar:</b>\n\n{restored_text}", kb)


def register_jobs(scheduler):
    scheduler.add_job(messenger.cleanup_tick, IntervalTrigger(minutes=10), id="cleanup", replace_existing=True)
