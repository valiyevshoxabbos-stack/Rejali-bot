"""
Ob-havo moduli (3.1):
- Manba: Open-Meteo API (kalitsiz, CC BY 4.0 "Open-Meteo.com" yozuvi bilan, `elevation` hisobga olingan).
- 75 daqiqalik kesh (60–90 daq TTL), timeout 5 soniya, `asyncio.to_thread` orqali tarmoqni bloklamaydi.
- His etilgan harorat (`apparent_temperature`), shamol (>=15 km/soat qo'lqop ogohlantirishi),
  faol oyna (07:00–21:30) yog'in ehtimoli (>=25% soyabon) va harorat farqi (>=8°C tonggi/kungi bo'linish).
"""

import asyncio
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import messenger
import services as sv

router = Router()


def _kb(day_key: str = "tod") -> InlineKeyboardMarkup:
    other = ("📅 Ertaga", "wx:tom") if day_key == "tod" else ("📅 Bugun", "wx:tod")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=other[0], callback_data=other[1]),
        InlineKeyboardButton(text="🔄 Yangilash", callback_data=f"wx:ref:{day_key}"),
    ]])


def render_weather(info: dict, loc: dict, header: str, compact: bool = False, cached_fallback: bool = False, cached_at: str = "") -> str:
    elev = loc.get("elevation") if loc.get("elevation") is not None else info.get("elevation")
    elev_s = f", {round(elev)} m" if elev is not None else ""
    lines = [f"{header} — <b>{app.esc(loc['city'])}{elev_s}</b>"]

    cur = info.get("current")
    if cur and not compact:
        lines.append(
            f"🌡 Hozir: <b>{round(cur['temp'])}°C</b> (his etiladi: {round(cur['feels'])}°C, "
            f"shamol {round(cur['wind'])} km/s)"
        )

    lines.append(
        f"📊 Kunduzgi (07:00–21:30): <b>{info['temp_min']}°…{info['temp_max']}°C</b> "
        f"(his: {info['feels_min']}°…{info['feels_max']}°C)"
    )

    if info["split_day"]:
        lines.append(f"🌅 Tongda ({info['morn_feels']}°C): {info['morn_cloth']}")
        lines.append(f"☀️ Kunduzi ({info['day_feels']}°C): {info['day_cloth']}")
    else:
        lines.append(f"🧥 Kiyim: {info['cloth']}")

    if info["wind_alert"]:
        lines.append(f"🌬 <b>Shamol kuchli, qo'lqop oling</b> ({info['max_wind']} km/soat)")

    if info["umbrella"]:
        lines.append(f"☂️ <b>Soyabon oling</b> (yog'in {info['max_precip']}%, {info['rain_span']})")
    elif not compact:
        lines.append(f"☀️ Yog'in ehtimoli past ({info['max_precip']}%)")

    if cached_fallback or info.get("cached_fallback"):
        c_at = cached_at or info.get("cached_at", "")
        c_str = f" ({c_at} holatiga)" if c_at else ""
        lines.append(f"\n⚠️ <i>Keshlangan ma'lumot{c_str}</i>")

    lines.append("<i>Manba: Open-Meteo.com</i>")
    return "\n".join(lines)


async def _fetch_async(force: bool = False, on_progress=None) -> dict | None:
    try:
        return await asyncio.to_thread(sv.get_weather_data, force, on_progress)
    except Exception as e:
        app.log.warning(f"Ob-havo ma'lumotini olishda xatolik: {e}")
        return None


async def morning_weather_section() -> str:
    """06:30 ertalabki xulosa uchun ixcham bugungi ob-havo bloki."""
    bundle = await _fetch_async(force=False)
    if not bundle:
        return ""
    info = sv.analyze_weather_for_date(bundle["raw"], app.today())
    if not info:
        return ""
    is_cached = bundle.get("cached_fallback", False)
    c_at = bundle.get("cached_at", "")
    return "\n\n" + render_weather(info, bundle["loc"], "🌦 <b>Bugungi ob-havo</b>", compact=True, cached_fallback=is_cached, cached_at=c_at)


async def evening_tomorrow_section() -> str:
    """21:45 kechki xulosa uchun ertangi ob-havo prognozi."""
    bundle = await _fetch_async(force=False)
    if not bundle:
        return ""
    tom = app.today() + timedelta(days=1)
    info = sv.analyze_weather_for_date(bundle["raw"], tom)
    if not info:
        return ""
    is_cached = bundle.get("cached_fallback", False)
    c_at = bundle.get("cached_at", "")
    return "\n\n" + render_weather(info, bundle["loc"], "🌦 <b>Ertaga ob-havo</b>", compact=True, cached_fallback=is_cached, cached_at=c_at)


async def _build_screen(
    day_key: str = "tod",
    force: bool = False,
    existing_msg=None,
    user_msg=None,
) -> tuple[str, InlineKeyboardMarkup | None, Message | None]:
    anim_msg = existing_msg

    async def on_progress(attempt: int, total: int, wait_sec: int):
        nonlocal anim_msg
        bar = sv.PROGRESS_BARS.get(attempt, "[▓▓░░░]")
        prog_text = f"⏳ Server bilan bog'lanilmoqda...\n{bar} {attempt}/{total}-urinish ({wait_sec} soniya kutilmoqda...)"
        try:
            if anim_msg is not None:
                await app.bot.edit_message_text(
                    prog_text, chat_id=app.CHAT_ID, message_id=anim_msg.message_id
                )
            elif user_msg is not None:
                anim_msg = await messenger.send(prog_text, "info", bind_trigger=False)
        except Exception:
            pass

    bundle = await _fetch_async(force=force, on_progress=on_progress)
    if not bundle:
        return "⚠️ Ob-havo xizmati vaqtincha javob bermayapti. Birozdan so'ng qayta urinib ko'ring.", None, anim_msg
    target: date = app.today() if day_key == "tod" else app.today() + timedelta(days=1)
    title = "🌦 <b>Bugungi ob-havo</b>" if day_key == "tod" else "🌦 <b>Ertangi ob-havo</b>"
    info = sv.analyze_weather_for_date(bundle["raw"], target)
    if not info:
        return "⚠️ Tanlangan kun uchun ob-havo ma'lumoti topilmadi.", None, anim_msg
    is_cached = bundle.get("cached_fallback", False)
    c_at = bundle.get("cached_at", "")
    text = render_weather(info, bundle["loc"], title, compact=False, cached_fallback=is_cached, cached_at=c_at)
    return text, _kb(day_key), anim_msg


async def entry(message: Message):
    """Doimiy menyudagi '🌦 Ob-havo' tugmasi kirish nuqtasi (keshdan oladi)."""
    text, kb, anim_msg = await _build_screen("tod", force=False, user_msg=message)
    if anim_msg is not None:
        try:
            await app.bot.edit_message_text(
                text, chat_id=app.CHAT_ID, message_id=anim_msg.message_id, reply_markup=messenger.with_delete(kb)
            )
            return
        except Exception:
            pass
    await messenger.send(text, "info", kb)


@router.message(Command("obhavo"))
async def cmd_weather(message: Message):
    await entry(message)


@router.message(Command("balandlik"))
async def cmd_elevation(message: Message):
    """Tog'li hudud uchun dengiz sathidan balandlikni (metrda) sozlash: /balandlik 582"""
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        loc = sv.weather_location()
        cur = f"{loc['elevation']} m" if loc["elevation"] is not None else "avto (DEM)"
        await messenger.send(
            f"Hozirgi balandlik: <b>{cur}</b>\nO'zgartirish: <code>/balandlik 582</code>",
            "info",
        )
        return
    val = max(-100, min(5500, int(parts[1])))
    db.set_setting("elevation", str(val))
    sv.clear_weather_cache()
    await messenger.send(f"✅ Balandlik saqlandi: <b>{val} m</b>", "confirm")


@router.callback_query(F.data.in_({"wx:tod", "wx:tom"}))
async def cb_switch_day(cb: CallbackQuery):
    day_key = cb.data.split(":")[1]
    await cb.answer()
    text, kb, anim_msg = await _build_screen(day_key, force=False, existing_msg=cb.message)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("wx:ref:"))
async def cb_refresh(cb: CallbackQuery):
    day_key = cb.data.split(":")[2]
    await cb.answer("Yangilandi")
    text, kb, anim_msg = await _build_screen(day_key, force=True, existing_msg=cb.message)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))
