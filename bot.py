"""
Umarkhan uchun kunlik reja + namoz vaqtlari eslatuvchi Telegram bot.
Python 3.10+, aiogram 3.x, APScheduler, requests (Aladhan API).
"""

import asyncio
import logging
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from storage import Storage
from schedule_data import WEEKLY_PLAN

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])  # faqat bitta foydalanuvchi uchun
CITY = os.environ.get("CITY", "Chirchiq")
COUNTRY = os.environ.get("COUNTRY", "Uzbekistan")
TZ = ZoneInfo(os.environ.get("TZ", "Asia/Tashkent"))
METHOD = os.environ.get("PRAYER_METHOD", "2")  # Aladhan hisoblash usuli

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)
storage = Storage("data.json")

WEEKDAY_NAMES_UZ = {
    0: "Dushanba",
    1: "Seshanba",
    2: "Chorshanba",
    3: "Payshanba",
    4: "Juma",
    5: "Shanba",
    6: "Yakshanba",
}

# --- Namoz vaqtlarini olish (Aladhan API) ---

def fetch_prayer_times_sync(city: str, country: str, d: date) -> dict:
    url = "https://api.aladhan.com/v1/timingsByCity"
    params = {
        "city": city,
        "country": country,
        "method": METHOD,
        "date": d.strftime("%d-%m-%Y"),
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()["data"]["timings"]
    return {
        "Bomdod": data["Fajr"],
        "Quyosh": data["Sunrise"],
        "Peshin": data["Dhuhr"],
        "Asr": data["Asr"],
        "Shom": data["Maghrib"],
        "Xufton": data["Isha"],
    }


async def fetch_prayer_times(city: str, country: str, d: date) -> dict:
    return await asyncio.to_thread(fetch_prayer_times_sync, city, country, d)


# --- Eslatma yuborish va bajarildi/yo'q tugmasi ---

def done_keyboard(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Bajardim", callback_data=f"done:{item_id}"),
                InlineKeyboardButton(text="❌ Bajarmadim", callback_data=f"skip:{item_id}"),
            ]
        ]
    )


async def send_reminder(text: str, item_id: str):
    await bot.send_message(CHAT_ID, text, reply_markup=done_keyboard(item_id))


@dp.callback_query(F.data.startswith("done:") | F.data.startswith("skip:"))
async def handle_mark(callback: CallbackQuery):
    status, item_id = callback.data.split(":", 1)
    today = date.today().isoformat()
    storage.mark(today, item_id, status == "done")
    mark = "✅" if status == "done" else "❌"
    await callback.message.edit_text(f"{callback.message.text}\n\n{mark} Belgilandi.")
    await callback.answer()


# --- Kunlik rejalashtirish: har kuni ertalab o'sha kun uchun barcha eslatmalarni jadvalga qo'yish ---

async def schedule_today_reminders():
    today = datetime.now(TZ).date()
    weekday = today.weekday()  # 0=Dushanba
    plan = WEEKLY_PLAN.get(weekday)
    if not plan:
        return

    try:
        prayers = await fetch_prayer_times(CITY, COUNTRY, today)
    except Exception as e:
        logger.error(f"Namoz vaqtlarini olishda xatolik: {e}")
        prayers = {}

    # Oddiy vazifalar (zal, universitet, basseyn va h.k.)
    for item in plan.get("tasks", []):
        hh, mm = map(int, item["time"].split(":"))
        run_time = datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_time < datetime.now(TZ):
            continue
        item_id = f"{today.isoformat()}_{item['id']}"
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=run_time,
            args=[f"⏰ <b>{item['label']}</b>\n{item.get('note', '')}", item_id],
            id=f"job_{item_id}",
            replace_existing=True,
        )

    # Namoz vaqtlari
    if plan.get("prayers", True):
        for prayer_name, time_str in prayers.items():
            hh, mm = map(int, time_str.split(":"))
            run_time = datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if run_time < datetime.now(TZ):
                continue
            item_id = f"{today.isoformat()}_namoz_{prayer_name}"
            scheduler.add_job(
                send_reminder,
                trigger="date",
                run_date=run_time,
                args=[f"🕌 <b>{prayer_name}</b> vaqti keldi ({time_str})", item_id],
                id=f"job_{item_id}",
                replace_existing=True,
            )

    logger.info(f"{today} uchun eslatmalar rejalashtirildi.")


# --- Buyruqlar ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Assalomu alaykum, Umarkhan!\n\n"
        "Men sizning kunlik rejangiz va namoz vaqtlarini eslatib turaman.\n\n"
        "Buyruqlar:\n"
        "/bugun — bugungi rejani ko'rish\n"
        "/namoz — bugungi namoz vaqtlari\n"
        "/statistika — oxirgi 7 kunlik bajarish statistikasi\n"
        "/shahar — namoz vaqtlari hisoblanadigan shaharni o'zgartirish"
    )


@dp.message(Command("bugun"))
async def cmd_today(message: Message):
    weekday = datetime.now(TZ).weekday()
    plan = WEEKLY_PLAN.get(weekday)
    name = WEEKDAY_NAMES_UZ[weekday]
    if not plan or not plan.get("tasks"):
        await message.answer(f"<b>{name}</b> uchun reja topilmadi.")
        return
    lines = [f"<b>{name} kuni rejasi:</b>\n"]
    for item in plan["tasks"]:
        lines.append(f"🕒 {item['time']} — {item['label']}")
    await message.answer("\n".join(lines))


@dp.message(Command("namoz"))
async def cmd_prayers(message: Message):
    today = datetime.now(TZ).date()
    try:
        prayers = await fetch_prayer_times(CITY, COUNTRY, today)
    except Exception as e:
        await message.answer(f"Namoz vaqtlarini olib bo'lmadi: {e}")
        return
    lines = [f"<b>{CITY} — bugungi namoz vaqtlari:</b>\n"]
    for name, t in prayers.items():
        lines.append(f"🕌 {name}: {t}")
    await message.answer("\n".join(lines))


@dp.message(Command("statistika"))
async def cmd_stats(message: Message):
    stats = storage.get_last_days(7)
    if not stats:
        await message.answer("Hali hech qanday belgilangan vazifa yo'q.")
        return
    lines = ["<b>Oxirgi 7 kunlik statistika:</b>\n"]
    for day, (done, total) in stats.items():
        percent = round(done / total * 100) if total else 0
        lines.append(f"{day}: {done}/{total} ({percent}%)")
    await message.answer("\n".join(lines))


@dp.message(Command("shahar"))
async def cmd_city(message: Message):
    args = message.text.split(maxsplit=1)
    global CITY
    if len(args) < 2:
        await message.answer(
            f"Hozirgi shahar: <b>{CITY}</b>\n"
            "O'zgartirish uchun: /shahar Toshkent"
        )
        return
    CITY = args[1].strip()
    await message.answer(f"Shahar <b>{CITY}</b> ga o'zgartirildi. Ertangi kundan yangi vaqtlar hisoblanadi.")


# --- Ishga tushirish ---

async def main():
    # Har kuni 00:05 da o'sha kun uchun barcha eslatmalarni tuzish
    scheduler.add_job(
        schedule_today_reminders,
        trigger=CronTrigger(hour=0, minute=5),
        id="daily_scheduler",
        replace_existing=True,
    )
    scheduler.start()

    # Bot ishga tushganda ham darhol bugungi qolgan eslatmalarni tuzib qo'yish
    await schedule_today_reminders()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
