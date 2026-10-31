"""
Sozlamalar (3.0.1): joylashuv, eslatma rejimlari, uyqu, vazn, pauza,
ma'lumotlar (zaxira, tiklash, CSV eksport, hammasini o'chirish).
Xabarlarni tozalash ekrani feat_cleanup.py da, zaxiradan tiklash oqimi feat_restore.py da.
"""

import csv
import io
import os
import tempfile

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_common as common
import feat_journal as journal
import feat_reminders as reminders
import feat_status as status
import messenger
import services as sv

router = Router()
BACK = InlineKeyboardButton(text="⬅️ Sozlamalar", callback_data="se:home")
WIPE_WORD = "OCHIRISH"


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📍 Joylashuv", "se:loc"), btn("🔔 Eslatmalar", "se:rem")],
        [btn("🧹 Xabarlarni tozalash", "se:clean"), btn("😴 Uyqu", "se:sleep")],
        [btn("⚖️ Vazn", "se:weight"), btn("⏸ Pauza", "se:pause")],
        [btn("🗂 Ma'lumotlar", "se:data"), btn("ℹ️ Holat", "se:status")],
    ])


HOME_TEXT = "⚙️ <b>Sozlamalar</b>"


async def send_home():
    await messenger.send(HOME_TEXT, "info", home_kb())


async def nav(cb: CallbackQuery, text: str, rows: list):
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.message(Command("sozlamalar"))
async def cmd_settings(message: Message):
    await send_home()


@router.callback_query(F.data == "se:home")
async def cb_home(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(HOME_TEXT, reply_markup=messenger.with_delete(home_kb()))


@router.callback_query(F.data == "se:status")
async def cb_status(cb: CallbackQuery):
    await nav(cb, status.build_text(), [[BACK]])


# ------------------------------------------------------------------ joylashuv

class City(StatesGroup):
    waiting = State()


@router.callback_query(F.data == "se:loc")
async def cb_loc(cb: CallbackQuery):
    lat, lon = db.get_setting("lat"), db.get_setting("lon")
    coords = f"{lat}, {lon}" if lat and lon else "yuborilmagan"
    await nav(cb, f"📍 <b>Joylashuv</b>\n\nShahar (namoz vaqtlari uchun): <b>{app.esc(sv.get_city())}</b>\n"
                  f"Koordinata (ob-havo uchun, 3.1): {coords}",
              [[btn("🏙 Shaharni o'zgartirish", "se:city")], [btn("📍 Joylashuvni yuborish", "se:geo")], [BACK]])


@router.callback_query(F.data == "se:city")
async def cb_city(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(City.waiting)
    await common.ask(state, "Shahar nomini yozing (lotin harflarda, masalan <code>Tashkent</code>):")


@router.message(City.waiting)
async def process_city(message: Message, state: FSMContext):
    await common.track_user(state, message)
    name = (message.text or "").strip()
    if not name or len(name) > 60:
        await messenger.send("Shahar nomini yozing.", "confirm", bind_trigger=False)
        return
    db.set_setting("city", name)
    await common.clear_prompts(state)
    await state.clear()
    import jobs
    await jobs.schedule_today()
    await messenger.send(f"✅ Shahar: <b>{app.esc(name)}</b>. Namoz vaqtlari yangilandi.", "confirm", bind_trigger=False)


@router.message(Command("shahar"))
async def cmd_city(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await messenger.send(f"Hozirgi shahar: <b>{app.esc(sv.get_city())}</b>\nO'zgartirish: /shahar Tashkent", "info")
        return
    name = parts[1].strip()
    db.set_setting("city", name)
    import jobs
    await jobs.schedule_today()
    await messenger.send(f"✅ Shahar: <b>{app.esc(name)}</b>", "confirm")


@router.callback_query(F.data == "se:geo")
async def cb_geo(cb: CallbackQuery):
    await cb.answer()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Joylashuvni yuborish", request_location=True)],
                  [KeyboardButton(text="⬅️ Bekor")]], resize_keyboard=True, one_time_keyboard=True)
    await messenger.send("Pastdagi tugma orqali joylashuvingizni yuboring (faqat koordinata saqlanadi).",
                         "flow", kb, deletable=False, bind_trigger=False)


@router.message(F.location)
async def got_location(message: Message):
    db.set_setting("lat", f"{message.location.latitude:.4f}")
    db.set_setting("lon", f"{message.location.longitude:.4f}")
    await messenger.send("✅ Joylashuv saqlandi.", "confirm")
    await messenger.send("Asosiy menyu:", "system", common.main_kb(), deletable=False, bind_trigger=False)


@router.message(F.text == "⬅️ Bekor")
async def geo_cancel(message: Message):
    await messenger.send("Asosiy menyu:", "system", common.main_kb(), deletable=False, bind_trigger=False)


# ------------------------------------------------------------------ eslatma rejimlari

def _groups() -> list[dict]:
    std = db.q("SELECT slug, MIN(id) AS id FROM reminders WHERE source='standard' AND active=1 AND slug IS NOT NULL "
               "GROUP BY slug ORDER BY MIN(id)")
    out = []
    for g in std:
        r = db.q1("SELECT title, mode, off_until FROM reminders WHERE id = ?", (g["id"],))
        out.append({"key": f"s:{g['slug']}", **r})
    for r in db.q("SELECT id, title, mode, off_until FROM reminders WHERE source='custom' AND active=1 ORDER BY id"):
        out.append({"key": f"c:{r['id']}", "title": r["title"], "mode": r["mode"], "off_until": r["off_until"]})
    return out


def _group(key: str) -> dict | None:
    return next((g for g in _groups() if g["key"] == key), None)


@router.callback_query(F.data == "se:rem")
async def cb_rem(cb: CallbackQuery):
    rows = []
    for g in _groups():
        until = f" ({g['off_until'][5:]} gacha)" if g["mode"] == "off" and g["off_until"] else ""
        rows.append([btn(f"{sv.MODE_ICON[g['mode']]} {g['title'][:30]}{until}", f"se:rm:{g['key']}")])
    rows.append([BACK])
    await nav(cb, "🔔 <b>Eslatmalar</b>\n\n✅ majburiy · 💡 ixtiyoriy (yumshoq taklif, foizga ta'sir qilmaydi) · "
                  "🔕 o'chirilgan.\nVaqtni o'zgartirish: /vaqt, ballni: /ball", rows)


@router.callback_query(F.data.startswith("se:rm:"))
async def cb_rem_item(cb: CallbackQuery):
    key = cb.data[len("se:rm:"):]
    g = _group(key)
    if not g:
        await cb.answer("Topilmadi")
        return
    await nav(cb, f"<b>{app.esc(g['title'])}</b>\nHozir: {sv.MODE_ICON[g['mode']]} {sv.MODE_NAME[g['mode']]}",
              [[btn("✅ Majburiy", f"se:rs:{key}:required"), btn("💡 Ixtiyoriy", f"se:rs:{key}:optional")],
               [btn("🔕 O'chirish...", f"se:ro:{key}")], [btn("⬅️ Orqaga", "se:rem")]])


async def _apply_mode(cb: CallbackQuery, key: str, mode: str, days: int = 0):
    reminders.set_mode(key, mode, days)
    await cb.answer("Saqlandi")
    import jobs
    await jobs.schedule_today()
    today = app.today()
    if db.get_setting(f"journal_{today.isoformat()}"):
        await journal.sync(today)
    rows = []
    for g in _groups():
        until = f" ({g['off_until'][5:]} gacha)" if g["mode"] == "off" and g["off_until"] else ""
        rows.append([btn(f"{sv.MODE_ICON[g['mode']]} {g['title'][:30]}{until}", f"se:rm:{g['key']}")])
    rows.append([BACK])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.edit_text(
        "🔔 <b>Eslatmalar</b>\n\n✅ majburiy · 💡 ixtiyoriy (yumshoq taklif, foizga ta'sir qilmaydi) · "
        "🔕 o'chirilgan.\nVaqtni o'zgartirish: /vaqt, ballni: /ball",
        reply_markup=messenger.with_delete(kb),
    )


@router.callback_query(F.data.regexp(r"^se:rs:(s|c):[^:]+:(required|optional)$"))
async def cb_rem_set(cb: CallbackQuery):
    rest = cb.data[len("se:rs:"):]
    key, mode = rest.rsplit(":", 1)
    await _apply_mode(cb, key, mode)


@router.callback_query(F.data.startswith("se:ro:"))
async def cb_rem_off(cb: CallbackQuery):
    key = cb.data[len("se:ro:"):]
    await nav(cb, "Qancha muddatga o'chiramiz? Muddat tugagach o'zi qaytadi va bir marta xabar beradi.",
              [[btn("1 hafta", f"se:rd:{key}:7"), btn("2 hafta", f"se:rd:{key}:14")],
               [btn("1 oy", f"se:rd:{key}:30"), btn("Muddatsiz", f"se:rd:{key}:0")],
               [btn("⬅️ Orqaga", f"se:rm:{key}")]])


@router.callback_query(F.data.regexp(r"^se:rd:(s|c):[^:]+:\d+$"))
async def cb_rem_off_days(cb: CallbackQuery):
    rest = cb.data[len("se:rd:"):]
    key, days = rest.rsplit(":", 1)
    await _apply_mode(cb, key, "off", int(days))


# ------------------------------------------------------------------ uyqu, vazn

@router.callback_query(F.data == "se:sleep")
async def cb_sleep(cb: CallbackQuery):
    lat = int(db.get_setting("sleep_latency", "20"))
    await nav(cb, f"😴 <b>Uyqu</b>\n\nYotishni tasdiqlagandan keyin uyqu hisobi <b>{lat} daqiqa</b>dan boshlanadi.",
              [[btn("−5", "se:sl:-5"), btn("+5", "se:sl:5")], [BACK]])


@router.callback_query(F.data.regexp(r"^se:sl:-?\d+$"))
async def cb_sleep_change(cb: CallbackQuery):
    new = max(5, min(45, int(db.get_setting("sleep_latency", "20")) + int(cb.data.split(":")[2])))
    db.set_setting("sleep_latency", str(new))
    await cb_sleep(cb)


@router.callback_query(F.data == "se:weight")
async def cb_weight(cb: CallbackQuery):
    on = db.get_setting("weight_weekly", "1") == "1"
    await nav(cb, f"⚖️ <b>Vazn</b>\n\nHaftalik so'rov (dushanba 11:00): <b>{'yoqilgan' if on else 'o‘chirilgan'}</b>\n"
                  "Istalgan vaqtda: /vazn 72,4",
              [[btn("🔕 O'chirish" if on else "🔔 Yoqish", "se:wt")], [BACK]])


@router.callback_query(F.data == "se:wt")
async def cb_weight_toggle(cb: CallbackQuery):
    db.set_setting("weight_weekly", "0" if db.get_setting("weight_weekly", "1") == "1" else "1")
    await cb_weight(cb)


# ------------------------------------------------------------------ pauza

def pause_text_rows():
    p = sv.pause_until()
    active = p is not None and p >= app.today()
    text = ("⏸ <b>Pauza rejimi</b>\n\nKasallik yoki safar vaqtida eslatmalar to'xtaydi va statistika buzilmaydi. "
            "Namoz vaqtlari ishlashda davom etadi.\n\n"
            + (f"Hozir: <b>{p.strftime('%d.%m.%Y')} gacha</b> pauzada." if active else "Hozir: pauza yo'q."))
    rows = [[btn("1 kun", "se:pz:1"), btn("3 kun", "se:pz:3")], [btn("1 hafta", "se:pz:7"), btn("2 hafta", "se:pz:14")]]
    if active:
        rows.append([btn("▶️ Pauzani tugatish", "se:pz:0")])
    return text, rows


@router.callback_query(F.data == "pz:open")
async def cb_pause_open(cb: CallbackQuery):
    await cb.answer()
    text, rows = pause_text_rows()
    await messenger.send(text, "info", InlineKeyboardMarkup(inline_keyboard=rows + [[BACK]]), bind_trigger=False)


@router.callback_query(F.data == "se:pause")
async def cb_pause(cb: CallbackQuery):
    text, rows = pause_text_rows()
    await nav(cb, text, rows + [[BACK]])


@router.callback_query(F.data.regexp(r"^se:pz:\d+$"))
async def cb_pause_set(cb: CallbackQuery):
    days = int(cb.data.split(":")[2])
    sv.end_pause() if days == 0 else sv.set_pause(days)
    await cb.answer("Saqlandi")
    import jobs
    await jobs.schedule_today()
    today = app.today()
    if db.get_setting(f"journal_{today.isoformat()}"):
        await journal.sync(today)
    text, rows = pause_text_rows()
    kb = InlineKeyboardMarkup(inline_keyboard=rows + [[BACK]])
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


# ------------------------------------------------------------------ ma'lumotlar

class Wipe(StatesGroup):
    waiting = State()


def make_backup() -> str:
    """Bazaning izchil nusxasini vaqtincha faylga yozadi (sqlite backup API)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.backup_to(db.path(), tmp)
    return tmp


async def send_backup(caption: str = "📤 Zaxira nusxa"):
    tmp = make_backup()
    try:
        name = f"altronbek_{app.today().isoformat()}.db"
        await app.bot.send_document(app.CHAT_ID, FSInputFile(tmp, filename=name), caption=caption)
        db.set_setting("last_backup", app.now().strftime("%d.%m.%Y %H:%M"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _csv(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


@router.callback_query(F.data == "se:data")
async def cb_data(cb: CallbackQuery):
    auto = db.get_setting("auto_backup", "1") == "1"
    await nav(
        cb,
        f"🗂 <b>Ma'lumotlar</b>\n\nOxirgi zaxira: {app.esc(db.get_setting('last_backup', 'hali yo‘q'))}\n"
        f"Haftalik avtomatik zaxira (yakshanba 21:00): <b>{'yoqilgan' if auto else 'o‘chirilgan'}</b>\n"
        "Zaxiradan tiklash: /tiklash",
        [
            [btn("📤 Zaxira nusxa", "se:bk"), btn("📥 Zaxiradan tiklash", "rs:start")],
            [btn("📊 CSV eksport", "se:csv")],
            [btn("🔕 Avto-zaxirani o'chirish" if auto else "🔔 Avto-zaxirani yoqish", "se:abk")],
            [btn("🗑 Hammasini o'chirish", "se:wipe")],
            [BACK],
        ],
    )


@router.callback_query(F.data == "se:bk")
async def cb_backup(cb: CallbackQuery):
    await cb.answer("Yuborilmoqda...")
    await send_backup()


@router.callback_query(F.data == "se:abk")
async def cb_auto_backup(cb: CallbackQuery):
    db.set_setting("auto_backup", "0" if db.get_setting("auto_backup", "1") == "1" else "1")
    await cb_data(cb)


@router.callback_query(F.data == "se:csv")
async def cb_csv(cb: CallbackQuery):
    await cb.answer("Yuborilmoqda...")
    w = db.q("SELECT at, kg, is_primary FROM weight_log ORDER BY at")
    b = db.q("SELECT name, day, month, year FROM birthdays ORDER BY month, day, name")
    s = db.q("SELECT bed_confirmed_at, sleep_start, wake_at, duration_min, wake_source FROM sleep_log ORDER BY id")
    o = db.q("SELECT date, title, status, late_minutes, points FROM occurrences WHERE prayer IS NULL ORDER BY date, scheduled_at")
    n = db.q("SELECT date, text, created_at FROM notes ORDER BY id")
    files = [
        ("vazn.csv", ["vaqt", "kg", "asosiy"], [[r["at"], r["kg"], r["is_primary"]] for r in w]),
        ("tugilgan_kunlar.csv", ["ism", "kun", "oy", "yil"], [[r["name"], r["day"], r["month"], r["year"] or ""] for r in b]),
        ("uyqu.csv", ["yotish_tasdiq", "uyqu_boshi", "uyg'onish", "daqiqa", "manba"],
         [[r["bed_confirmed_at"], r["sleep_start"], r["wake_at"], r["duration_min"], r["wake_source"]] for r in s]),
        ("reja.csv", ["sana", "nomi", "holat", "kechikish_daq", "ball"],
         [[r["date"], r["title"], r["status"], r["late_minutes"], r["points"]] for r in o]),
        ("izohlar.csv", ["sana", "matn", "yaratilgan"], [[r["date"], r["text"], r["created_at"]] for r in n]),
    ]
    for name, headers, rows in files:
        await app.bot.send_document(app.CHAT_ID, BufferedInputFile(_csv(headers, rows), filename=name),
                                    caption=f"{name}: {len(rows)} qator")


@router.callback_query(F.data == "se:wipe")
async def cb_wipe(cb: CallbackQuery):
    await nav(cb, "⚠️ <b>Hammasini o'chirish</b>\n\nReja tarixi, ballar, uyqu, vazn, tug'ilgan kunlar, qazolar va "
                  "maxsus eslatmalar o'chadi. Sozlamalar (shahar va h.k.) va standart reja qoladi.\n"
                  "Bu amalni qaytarib bo'lmaydi. Avval zaxira nusxa oling.",
              [[btn("Davom etish", "se:wipe2")], [btn("❌ Bekor", "se:data")]])


@router.callback_query(F.data == "se:wipe2")
async def cb_wipe2(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(Wipe.waiting)
    await common.ask(state, f"Tasdiqlash uchun <code>{WIPE_WORD}</code> deb yozing:")


@router.message(Wipe.waiting)
async def process_wipe(message: Message, state: FSMContext):
    await common.track_user(state, message)
    if (message.text or "").strip() != WIPE_WORD:
        await messenger.send(f"Tasdiqlanmadi. <code>{WIPE_WORD}</code> deb yozing yoki ❌ Bekor bosing.",
                             "confirm", bind_trigger=False)
        return
    for table in ("occurrences", "qazo", "notes", "checkpoint_status", "overrides", "sleep_log", "weight_log",
                  "birthdays", "birthday_notices", "msg_log", "cleanup_events", "checkpoints"):
        db.ex(f"DELETE FROM {table}")
    db.ex("DELETE FROM reminders WHERE source = 'custom'")
    db.ex("UPDATE reminders SET mode = 'required', off_until = NULL")
    db.ex("DELETE FROM settings WHERE key LIKE 'journal_%' OR key LIKE 'trg_%'")
    for key in ("pause_until", "weight_prompt_until", "weight_pending"):
        db.del_setting(key)
    await common.clear_prompts(state)
    await state.clear()
    import jobs
    await jobs.schedule_today()
    await messenger.send("🗑 Barcha ma'lumotlar o'chirildi.", "confirm", bind_trigger=False)


async def weekly_backup():
    if db.get_setting("auto_backup", "1") == "1":
        try:
            await send_backup("📤 Haftalik avtomatik zaxira")
        except Exception as e:
            app.log.error(f"Zaxirada xatolik: {e}")


def register_jobs(scheduler):
    scheduler.add_job(weekly_backup, CronTrigger(day_of_week="sun", hour=21, minute=0),
                      id="weekly_backup", replace_existing=True)
