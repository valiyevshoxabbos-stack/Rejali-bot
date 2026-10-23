"""Xabarlarni tozalash: 🗑 tugmasi, avto-o'chirish sozlamalari, o'chirilganlarni tiklash."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.triggers.interval import IntervalTrigger

import app
import messenger

router = Router()
HOME = InlineKeyboardButton(text="⬅️ Sozlamalar", callback_data="se:home")


@router.callback_query(F.data == "del")
async def cb_delete(cb: CallbackQuery):
    res = await messenger.user_delete(cb.message.message_id)
    if not res["ok"]:
        await cb.answer("Xabar 48 soatdan eski, Telegram o'chirishga ruxsat bermaydi.", show_alert=True)
        return
    await cb.answer()
    if res.get("switched"):
        label = messenger.LABELS.get(res["category"], res["category"])
        pref = messenger.get_pref(res["category"])
        await messenger.send(
            f"ℹ️ «{label}» ko'p o'chirilgani uchun endi {messenger.delay_text(pref['delay_min'])}dan keyin "
            "avtomatik o'chadi.\nQaytarish: Sozlamalar → Xabarlarni tozalash.", "system")


def menu_text_kb():
    lines = ["🧹 <b>Xabarlarni tozalash</b>\n",
             "Toifa 7 kunda 5 marta o'chirilsa, avtomatik o'chadigan bo'ladi. "
             "Harakat talab qiladigan xabarlar (javobsiz eslatma, qazo) hech qachon o'chmaydi.\n"]
    rows = []
    for cat, label in messenger.LABELS.items():
        p = messenger.get_pref(cat)
        state = f"avto, {messenger.delay_text(p['delay_min'])}" if p["auto_delete"] else "qo'lda"
        lines.append(f"• {label}: {state}")
        rows.append([InlineKeyboardButton(text=f"{'🟢' if p['auto_delete'] else '⚪'} {label}", callback_data=f"cl:t:{cat}"),
                     InlineKeyboardButton(text="⏱", callback_data=f"cl:d:{cat}")])
    rows.append([InlineKeyboardButton(text="♻️ O'chirilganlarni tiklash", callback_data="cl:r")])
    rows.append([HOME])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(cb: CallbackQuery):
    text, kb = menu_text_kb()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "se:clean")
async def cb_menu(cb: CallbackQuery):
    await show_menu(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("cl:t:"))
async def cb_toggle(cb: CallbackQuery):
    cat = cb.data.split(":")[2]
    messenger.set_pref(cat, auto=not messenger.get_pref(cat)["auto_delete"])
    await show_menu(cb)
    await cb.answer()


@router.callback_query(F.data.startswith("cl:d:"))
async def cb_delay(cb: CallbackQuery):
    cat = cb.data.split(":")[2]
    cur = messenger.get_pref(cat)["delay_min"]
    steps = messenger.DELAY_STEPS
    nxt = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else steps[0]
    messenger.set_pref(cat, delay=nxt)
    await show_menu(cb)
    await cb.answer(f"Kechikish: {messenger.delay_text(nxt)}")


@router.callback_query(F.data == "cl:r")
async def cb_restore_list(cb: CallbackQuery):
    items = messenger.recent_deleted()
    if not items:
        await cb.answer("Oxirgi 7 kunda o'chirilgan xabar yo'q", show_alert=True)
        return
    rows = []
    lines = ["♻️ <b>Tiklash</b> (matn qayta yuboriladi; asl vaqt va tugmalar tiklanmaydi)\n"]
    for i, it in enumerate(items, 1):
        snippet = " ".join((it["text"] or "").replace("<b>", "").replace("</b>", "").split())[:40]
        lines.append(f"{i}. {app.esc(snippet)}")
        rows.append([InlineKeyboardButton(text=f"↩️ {i}", callback_data=f"cl:x:{it['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="se:clean")])
    await cb.message.edit_text("\n".join(lines), reply_markup=messenger.with_delete(InlineKeyboardMarkup(inline_keyboard=rows)))
    await cb.answer()


@router.callback_query(F.data.startswith("cl:x:"))
async def cb_restore(cb: CallbackQuery):
    ok = await messenger.restore(int(cb.data.split(":")[2]))
    await cb.answer("Qayta yuborildi" if ok else "Tiklab bo'lmadi", show_alert=not ok)


def register_jobs(scheduler):
    scheduler.add_job(messenger.cleanup_tick, IntervalTrigger(minutes=10), id="cleanup", replace_existing=True)
