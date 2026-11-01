"""
AltronBek 3.1: umumiy ob'ektlar (bot, dispatcher, scheduler) va yordamchilar.
Barcha modullar shu faylni `import app` qilib ishlatadi (vaqt uchun app.now()).
"""

import html
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: E402

VERSION = "3.1"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
COUNTRY = os.environ.get("COUNTRY", "Uzbekistan")
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("altronbek")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)

STARTED_AT = datetime.now(TZ)

# Ixtiyoriy funksiyalar (weather, calculator ...): yuklangan bo'lsa shu yerda turadi.
OPTIONAL: dict = {}


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def at(d: date, hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=TZ)


def esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def schedule_job(run_at: datetime, func, args: list, job_id: str) -> bool:
    """Bir martalik ish. Vaqti o'tib ketgan bo'lsa qo'shilmaydi."""
    if run_at <= now():
        return False
    scheduler.add_job(
        func, trigger="date", run_date=run_at, args=args, id=job_id,
        replace_existing=True, misfire_grace_time=300,
    )
    return True
