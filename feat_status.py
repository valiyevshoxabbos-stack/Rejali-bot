"""Bot holati: /holat (baza yo'li, Volume tekshiruvi, yozuvlar soni) va ishga tushish xabari."""

import os

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import app
import db
import messenger
import services as sv

router = Router()


def _in_volume(db_path: str) -> bool:
    norm = db_path.replace("\\", "/")
    return norm.startswith("/data/")


def build_text() -> str:
    up = app.now() - app.STARTED_AT
    hours, rem = divmod(int(up.total_seconds()), 3600)
    p = db.path()
    try:
        size = f"{os.path.getsize(p) / 1024:.0f} KB"
    except OSError:
        size = "?"
    last_backup = db.get_setting("last_backup", "hali yo'q")
    pause = sv.pause_until()
    bdays = db.q1("SELECT COUNT(*) AS c FROM birthdays")["c"]
    custom = db.q1("SELECT COUNT(*) AS c FROM reminders WHERE source='custom' AND active=1")["c"]
    sleeps = db.q1("SELECT COUNT(*) AS c FROM sleep_log WHERE duration_min IS NOT NULL")["c"]
    weights = db.q1("SELECT COUNT(*) AS c FROM weight_log")["c"]
    qazos = db.q1("SELECT COUNT(*) AS c FROM qazo WHERE status='open'")["c"]

    vol_warn = ""
    if not _in_volume(p):
        vol_warn = "\n⚠️ <b>Diqqat:</b> baza <code>/data</code> (Volume) ichida emas! Qayta deploy'da ma'lumot o'chishi mumkin.\n"

    return (
        f"ℹ️ <b>AltronBek {app.VERSION}</b>\n"
        f"{vol_warn}\n"
        f"Ishlash vaqti: {hours} s {rem // 60} daq\n"
        f"Baza: <code>{app.esc(p)}</code> ({size}, sxema v{db.schema_version()})\n"
        f"Yozuvlar: 🎂 {bdays} · 📝 maxsus {custom} · 😴 {sleeps} · ⚖️ {weights} · 🕌 ochiq qazo {qazos}\n"
        f"Rejalashtirilgan ishlar: {len(app.scheduler.get_jobs())}\n"
        f"Shahar: {app.esc(sv.get_city())}\n"
        f"Oxirgi zaxira: {app.esc(last_backup)}\n"
        f"Pauza: {pause.strftime('%d.%m.%Y') + ' gacha' if pause and pause >= app.today() else 'yo‘q'}\n"
        f"Ixtiyoriy funksiyalar: {', '.join(app.OPTIONAL) or 'hali yo‘q (3.1+)'}"
    )


@router.message(Command("holat"))
async def cmd_status(message: Message):
    await messenger.send(build_text(), "info")


async def startup_message():
    await messenger.send(f"🟢 AltronBek {app.VERSION} ishga tushdi.", "system", bind_trigger=False)
