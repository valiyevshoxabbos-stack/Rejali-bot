"""
Umarkhan uchun kunlik reja + namoz vaqtlari eslatuvchi Telegram bot.
Python 3.10+, aiogram 3.x, APScheduler, requests (Aladhan API).
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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
from custom_reminders import CustomReminders

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
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone=TZ)
storage = Storage("data.json")
custom_reminders = CustomReminders("custom_reminders.json")

WEEKDAY_NAMES_UZ = {
    0: "Dushanba",
    1: "Seshanba",
    2: "Chorshanba",
    3: "Payshanba",
    4: "Juma",
    5: "Shanba",
    6: "Yakshanba",
}

WEEKDAY_NAME_TO_NUM = {
    "dushanba": 0,
    "seshanba": 1,
    "chorshanba": 2,
    "payshanba": 3,
    "juma": 4,
    "shanba": 5,
    "yakshanba": 6,
}

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class AddReminder(StatesGroup):
    choosing_type = State()
    waiting_weekday = State()
    waiting_date = State()
    waiting_time = State()
    waiting_text = State()

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
    try:
        await bot.send_message(CHAT_ID, text, reply_markup=done_keyboard(item_id))
    except Exception as e:
        logger.error(f"Eslatma yuborishda xatolik ({item_id}): {e}")


@dp.callback_query(F.data.startswith("done:") | F.data.startswith("skip:"))
async def handle_mark(callback: CallbackQuery):
    status, item_id = callback.data.split(":", 1)
    today = date.today().isoformat()
    storage.mark(today, item_id, status == "done")
    mark = "✅" if status == "done" else "❌"
    await callback.message.edit_text(f"{callback.message.text}\n\n{mark} Belgilandi.")
    await callback.answer()


@dp.errors()
async def global_error_handler(event):
    """
    Har qanday handler ichida kutilmagan xato yuz bersa, botni yiqitmasdan
    faqat log yozadi va ishlashda davom etadi.
    """
    logger.exception(f"Kutilmagan xatolik: {event.exception}")
    return True


# --- Kunlik rejalashtirish: har kuni ertalab o'sha kun uchun barcha eslatmalarni jadvalga qo'yish ---

async def schedule_today_reminders():
    today = datetime.now(TZ).date()
    weekday = today.weekday()  # 0=Dushanba
    plan = WEEKLY_PLAN.get(weekday, {})

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

    # Foydalanuvchi qo'shgan doimiy (har kuni / haftalik) eslatmalar
    for item in custom_reminders.get_for_daily_and_weekly(weekday):
        hh, mm = map(int, item["time"].split(":"))
        run_time = datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_time < datetime.now(TZ):
            continue
        item_id = f"{today.isoformat()}_custom_{item['id']}"
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=run_time,
            args=[f"⏰ <b>{html.escape(item['text'])}</b>", item_id],
            id=f"job_{item_id}",
            replace_existing=True,
        )

    # Foydalanuvchi qo'shgan bir martalik (aniq sanali) eslatmalar
    for item in custom_reminders.get_for_date(today.isoformat()):
        hh, mm = map(int, item["time"].split(":"))
        run_time = datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run_time < datetime.now(TZ):
            continue
        item_id = f"{today.isoformat()}_once_{item['id']}"
        rid = item["id"]
        scheduler.add_job(
            fire_once_reminder,
            trigger="date",
            run_date=run_time,
            args=[f"📅 <b>{html.escape(item['text'])}</b>", item_id, rid],
            id=f"job_{item_id}",
            replace_existing=True,
        )

    logger.info(f"{today} uchun eslatmalar rejalashtirildi.")


async def fire_once_reminder(text: str, item_id: str, reminder_id: int):
    await send_reminder(text, item_id)
    custom_reminders.remove_once_after_fire(reminder_id)


def schedule_single_custom(reminder_id: int, time_str: str, text: str, once: bool):
    """
    Yangi qo'shilgan maxsus eslatma bugungi kunga tegishli bo'lsa, uni
    kutib o'tirmasdan darhol schedulerga qo'shadi (agar vaqti hali o'tmagan bo'lsa).
    """
    today = datetime.now(TZ).date()
    hh, mm = map(int, time_str.split(":"))
    run_time = datetime.now(TZ).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run_time < datetime.now(TZ):
        return  # vaqti allaqachon o'tgan, bugun uchun eslatma yubormaymiz

    if once:
        item_id = f"{today.isoformat()}_once_{reminder_id}"
        scheduler.add_job(
            fire_once_reminder,
            trigger="date",
            run_date=run_time,
            args=[f"📅 <b>{html.escape(text)}</b>", item_id, reminder_id],
            id=f"job_{item_id}",
            replace_existing=True,
        )
    else:
        item_id = f"{today.isoformat()}_custom_{reminder_id}"
        scheduler.add_job(
            send_reminder,
            trigger="date",
            run_date=run_time,
            args=[f"⏰ <b>{html.escape(text)}</b>", item_id],
            id=f"job_{item_id}",
            replace_existing=True,
        )


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
        "/shahar — namoz vaqtlari hisoblanadigan shaharni o'zgartirish\n"
        "/qoshish — yangi eslatma qo'shish (har kuni / haftalik / aniq sana)\n"
        "/royxat — qo'shilgan maxsus eslatmalar ro'yxati\n"
        "/ochirish — maxsus eslatmani o'chirish (masalan: /ochirish 3)\n"
        "/bekor — eslatma qo'shish jarayonini bekor qilish"
    )


@dp.message(Command("bugun"))
async def cmd_today(message: Message):
    weekday = datetime.now(TZ).weekday()
    today_str = datetime.now(TZ).date().isoformat()
    plan = WEEKLY_PLAN.get(weekday)
    name = WEEKDAY_NAMES_UZ[weekday]

    all_items = []  # (vaqt, matn) juftliklari, keyin vaqt bo'yicha saralanadi

    if plan and plan.get("tasks"):
        for item in plan["tasks"]:
            all_items.append((item["time"], item["label"]))

    for item in custom_reminders.get_for_daily_and_weekly(weekday):
        all_items.append((item["time"], f"{item['text']} (maxsus)"))

    for item in custom_reminders.get_for_date(today_str):
        all_items.append((item["time"], f"{item['text']} (maxsus, bugun)"))

    if not all_items:
        await message.answer(f"<b>{name}</b> uchun reja topilmadi.")
        return

    all_items.sort(key=lambda x: x[0])
    lines = [f"<b>{name} kuni rejasi:</b>\n"]
    for time_str, label in all_items:
        lines.append(f"🕒 {time_str} — {html.escape(label)}")
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


@dp.message(Command("qoshish"))
async def cmd_add_reminder(message: Message, state: FSMContext):
    await state.set_state(AddReminder.choosing_type)
    await message.answer(
        "Yangi eslatma qo'shamiz.\n\n"
        "Qanday turdagi eslatma bo'lsin?\n"
        "1 — Har kuni takrorlanadi\n"
        "2 — Haftaning bitta kunida takrorlanadi\n"
        "3 — Faqat bitta aniq sanada (bir marta)\n\n"
        "Raqamni yozing (1, 2 yoki 3). Bekor qilish uchun /bekor"
    )


@dp.message(Command("bekor"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Bekor qilinadigan jarayon yo'q.")
        return
    await state.clear()
    await message.answer("Bekor qilindi.")


@dp.message(AddReminder.choosing_type)
async def process_type(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, 1, 2 yoki 3 raqamini matn sifatida yozing.")
        return
    choice = message.text.strip()
    if choice == "1":
        await state.update_data(rtype="daily")
        await state.set_state(AddReminder.waiting_time)
        await message.answer("Soatini kiriting (masalan 22:00):")
    elif choice == "2":
        await state.update_data(rtype="weekly")
        await state.set_state(AddReminder.waiting_weekday)
        await message.answer(
            "Qaysi kuni? Yozing: dushanba, seshanba, chorshanba, payshanba, "
            "juma, shanba yoki yakshanba"
        )
    elif choice == "3":
        await state.update_data(rtype="once")
        await state.set_state(AddReminder.waiting_date)
        await message.answer("Sanani kiriting (YYYY-MM-DD, masalan 2026-10-05):")
    else:
        await message.answer("Iltimos, 1, 2 yoki 3 raqamini yozing.")


@dp.message(AddReminder.waiting_weekday)
async def process_weekday(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, kun nomini matn sifatida yozing.")
        return
    text = message.text.strip().lower()
    if text not in WEEKDAY_NAME_TO_NUM:
        await message.answer(
            "Kunni to'g'ri yozing: dushanba, seshanba, chorshanba, payshanba, "
            "juma, shanba yoki yakshanba"
        )
        return
    await state.update_data(weekday=WEEKDAY_NAME_TO_NUM[text])
    await state.set_state(AddReminder.waiting_time)
    await message.answer("Soatini kiriting (masalan 18:00):")


@dp.message(AddReminder.waiting_date)
async def process_date(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, sanani matn sifatida yozing.")
        return
    text = message.text.strip()
    if not DATE_RE.match(text):
        await message.answer("Format noto'g'ri. Iltimos YYYY-MM-DD ko'rinishida yozing (masalan 2026-10-05):")
        return
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Bunday sana mavjud emas. Qaytadan kiriting:")
        return
    await state.update_data(date=text)
    await state.set_state(AddReminder.waiting_time)
    await message.answer("Soatini kiriting (masalan 09:00):")


@dp.message(AddReminder.waiting_time)
async def process_time(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, vaqtni matn sifatida yozing.")
        return
    text = message.text.strip()
    if not TIME_RE.match(text):
        await message.answer("Format noto'g'ri. Iltimos HH:MM ko'rinishida yozing (masalan 09:30):")
        return
    await state.update_data(time=text)
    await state.set_state(AddReminder.waiting_text)
    await message.answer("Endi eslatma matnini yozing (masalan \"Dori ichish\"):")


@dp.message(AddReminder.waiting_text)
async def process_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Iltimos, eslatma matnini yozing.")
        return
    text = message.text.strip()
    data = await state.get_data()
    rtype = data["rtype"]
    time_str = data["time"]

    if rtype == "daily":
        rid = custom_reminders.add_daily(time_str, text)
        desc = f"Har kuni soat {time_str} da"
        schedule_single_custom(rid, time_str, text, once=False)
    elif rtype == "weekly":
        weekday = data["weekday"]
        rid = custom_reminders.add_weekly(weekday, time_str, text)
        desc = f"Har {WEEKDAY_NAMES_UZ[weekday]} kuni soat {time_str} da"
        if weekday == datetime.now(TZ).weekday():
            schedule_single_custom(rid, time_str, text, once=False)
    else:  # once
        date_str = data["date"]
        rid = custom_reminders.add_once(date_str, time_str, text)
        desc = f"{date_str} kuni soat {time_str} da (bir marta)"
        if date_str == datetime.now(TZ).date().isoformat():
            schedule_single_custom(rid, time_str, text, once=True)

    await state.clear()
    await message.answer(
        f"✅ Eslatma qo'shildi (ID: {rid}):\n<b>{html.escape(text)}</b>\n{desc}\n\n"
        "Bu eslatma bugungi/keyingi kunlar uchun avtomatik ishlaydi. "
        "Ro'yxatni ko'rish uchun /royxat, o'chirish uchun /ochirish (masalan: /ochirish 3)."
    )


@dp.message(Command("royxat"))
async def cmd_list_custom(message: Message):
    items = custom_reminders.get_all()
    if not items:
        await message.answer("Hozircha maxsus eslatmalar yo'q. Qo'shish uchun /qoshish")
        return
    lines = ["<b>Maxsus eslatmalar:</b>\n"]
    for item in items:
        if item["type"] == "daily":
            when = f"Har kuni {item['time']}"
        elif item["type"] == "weekly":
            when = f"Har {WEEKDAY_NAMES_UZ[item['weekday']]} {item['time']}"
        else:
            when = f"{item['date']} {item['time']} (bir marta)"
        lines.append(f"#{item['id']} — {html.escape(item['text'])} ({when})")
    lines.append("\nO'chirish uchun: /ochirish (masalan: /ochirish 3)")
    await message.answer("\n".join(lines))


@dp.message(Command("ochirish"))
async def cmd_remove_custom(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().lstrip("#").isdigit():
        await message.answer("To'g'ri formatda yozing: /ochirish 3\n(ID raqamini /royxat orqali ko'rishingiz mumkin)")
        return
    rid = int(args[1].strip().lstrip("#"))
    ok = custom_reminders.remove(rid)
    if ok:
        await message.answer(f"✅ #{rid} eslatma o'chirildi.")
    else:
        await message.answer(f"#{rid} raqamli eslatma topilmadi.")


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
