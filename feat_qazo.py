"""Qazo: avtomatik aniqlash (tezkor tekshiruv bilan), /qazolar, /bomdod_21_09 kabi kodlar bilan chiqarish."""

import asyncio
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from apscheduler.triggers.interval import IntervalTrigger

import app
import db
import feat_journal as journal
import messenger
import qazo as qz
import services as sv

router = Router()


def list_text() -> str:
    items = qz.open_qazo()
    if not items:
        return "✅ Ochiq qazo yo'q."
    lines = ["<b>Ochiq qazolar:</b>\n"] + [f"• {app.esc(q['title'])} ({q['date']}): /{q['code']}" for q in items]
    return "\n".join(lines) + "\n\nO'qigan bo'lsangiz, tegishli kodni yuboring."


@router.message(Command("qazolar"))
async def cmd_list(message: Message):
    await messenger.send(list_text(), "info")


@router.callback_query(F.data == "qz:list")
async def cb_list(cb: CallbackQuery):
    await cb.answer()
    await messenger.send(list_text(), "info", bind_trigger=False)


@router.message(F.text.regexp(qz.CODE_RE))
async def code_handler(message: Message):
    code = message.text.strip().lstrip("/").lower()
    row = db.q1("SELECT date FROM qazo WHERE code = ? AND status = 'open' ORDER BY date, id", (code,))
    if qz.mark_qazo_done(code, app.now().isoformat()):
        await messenger.send(f"✅ Qazo chiqarildi: {code}. Alloh qabul qilsin.", "confirm")
        if row and db.get_setting(f"journal_{row['date']}"):
            await journal.sync(date.fromisoformat(row["date"]))
    else:
        await messenger.send("Bunday ochiq qazo topilmadi (kod noto'g'ri yoki allaqachon chiqarilgan).", "confirm")


async def check_qazo():
    if not sv.has_pending_past_prayers(app.now()):
        return
    try:
        created = await asyncio.to_thread(sv.find_new_qazo, app.now(), sv.get_city(), app.COUNTRY)
    except Exception as e:
        app.log.error(f"Qazo tekshiruvida xatolik: {e}")
        return
    touched_dates = set()
    for q in created:
        await messenger.send(
            f"⚠️ <b>{app.esc(q['title'])}</b> qazo bo'ldi.\n"
            f"Qazosini o'qigach, shu kodni yuboring: /{q['code']}",
            "qazo", actionable=True, bind_trigger=False,
        )
        touched_dates.add(q["date"])
    for d in touched_dates:
        if db.get_setting(f"journal_{d.isoformat()}"):
            await journal.sync(d)


def register_jobs(scheduler):
    scheduler.add_job(check_qazo, IntervalTrigger(minutes=1), id="qazo_check", replace_existing=True)
