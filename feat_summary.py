"""Kunlik xulosa: /bugun, ertalabki (06:30) va kechki (21:45) xabarlar."""

import asyncio
from datetime import date

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_birthdays as birthdays
import feat_challenges as challenges
import feat_finance as finance
import feat_journal as journal
import feat_sleep as sleep
import feat_weather as weather
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


def school_today_section(d: date) -> str:
    if "education" not in app.OPTIONAL:
        return ""
    edu = app.OPTIONAL["education"]
    off, off_title, off_type = edu.is_off_day(d)
    if off or d.weekday() >= 6:
        return ""
    slots = [s for s in edu.get_timetable_for_day(d.weekday()) if s.get("course_id")]
    if not slots:
        return ""
    lines = ["\n🎓 <b>Bugungi maktab darslari:</b>"]
    for s in slots:
        p = s["period_number"]
        c_name = s["class_name"]
        s_name = s["subject_name"]
        start_t, _ = edu.EDU_BELLS[p]
        c_order = s["current_topic_order"]
        topic = db.q1("SELECT title FROM edu_topics WHERE course_id = ? AND topic_order = ?", (s["course_id"], c_order))
        t_title = f": <i>{app.esc(topic['title'])}</i>" if topic else ""
        lines.append(f"• <b>{p}-soat ({start_t}):</b> {app.esc(c_name)} — {app.esc(s_name)} (№{c_order}{t_title})")
        notes = edu.get_unresolved_notes(s["course_id"])
        if notes:
            lines.append("  📌 <i>Izoh:</i> " + "; ".join(app.esc(n["note_text"]) for n in notes))
    return "\n".join(lines)


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
    edu_text = school_today_section(d)
    if edu_text:
        lines.append(edu_text)
    return "\n".join(lines)


def today_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Bajardim", callback_data="ql:list"),
        InlineKeyboardButton(text="🕌 Qazolar", callback_data="qz:list"),
    ], [
        InlineKeyboardButton(text="🏆 Chelenjlar", callback_data="ch:home"),
        InlineKeyboardButton(text="💰 Moliya", callback_data="fn:home"),
    ], [
        InlineKeyboardButton(text="🎓 Maktab", callback_data="ed:home"),
        InlineKeyboardButton(text="💊 Dorixona", callback_data="hl:home"),
    ], [
        InlineKeyboardButton(text="😴 Yotdim / Uyqu", callback_data="sl:menu"),
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
    if sleep.has_open_sleep():
        sleep.auto_close_open_sleep(app.at(d, "06:30"))
    text = "🌅 <b>Xayrli tong!</b>\n\n" + await build_today_text()
    text += await weather.morning_weather_section()
    text += finance.morning_finance_section(d)
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
    if ds["done"] or ds["bonus"] or ds.get("challenge_points"):
        extra = f" (+{ds['bonus']} bonus)" if ds["bonus"] else ""
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

    today_notes = db.q("SELECT text FROM notes WHERE date = ? ORDER BY id", (d.isoformat(),))
    notes_block = ""
    if today_notes:
        notes_block = "\n\n📌 <b>Bugungi qaydlar:</b>\n" + "\n".join(
            f"• {app.esc(n['text'])}" for n in today_notes
        )

    ch_text, ch_kb = challenges.evening_challenges_section_and_kb(d)
    tom_wx = await weather.evening_tomorrow_section()
    await messenger.send(
        "\n".join(lines) + notes_block + ch_text + tom_wx + qazo_block(),
        "summary",
        ch_kb,
        bind_trigger=False,
    )


async def send_sunday_weekly_review():
    sections = []
    if "finance" in app.OPTIONAL:
        fin_rep = app.OPTIONAL["finance"].render_weekly_finance_report()
        sections.append(fin_rep)
    if "health" in app.OPTIONAL:
        health_rep = app.OPTIONAL["health"].render_weekly_health_report()
        sections.append(health_rep)
    if sections:
        text = "📅 <b>Yakshanba oqshomi: Haftalik hisobot</b>\n\n" + "\n\n".join(sections)
        await messenger.send(text, "summary", bind_trigger=False)


def register_jobs(scheduler):
    scheduler.add_job(send_morning, CronTrigger(hour=6, minute=30), id="morning", replace_existing=True)
    scheduler.add_job(send_evening, CronTrigger(hour=21, minute=45), id="evening", replace_existing=True)
    scheduler.add_job(send_sunday_weekly_review, CronTrigger(day_of_week="sun", hour=21, minute=0), id="sunday_weekly_review", replace_existing=True)
