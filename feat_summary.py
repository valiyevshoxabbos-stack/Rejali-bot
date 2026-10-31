"""Kunlik xulosa: /bugun, ertalabki (06:30) va kechki (21:45) xabarlar."""

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_birthdays as birthdays
import feat_journal as journal
import messenger
import qazo
import scoring
import services as sv

router = Router()
PRAYERS = ("Bomdod", "Peshin", "Asr", "Shom", "Xufton")


def qazo_block() -> str:
    items = qazo.open_qazo()
    if not items:
        return ""
    lines = ["\n\n⚠️ <b>Ochiq qazolar:</b>"]
    lines += [f"• {app.esc(q['title'])} ({q['date']}): /{q['code']}" for q in items]
    return "\n".join(lines)


async def prayer_times_today() -> dict | None:
    try:
        return await asyncio.to_thread(sv.fetch_prayer_times, sv.get_city(), app.COUNTRY, app.today())
    except Exception as e:
        app.log.error(f"Namoz vaqtlarini olishda xatolik: {e}")
        return None


async def build_today_text() -> str:
    d = app.today()
    items = []
    for r in sv.reminders_for_day(d):
        icon = scoring.PRIORITY_ICON.get(r["priority"], "")
        tag = " (ixtiyoriy)" if r["eff_mode"] == "optional" else ""
        items.append((r["start"], f"{icon} {app.esc(r['title'])}{tag}".strip()))
    times = await prayer_times_today()
    if times:
        items += [(times[p], f"{p} namozi") for p in PRAYERS]
    items.sort(key=lambda x: x[0])
    lines = [f"<b>{sv.WEEKDAY_NAMES[d.weekday()]} kuni rejasi:</b>\n"]
    if sv.pause_active(d):
        lines.append(f"⏸ Pauza rejimi ({sv.pause_until().strftime('%d.%m')} gacha): faqat namoz vaqtlari.\n")
    lines += [f"🕒 {t} — {label}" for t, label in items]
    if not items:
        lines.append("Bugun reja yo'q.")
    return "\n".join(lines)


def today_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Bajardim", callback_data="ql:list"),
        InlineKeyboardButton(text="🕌 Qazolar", callback_data="qz:list"),
    ], [
        InlineKeyboardButton(text="😴 Uyqu", callback_data="sl:menu"),
        InlineKeyboardButton(text="⏸ Pauza", callback_data="pz:open"),
    ]])


async def show_today(target):
    text = await build_today_text() + qazo_block()
    await messenger.send(text, "summary", today_keyboard())


@router.message(Command("bugun"))
async def cmd_today(message: Message):
    await show_today(message)


async def send_morning():
    d = app.today()
    text = "🌅 <b>Xayrli tong!</b>\n\n" + await build_today_text()
    bdays = birthdays.today_lines(d)
    if bdays:
        text += "\n\n" + "\n".join(bdays)
    await messenger.send(text + qazo_block(), "summary", bind_trigger=False)
    await journal.sync(d)


async def send_evening():
    d = app.today()
    now = app.now()
    ds = scoring.day_summary(d)
    lines = ["🌙 <b>Kun yakuni</b>\n"]
    if ds["done"] or ds["bonus"]:
        extra = f" (+{ds['bonus']} ixtiyoriy)" if ds["bonus"] else ""
        lines.append(f"✅ Bugun bajarildi: {ds['done']}/{ds['total']}{extra}, +{ds['points']} ball")
    left = db.q(
        "SELECT title, status FROM occurrences WHERE date = ? AND is_subtask = 0 AND is_optional = 0 "
        "AND prayer IS NULL AND status IN ('skipped','pending','postponed') AND scheduled_at < ?",
        (d.isoformat(), now.isoformat()),
    )
    if left:
        names = {"skipped": "bajarilmadi", "pending": "javobsiz qoldi", "postponed": "kechiktirildi"}
        lines.append("\n<b>Qoldirilgan eslatmalar:</b>")
        lines += [f"• {app.esc(r['title'])} — {names[r['status']]}" for r in left]
    if len(lines) == 1:
        lines.append("Bugun uchun yozuv yo'q.")
    await messenger.send("\n".join(lines) + qazo_block(), "summary", bind_trigger=False)


def register_jobs(scheduler):
    scheduler.add_job(send_morning, CronTrigger(hour=6, minute=30), id="morning", replace_existing=True)
    scheduler.add_job(send_evening, CronTrigger(hour=21, minute=45), id="evening", replace_existing=True)
