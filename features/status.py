"""Bot holati: /holat va ishga tushish xabari."""

import os

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import app
import db
import messenger
import services as sv

router = Router()


def build_text() -> str:
    up = app.now() - app.STARTED_AT
    hours, rem = divmod(int(up.total_seconds()), 3600)
    try:
        size = f"{os.path.getsize(db.path()) / 1024:.0f} KB"
    except OSError:
        size = "?"
    last_backup = db.get_setting("last_backup", "hali yo'q")
    pause = sv.pause_until()
    return (f"ℹ️ <b>AltronBek {app.VERSION}</b>\n\n"
            f"Ishlash vaqti: {hours} s {rem // 60} daq\n"
            f"Baza: {size} (sxema v{db.schema_version()})\n"
            f"Rejalashtirilgan ishlar: {len(app.scheduler.get_jobs())}\n"
            f"Shahar: {app.esc(sv.get_city())}\n"
            f"Oxirgi zaxira: {app.esc(last_backup)}\n"
            f"Pauza: {pause.strftime('%d.%m.%Y') + ' gacha' if pause and pause >= app.today() else 'yo‘q'}\n"
            f"Ixtiyoriy funksiyalar: {', '.join(app.OPTIONAL) or 'hali yo‘q (3.1+)'}")


@router.message(Command("holat"))
async def cmd_status(message: Message):
    await messenger.send(build_text(), "info")


async def startup_message():
    await messenger.send(f"🟢 AltronBek {app.VERSION} ishga tushdi.", "system")
