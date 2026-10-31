"""
Uyqu ritmi (3.0.1):
- "Yotish vaqti" eslatmasi tasdiqlangach, uyqu boshi = tasdiqlangan payt + kechikish (standart 20 daq).
- Bomdod vaqtida "uyg'ondim" xabari: agar bugun uyg'onish allaqachon qayd etilgan bo'lsa yuborilmaydi;
  eski tugma ikkinchi marta bosilsa dublikat yaratmaydi.
- Qo'lda tungi uyqu kiritish (/uyqu_kiritish yoki menyu):
  `18-19.10.2026 23:30-5:04`, `18.10 23:30 - 19.10 05:04`, `23:30-05:04` (3–16 soat, ustma-ust tekshiruvi bilan).
"""

import re
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import feat_common as common
import messenger

router = Router()
MIN_SLEEP_MIN = 3 * 60
MAX_SLEEP_MIN = 16 * 60

_RE_RANGE_DAY = re.compile(
    r"^(\d{1,2})\s*-\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$"
)
_RE_TWO_DATES = re.compile(
    r"^(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\s+(\d{1,2}:\d{2})\s*-\s*"
    r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\s+(\d{1,2}:\d{2})$"
)
_RE_TIMES_ONLY = re.compile(r"^(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$")


class WakeTime(StatesGroup):
    waiting = State()


class ManualSleep(StatesGroup):
    waiting = State()
    confirm_replace = State()


def latency() -> int:
    return int(db.get_setting("sleep_latency", "20"))


def fmt_min(m: int | None) -> str:
    if m is None:
        return "ma'lumot yo'q"
    return f"{m // 60} s {m % 60:02d} daq"


def woke_today(d: date | None = None) -> bool:
    """Shu sana uchun uyg'onish allaqachon qayd etilganmi."""
    d = d or app.today()
    start_iso = datetime(d.year, d.month, d.day, 0, 0, tzinfo=app.TZ).isoformat()
    end_d = d + timedelta(days=1)
    end_iso = datetime(end_d.year, end_d.month, end_d.day, 0, 0, tzinfo=app.TZ).isoformat()
    row = db.q1("SELECT 1 FROM sleep_log WHERE wake_at >= ? AND wake_at < ? LIMIT 1", (start_iso, end_iso))
    return row is not None


def on_bedtime_confirmed(confirmed_at: datetime | None = None) -> datetime:
    """Yotish tasdiqlandi. Shu tun ochiq yozuv bo'lsa yangilaydi, bo'lmasa yangi ochadi."""
    confirmed_at = confirmed_at or app.now()
    start = confirmed_at + timedelta(minutes=latency())
    open_row = db.q1("SELECT * FROM sleep_log WHERE wake_at IS NULL AND sleep_start IS NOT NULL ORDER BY id DESC")
    if open_row:
        prev_start = datetime.fromisoformat(open_row["sleep_start"])
        if abs((start - prev_start).total_seconds()) <= 12 * 3600:
            db.ex("UPDATE sleep_log SET bed_confirmed_at = ?, sleep_start = ? WHERE id = ?",
                  (confirmed_at.isoformat(), start.isoformat(), open_row["id"]))
            return start
    db.ins("INSERT INTO sleep_log (bed_confirmed_at, sleep_start) VALUES (?,?)",
           (confirmed_at.isoformat(), start.isoformat()))
    return start


def finish_sleep(wake_at: datetime, source: str) -> dict:
    """Uyg'onish vaqtini qayd etadi. {'duration': daq|None} yoki {'error': matn}"""
    if woke_today(wake_at.date()):
        return {"already": True}
    open_row = db.q1(
        "SELECT * FROM sleep_log WHERE wake_at IS NULL AND sleep_start IS NOT NULL ORDER BY id DESC")
    if open_row:
        start = datetime.fromisoformat(open_row["sleep_start"])
        minutes = int((wake_at - start).total_seconds() // 60)
        if 0 < minutes <= MAX_SLEEP_MIN:
            db.ex("UPDATE sleep_log SET wake_at=?, duration_min=?, wake_source=? WHERE id=?",
                  (wake_at.isoformat(), minutes, source, open_row["id"]))
            return {"duration": minutes, "start": start}
        if minutes <= 0:
            return {"error": "Uyg'onish vaqti uyqu boshlanishidan oldin chiqdi. Vaqtni qayta tekshiring."}
    db.ins("INSERT INTO sleep_log (wake_at, wake_source) VALUES (?,?)", (wake_at.isoformat(), source))
    return {"duration": None}


def wake_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☀️ Hozir uyg'ondim", callback_data="wk:0")],
        [InlineKeyboardButton(text="−15 daq", callback_data="wk:15"),
         InlineKeyboardButton(text="−30 daq", callback_data="wk:30"),
         InlineKeyboardButton(text="−1 soat", callback_data="wk:60")],
        [InlineKeyboardButton(text="🕐 Vaqtni yozaman", callback_data="wk:manual")],
    ])


async def send_wake_prompt():
    if woke_today(app.today()):
        return
    await messenger.send("☀️ <b>Xayrli tong!</b> Qachon uyg'ondingiz?", "wake", wake_keyboard(),
                         actionable=True, bind_trigger=False)


def _result_text(res: dict, wake_at: datetime) -> str:
    if "error" in res:
        return "⚠️ " + res["error"]
    if res["duration"] is None:
        return f"☀️ Uyg'onish: {wake_at.strftime('%H:%M')}. Kecha yotish belgilanmagan, uyqu hisoblanmadi."
    return (f"☀️ Uyg'onish: {wake_at.strftime('%H:%M')}\n😴 Uyqu: <b>{fmt_min(res['duration'])}</b> "
            f"({res['start'].strftime('%H:%M')} dan)")


@router.callback_query(F.data.startswith("wk:"))
async def cb_wake(cb: CallbackQuery, state: FSMContext):
    if woke_today(app.today()):
        await cb.answer("Bugungi uyg'onish allaqachon qayd etilgan", show_alert=True)
        await messenger.edit_result(cb.message, "☀️ Bugungi uyg'onish allaqachon qayd etilgan.", "reminder_done")
        return
    arg = cb.data.split(":")[1]
    if arg == "manual":
        await cb.answer()
        await state.clear()
        await state.set_state(WakeTime.waiting)
        prompt = await common.ask(state, "Uyg'ongan vaqtni yozing (masalan <code>05:40</code>):")
        await state.update_data(wake_msg=cb.message.message_id, prompts=[prompt.message_id] if prompt else [])
        return
    wake_at = app.now() - timedelta(minutes=int(arg))
    res = finish_sleep(wake_at, "tap" if arg == "0" else f"tap-{arg}")
    if res.get("already"):
        await cb.answer("Uyg'onish allaqachon qayd etilgan", show_alert=True)
        return
    if "error" in res:
        await cb.answer(res["error"], show_alert=True)
        return
    await cb.answer()
    await messenger.edit_result(cb.message, _result_text(res, wake_at), "reminder_done")


@router.message(WakeTime.waiting)
async def process_wake_time(message: Message, state: FSMContext):
    await common.track_user(state, message)
    t = (message.text or "").strip()
    try:
        h, m = map(int, t.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        wake_at = app.now().replace(hour=h, minute=m, second=0, microsecond=0)
    except ValueError:
        await messenger.send("Format: <code>HH:MM</code> (masalan 05:40)", "confirm", bind_trigger=False)
        return
    if wake_at > app.now():
        await messenger.send("Bu vaqt hali kelmagan. Uyg'ongan vaqtni yozing.", "confirm", bind_trigger=False)
        return
    res = finish_sleep(wake_at, "manual")
    if res.get("already"):
        await common.clear_prompts(state)
        await state.clear()
        await messenger.send("☀️ Bugungi uyg'onish allaqachon qayd etilgan.", "confirm", bind_trigger=False)
        return
    if "error" in res:
        await messenger.send("⚠️ " + res["error"], "confirm", bind_trigger=False)
        return
    data = await state.get_data()
    await common.clear_prompts(state)
    await state.clear()
    text = _result_text(res, wake_at)
    if data.get("wake_msg"):
        await messenger.edit_result_by_id(data["wake_msg"], text)
    else:
        await messenger.send(text, "reminder_done", bind_trigger=False)


# ------------------------------------------------------------------ qo'lda tungi uyqu kiritish (/uyqu_kiritish)

def _parse_hm(s: str) -> tuple[int, int] | None:
    try:
        h, m = map(int, s.split(":"))
    except ValueError:
        return None
    return (h, m) if (0 <= h <= 23 and 0 <= m <= 59) else None


def _resolve_date(day: int, month: int, year_s: str | None, ref_today: date) -> date | None:
    if year_s:
        try:
            return date(int(year_s), month, day)
        except ValueError:
            return None
    for yr in (ref_today.year, ref_today.year - 1):
        try:
            d = date(yr, month, day)
        except ValueError:
            continue
        if (d - ref_today).days > 60:
            continue
        return d
    return None


def parse_manual_sleep(raw: str, current: datetime | None = None) -> dict:
    """
    Qo'lda tungi uyqu matnini tahlil qiladi.
    Qaytaradi: {'start': dt, 'wake': dt, 'minutes': int, 'overlaps': [id...]} yoki {'error': str}.
    """
    current = current or app.now()
    ref_today = current.date()
    norm = re.sub(r"[–—]", "-", (raw or "").strip())

    start_date = wake_date = None
    hm1 = hm2 = None

    m = _RE_RANGE_DAY.match(norm)
    if m:
        d1, d2, mo, yr_s, t1, t2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4), m.group(5), m.group(6)
        wake_date = _resolve_date(d2, mo, yr_s, ref_today)
        if wake_date:
            try:
                if d1 > d2:
                    prev_m = 12 if mo == 1 else mo - 1
                    prev_y = wake_date.year - 1 if mo == 1 else wake_date.year
                    start_date = date(prev_y, prev_m, d1)
                else:
                    start_date = date(wake_date.year, mo, d1)
            except ValueError:
                start_date = None
        hm1, hm2 = _parse_hm(t1), _parse_hm(t2)
    else:
        m = _RE_TWO_DATES.match(norm)
        if m:
            d1, mo1, yr1 = int(m.group(1)), int(m.group(2)), m.group(3)
            t1 = m.group(4)
            d2, mo2, yr2 = int(m.group(5)), int(m.group(6)), m.group(7)
            t2 = m.group(8)
            wake_date = _resolve_date(d2, mo2, yr2, ref_today)
            if wake_date:
                if yr1:
                    start_date = _resolve_date(d1, mo1, yr1, ref_today)
                else:
                    try:
                        cand = date(wake_date.year, mo1, d1)
                        if cand > wake_date:
                            cand = date(wake_date.year - 1, mo1, d1)
                        start_date = cand
                    except ValueError:
                        start_date = None
            hm1, hm2 = _parse_hm(t1), _parse_hm(t2)
        else:
            m = _RE_TIMES_ONLY.match(norm)
            if m:
                hm1, hm2 = _parse_hm(m.group(1)), _parse_hm(m.group(2))
                if hm1 and hm2:
                    wake_date = ref_today
                    start_date = ref_today - timedelta(days=1) if hm1 > hm2 else ref_today

    if not (start_date and wake_date and hm1 and hm2):
        return {
            "error": (
                "Format tushunarsiz. Misollar:\n"
                "• <code>23:30-05:04</code> (oxirgi tun)\n"
                "• <code>18-19.10.2026 23:30-05:04</code>\n"
                "• <code>18.10 23:30 - 19.10 05:04</code>"
            )
        }

    start_dt = datetime(start_date.year, start_date.month, start_date.day, hm1[0], hm1[1], tzinfo=app.TZ)
    wake_dt = datetime(wake_date.year, wake_date.month, wake_date.day, hm2[0], hm2[1], tzinfo=app.TZ)

    if wake_dt > current or start_dt > current:
        return {"error": "Kelajakdagi sana yoki vaqt qabul qilinmaydi."}

    minutes = int((wake_dt - start_dt).total_seconds() // 60)
    if minutes <= 0:
        return {"error": "Uyg'onish vaqti uxlash vaqtidan keyin bo'lishi kerak."}
    if minutes < MIN_SLEEP_MIN:
        return {"error": "3 soatdan qisqa uyqu tungi uyquga kirmaydi (qisqa uyqu keyingi versiyada qo'shiladi)."}
    if minutes > MAX_SLEEP_MIN:
        return {"error": "16 soatdan uzun uyqu qabul qilinmaydi. Vaqtni tekshiring."}

    overlaps = _find_overlaps(start_dt, wake_dt)
    return {"start": start_dt, "wake": wake_dt, "minutes": minutes, "overlaps": overlaps}


def _find_overlaps(start_dt: datetime, wake_dt: datetime) -> list[int]:
    s_iso, w_iso = start_dt.isoformat(), wake_dt.isoformat()
    w_day = wake_dt.date().isoformat()
    ids = []
    for r in db.q("SELECT id, sleep_start, wake_at FROM sleep_log WHERE wake_at IS NOT NULL"):
        if r["sleep_start"]:
            if r["sleep_start"] < w_iso and r["wake_at"] > s_iso:
                ids.append(r["id"])
        elif r["wake_at"][:10] == w_day:
            ids.append(r["id"])
    return ids


def save_manual_sleep(start_dt: datetime, wake_dt: datetime, minutes: int, replace_ids: list[int] | None = None):
    for rid in (replace_ids or []):
        db.ex("DELETE FROM sleep_log WHERE id = ?", (rid,))
    # Shu tunning ochiq sikli bo'lsa yopamiz (osilib qolmasligi uchun)
    for op in db.q("SELECT id, sleep_start FROM sleep_log WHERE wake_at IS NULL AND sleep_start IS NOT NULL"):
        op_start = datetime.fromisoformat(op["sleep_start"])
        if abs((wake_dt - op_start).total_seconds()) <= 20 * 3600:
            db.ex("DELETE FROM sleep_log WHERE id = ?", (op["id"],))
    db.ins(
        "INSERT INTO sleep_log (bed_confirmed_at, sleep_start, wake_at, duration_min, wake_source) "
        "VALUES (NULL, ?, ?, ?, 'manual-entry')",
        (start_dt.isoformat(), wake_dt.isoformat(), minutes),
    )
    if wake_dt.date() == app.today():
        job = app.scheduler.get_job("wake_prompt")
        if job:
            job.remove()


async def start_manual_entry(state: FSMContext):
    await state.clear()
    await state.set_state(ManualSleep.waiting)
    await common.ask(
        state,
        "✍️ <b>Tungi uyquni kiritish</b>\n\n"
        "Quyidagi ko'rinishlardan birida yozing:\n"
        "• <code>23:30-05:04</code> — oxirgi tun\n"
        "• <code>18-19.10.2026 23:30-05:04</code>\n"
        "• <code>18.10 23:30 - 19.10 05:04</code>",
    )


@router.message(Command("uyqu_kiritish"))
async def cmd_manual_sleep(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        await _handle_manual_text(message, state, parts[1].strip(), direct=True)
        return
    await start_manual_entry(state)


@router.callback_query(F.data == "ms:start")
async def cb_manual_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await start_manual_entry(state)


@router.message(ManualSleep.waiting)
async def process_manual_sleep(message: Message, state: FSMContext):
    await common.track_user(state, message)
    await _handle_manual_text(message, state, message.text or "", direct=False)


async def _handle_manual_text(message: Message, state: FSMContext, text: str, direct: bool):
    parsed = parse_manual_sleep(text)
    if "error" in parsed:
        await messenger.send("⚠️ " + parsed["error"], "confirm", bind_trigger=direct)
        return
    start_dt, wake_dt, minutes = parsed["start"], parsed["wake"], parsed["minutes"]
    overlaps = parsed["overlaps"]
    if overlaps:
        await state.set_state(ManualSleep.confirm_replace)
        await state.update_data(
            ms_start=start_dt.isoformat(),
            ms_wake=wake_dt.isoformat(),
            ms_min=minutes,
            ms_overlaps=overlaps,
        )
        await common.ask(
            state,
            f"⚠️ Shu oraliqda ({start_dt.strftime('%d.%m %H:%M')} – {wake_dt.strftime('%d.%m %H:%M')}) "
            "oldingi uyqu yozuvi bor. Yangisi bilan almashtiramizmi?",
            [[("🔄 Almashtirish", "ms:rep")]],
        )
        return
    save_manual_sleep(start_dt, wake_dt, minutes)
    if not direct:
        await common.clear_prompts(state)
        await state.clear()
    await messenger.send(
        f"✅ Uyqu saqlandi: <b>{fmt_min(minutes)}</b> "
        f"({start_dt.strftime('%d.%m %H:%M')} – {wake_dt.strftime('%d.%m %H:%M')})",
        "confirm", bind_trigger=direct,
    )


@router.callback_query(ManualSleep.confirm_replace, F.data == "ms:rep")
async def cb_manual_replace(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    start_dt = datetime.fromisoformat(data["ms_start"])
    wake_dt = datetime.fromisoformat(data["ms_wake"])
    minutes = int(data["ms_min"])
    save_manual_sleep(start_dt, wake_dt, minutes, data.get("ms_overlaps", []))
    await cb.answer("Almashtirildi")
    await common.clear_prompts(state)
    await state.clear()
    await messenger.send(
        f"✅ Uyqu yangilandi: <b>{fmt_min(minutes)}</b> "
        f"({start_dt.strftime('%d.%m %H:%M')} – {wake_dt.strftime('%d.%m %H:%M')})",
        "confirm", bind_trigger=False,
    )


# ------------------------------------------------------------------ qo'lda yotish va menyu

def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="😴 Yotdim (hozir)", callback_data="sl:bed"),
            InlineKeyboardButton(text="☀️ Uyg'ondim", callback_data="sl:wake"),
        ],
        [InlineKeyboardButton(text="✍️ Qo'lda kiritish", callback_data="ms:start")],
    ])


@router.callback_query(F.data == "sl:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.answer()
    last = db.q1("SELECT * FROM sleep_log WHERE duration_min IS NOT NULL ORDER BY wake_at DESC, id DESC")
    text = "😴 <b>Uyqu</b>\n"
    text += f"Oxirgi uyqu: {fmt_min(last['duration_min'])}" if last else "Hali ma'lumot yo'q."
    await messenger.send(text, "info", menu_kb(), bind_trigger=False)


@router.message(Command("uyqu"))
async def cmd_sleep(message: Message):
    last = db.q1("SELECT * FROM sleep_log WHERE duration_min IS NOT NULL ORDER BY wake_at DESC, id DESC")
    text = "😴 <b>Uyqu</b>\n" + (f"Oxirgi uyqu: {fmt_min(last['duration_min'])}" if last else "Hali ma'lumot yo'q.")
    await messenger.send(text, "info", menu_kb())


@router.callback_query(F.data == "sl:bed")
async def cb_bed(cb: CallbackQuery):
    start = on_bedtime_confirmed()
    await cb.answer()
    await messenger.edit_result(cb.message, f"😴 Yotish qayd etildi. Uyqu {start.strftime('%H:%M')} dan hisoblanadi.", "confirm")


@router.callback_query(F.data == "sl:wake")
async def cb_wake_menu(cb: CallbackQuery):
    if woke_today(app.today()):
        await cb.answer("Bugungi uyg'onish allaqachon qayd etilgan", show_alert=True)
        return
    await cb.answer()
    await messenger.send("☀️ Qachon uyg'ondingiz?", "wake", wake_keyboard(), actionable=True, bind_trigger=False)


# ------------------------------------------------------------------ statistika

def stats(days: int) -> dict:
    since = (app.now() - timedelta(days=days)).isoformat()
    rows = db.q("SELECT * FROM sleep_log WHERE duration_min IS NOT NULL AND wake_at >= ? ORDER BY wake_at", (since,))
    if not rows:
        return {"nights": 0}
    avg = sum(r["duration_min"] for r in rows) // len(rows)
    starts = []
    for r in rows:
        t = datetime.fromisoformat(r["sleep_start"])
        mins = t.hour * 60 + t.minute
        starts.append(mins if mins >= 12 * 60 else mins + 24 * 60)
    avg_start = (sum(starts) // len(starts)) % (24 * 60)
    return {
        "nights": len(rows),
        "avg": avg,
        "avg_start": f"{avg_start // 60:02d}:{avg_start % 60:02d}",
        "series": [r["duration_min"] for r in rows],
    }
