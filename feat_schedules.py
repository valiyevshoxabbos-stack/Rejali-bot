"""
AltronBek 4.0.1: Yagona Dinamik Jadval Tizimi, Yopishqoq Izohlar (Sticky Notes),
Tezkor Snooze (+15/+30 daq), Dinamik Eslatmalar (busy_block / deadline) va
Eski Hardcode Eslatmalar Migratsiya Ustasi (Legacy Migration Wizard).
"""

import re
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import feat_common as common
import messenger
import schedule_data as sd

router = Router()

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

PRAYER_REF_TO_UZ = {
    "fajr": "Bomdod",
    "dhuhr": "Peshin",
    "asr": "Asr",
    "maghrib": "Shom",
    "isha": "Xufton",
}
PRAYER_UZ_TO_REF = {v.lower(): k for k, v in PRAYER_REF_TO_UZ.items()}
PRAYER_LABELS = {
    "fajr": "🌅 Bomdod",
    "dhuhr": "☀️ Peshin",
    "asr": "🌤 Asr",
    "maghrib": "🌇 Shom",
    "isha": "🌙 Xufton",
}

WEEKDAY_SHORT = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]

# Eski schedule_data.py dan dinamik jadvalga ko'chirish katalogi (Namozdan tashqari)
LEGACY_MIGRATION_CATALOG: list[dict] = [
    {
        "slug": "yotish",
        "title": "Yotish vaqti",
        "kind": "deadline",
        "start_time": "22:00",
        "end_time": None,
        "days_of_week": "all",
    },
    {
        "slug": "fizika",
        "title": "Fizika: nazariy qism va tayyorlanish",
        "kind": "busy_block",
        "start_time": "07:20",
        "end_time": "08:40",
        "days_of_week": "0,1,2,3,4,5",
    },
    {
        "slug": "zal",
        "title": "Zal mashg'uloti",
        "kind": "busy_block",
        "start_time": "09:00",
        "end_time": "11:00",
        "days_of_week": "0,2,4",
    },
    {
        "slug": "dush",
        "title": "Dush, kiyinish",
        "kind": "deadline",
        "start_time": "11:00",
        "end_time": None,
        "days_of_week": "0,2",
    },
    {
        "slug": "basseyn",
        "title": "Basseyn",
        "kind": "busy_block",
        "start_time": "19:00",
        "end_time": "21:00",
        "days_of_week": "1,3,5",
    },
    {
        "slug": "ish",
        "title": "Maktabda ish",
        "kind": "busy_block",
        "start_time": "07:00",
        "end_time": "14:45",
        "days_of_week": "4,5",
    },
    {
        "slug": "ish_tugash",
        "title": "Ish tugadi",
        "kind": "deadline",
        "start_time": "14:45",
        "end_time": None,
        "days_of_week": "4,5",
    },
]

# Foydalanuvchi tanlagan migratsiya holati (in-memory UI state)
_MIGRATION_SELECTION: dict[int, set[str]] = {}


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _safe_edit(message, text: str, kb: InlineKeyboardMarkup | None = None, add_delete: bool = True):
    markup = messenger.with_delete(kb) if (add_delete and kb is not None) else kb
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            app.log.warning(f"feat_schedules edit_text xatoligi: {e}")


# ------------------------------------------------------------------ 1. System Schedules CRUD & Resolution

def list_system_schedules() -> list[dict]:
    return db.q("SELECT * FROM system_schedules ORDER BY COALESCE(time_hhmm, '23:59'), key")


def get_system_schedule(key: str) -> dict | None:
    return db.q1("SELECT * FROM system_schedules WHERE key = ?", (key,))


def update_system_schedule(
    key: str,
    mode: str | None = None,
    time_hhmm: str | None = None,
    prayer_ref: str | None = None,
    offset_minutes: int | None = None,
    is_enabled: int | None = None,
) -> bool:
    cur = get_system_schedule(key)
    if not cur:
        return False
    new_mode = mode if mode is not None else cur["mode"]
    new_time = time_hhmm if time_hhmm is not None else cur["time_hhmm"]
    new_pref = prayer_ref if prayer_ref is not None else cur["prayer_ref"]
    new_offset = int(offset_minutes) if offset_minutes is not None else int(cur["offset_minutes"] or 0)
    new_en = int(is_enabled) if is_enabled is not None else int(cur["is_enabled"])
    db.ex(
        "UPDATE system_schedules SET mode = ?, time_hhmm = ?, prayer_ref = ?, offset_minutes = ?, "
        "is_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
        (new_mode, new_time, new_pref, new_offset, new_en, key),
    )
    return True


def set_today_override(key: str, hhmm: str, d: date | None = None):
    target_d = d or app.today()
    db.set_setting(f"sys_sched_override_{key}_{target_d.isoformat()}", hhmm)


def get_today_override(key: str, d: date | None = None) -> str | None:
    target_d = d or app.today()
    return db.get_setting(f"sys_sched_override_{key}_{target_d.isoformat()}")


def describe_schedule(sched: dict) -> str:
    if sched["mode"] == "relative_prayer" and sched.get("prayer_ref"):
        pname = PRAYER_REF_TO_UZ.get(sched["prayer_ref"], sched["prayer_ref"])
        off = int(sched.get("offset_minutes") or 0)
        if off > 0:
            return f"{pname} +{off} daq"
        if off < 0:
            return f"{pname} {off} daq"
        return f"{pname} vaqtida"
    return sched.get("time_hhmm") or "--:--"


def resolve_schedule_time(
    key_or_row: str | dict,
    target_date: date | None = None,
    prayer_times: dict | None = None,
) -> datetime | None:
    """
    Tizim jadvali uchun berilgan kundagi aniq datetime ni hisoblaydi.
    - Agar bugun uchun vaqtinchalik override bo'lsa, o'shani ishlatadi.
    - `mode == 'relative_prayer'` bo'lsa, Aladhan namoz vaqtiga `offset_minutes` qo'shadi.
      Namoz vaqti hali kelmagan bo'lsa, fallback sifatida `time_hhmm` ni ishlatadi.
    - `is_enabled == 0` bo'lsa None qaytaradi.
    """
    sched = get_system_schedule(key_or_row) if isinstance(key_or_row, str) else key_or_row
    if not sched or not int(sched.get("is_enabled", 1)):
        return None
    d = target_date or app.today()

    ov = get_today_override(sched["key"], d)
    if ov and TIME_RE.match(ov):
        return app.at(d, ov)

    if sched["mode"] == "relative_prayer" and sched.get("prayer_ref"):
        prayer_uz = PRAYER_REF_TO_UZ.get(sched["prayer_ref"].lower())
        if prayer_times and prayer_uz and prayer_uz in prayer_times:
            base_dt = app.at(d, prayer_times[prayer_uz])
            return base_dt + timedelta(minutes=int(sched.get("offset_minutes") or 0))
        # Fallback: time_hhmm
        if sched.get("time_hhmm") and TIME_RE.match(sched["time_hhmm"]):
            return app.at(d, sched["time_hhmm"])
        return None

    hhmm = sched.get("time_hhmm")
    if hhmm and TIME_RE.match(hhmm):
        return app.at(d, hhmm)
    return None


# ------------------------------------------------------------------ 2. Sticky Notes (schedule_notes)

def add_schedule_note(schedule_key: str, note_text: str, note_type: str = "once") -> int:
    nt = "recurring" if note_type == "recurring" else "once"
    return db.ins(
        "INSERT INTO schedule_notes (schedule_key, note_text, note_type, is_done) VALUES (?, ?, ?, 0)",
        (schedule_key, note_text.strip(), nt),
    )


def list_schedule_notes(schedule_key: str | None = None, only_active: bool = True) -> list[dict]:
    sql = "SELECT * FROM schedule_notes WHERE 1=1 "
    args: list = []
    if schedule_key:
        sql += "AND schedule_key = ? "
        args.append(schedule_key)
    if only_active:
        sql += "AND is_done = 0 "
    sql += "ORDER BY id ASC"
    return db.q(sql, tuple(args))


def mark_note_done(note_id: int) -> bool:
    return db.ex("UPDATE schedule_notes SET is_done = 1 WHERE id = ?", (note_id,)) > 0


def delete_schedule_note(note_id: int) -> bool:
    return db.ex("DELETE FROM schedule_notes WHERE id = ?", (note_id,)) > 0


def attach_sticky_notes(
    schedule_key: str,
    text: str,
    kb: InlineKeyboardMarkup | None = None,
    add_snooze: bool = True,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Tizim xabari ostiga faol `schedule_notes` (`is_done=0`) blokini ulaydi,
    `once` izohlar uchun `[✅ Oldim / Bajardim]` va umumiy Snooze (`[⏳ +15 daq]`, `[⏳ +30 daq]`)
    inline tugmalarini qo'shadi.
    """
    notes = list_schedule_notes(schedule_key, only_active=True)
    rows = [list(r) for r in kb.inline_keyboard] if kb else []

    if notes:
        note_lines = ["\n\n📌 <b>Qo'shimcha izoh:</b>"]
        for n in notes:
            tag = "🔄" if n["note_type"] == "recurring" else "1️⃣"
            note_lines.append(f"• {tag} {app.esc(n['note_text'])}")
            if n["note_type"] == "once":
                short_lbl = n["note_text"][:22]
                rows.append([btn(f"✅ Oldim / Bajardim: {short_lbl}", f"sc:ndone:{n['id']}")])
        text = text + "\n".join(note_lines)

    if add_snooze:
        rows.append([
            btn("⏳ +15 daq", f"sc:snz:sys:{schedule_key}:15"),
            btn("⏳ +30 daq", f"sc:snz:sys:{schedule_key}:30"),
        ])

    new_kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    return text, new_kb


# ------------------------------------------------------------------ 3. Dynamic Reminders (busy_block / deadline)

def add_dynamic_reminder(
    title: str,
    kind: str = "deadline",
    start_time: str = "09:00",
    end_time: str | None = None,
    days_of_week: str = "all",
    remind_before_min: int = 0,
    is_enabled: int = 1,
) -> int:
    k = "busy_block" if kind == "busy_block" else "deadline"
    return db.ins(
        "INSERT INTO dynamic_reminders (title, kind, start_time, end_time, days_of_week, remind_before_min, is_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title.strip(), k, start_time.strip(), end_time.strip() if end_time else None,
         (days_of_week or "all").strip(), int(remind_before_min or 0), int(is_enabled)),
    )


def update_dynamic_reminder(rem_id: int, **fields) -> bool:
    allowed = {"title", "kind", "start_time", "end_time", "days_of_week", "remind_before_min", "is_enabled"}
    updates = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
    if not updates:
        return False
    sets = ", ".join(f"{k} = ?" for k, _ in updates)
    vals = [v for _, v in updates] + [rem_id]
    return db.ex(f"UPDATE dynamic_reminders SET {sets} WHERE id = ?", tuple(vals)) > 0


def delete_dynamic_reminder(rem_id: int) -> bool:
    return db.ex("DELETE FROM dynamic_reminders WHERE id = ?", (rem_id,)) > 0


def list_dynamic_reminders(only_enabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM dynamic_reminders "
    if only_enabled:
        sql += "WHERE is_enabled = 1 "
    sql += "ORDER BY start_time ASC, id ASC"
    return db.q(sql)


def matches_day(days_of_week: str | None, d: date) -> bool:
    spec = (days_of_week or "all").strip().lower()
    if spec == "all" or not spec:
        return True
    if spec.startswith("once:"):
        return spec[5:].strip() == d.isoformat()
    parts = [p.strip() for p in spec.split(",") if p.strip().isdigit()]
    return str(d.weekday()) in parts


def get_dynamic_reminders_for_date(d: date | None = None) -> list[dict]:
    target_d = d or app.today()
    return [r for r in list_dynamic_reminders(only_enabled=True) if matches_day(r.get("days_of_week"), target_d)]


def get_active_busy_blocks(at_dt: datetime | None = None) -> list[dict]:
    """Berilgan vaqtda foydalanuvchi band bo'lgan `busy_block` larni qaytaradi."""
    dt = at_dt or app.now()
    hhmm = dt.strftime("%H:%M")
    active = []
    for r in get_dynamic_reminders_for_date(dt.date()):
        if r["kind"] != "busy_block" or not r.get("end_time"):
            continue
        if r["start_time"] <= hhmm <= r["end_time"]:
            active.append(r)
    return active


def format_days_label(days_of_week: str | None) -> str:
    spec = (days_of_week or "all").strip().lower()
    if spec == "all" or not spec:
        return "Har kuni"
    if spec.startswith("once:"):
        return f"Bir marta ({spec[5:]})"
    nums = [int(x) for x in spec.split(",") if x.strip().isdigit() and 0 <= int(x) <= 6]
    return ", ".join(WEEKDAY_SHORT[n] for n in nums) if nums else spec


async def send_dynamic_reminder(rem_id: int):
    r = db.q1("SELECT * FROM dynamic_reminders WHERE id = ?", (rem_id,))
    if not r or not int(r["is_enabled"]):
        return
    if r["kind"] == "busy_block" and r.get("end_time"):
        header = f"🚧 <b>Bandlik bloki: {app.esc(r['title'])}</b>\n🕒 {r['start_time']}–{r['end_time']}"
    else:
        header = f"⏰ <b>Dinamik eslatma: {app.esc(r['title'])}</b>\n🕒 {r['start_time']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Bajardim", f"sc:ddone:{rem_id}")],
        [btn("⏳ +15 daq", f"sc:snz:dyn:{rem_id}:15"), btn("⏳ +30 daq", f"sc:snz:dyn:{rem_id}:30")],
    ])
    await messenger.send(header, "reminder", kb, actionable=True, bind_trigger=False)


def schedule_dynamic_reminders(d: date | None = None):
    target_d = d or app.today()
    now = app.now()
    for job in list(app.scheduler.get_jobs()):
        if job.id.startswith("dyn_rem_"):
            job.remove()
    for r in get_dynamic_reminders_for_date(target_d):
        if not TIME_RE.match(r["start_time"]):
            continue
        start_dt = app.at(target_d, r["start_time"])
        lead = int(r.get("remind_before_min") or 0)
        run_dt = start_dt - timedelta(minutes=lead)
        if run_dt > now:
            app.schedule_job(run_dt, send_dynamic_reminder, [r["id"]], f"dyn_rem_{r['id']}_{target_d.isoformat()}")


# ------------------------------------------------------------------ 4. System Schedule Dispatcher & Snooze

async def _run_system_schedule(key: str):
    """Kalit bo'yicha tegishli tizim xabarini chaqiradi."""
    import feat_birthdays
    import feat_summary

    if key == "morning_summary":
        await feat_summary.send_morning()
    elif key == "evening_summary":
        await feat_summary.send_evening()
    elif key == "daily_notice":
        await feat_birthdays.notice_job()
    elif key == "morning_hygiene":
        if "health" in app.OPTIONAL:
            await app.OPTIONAL["health"].send_morning_hygiene_reminder()
    elif key in ("finance_alerts", "finance_alerts_pm"):
        if "finance" in app.OPTIONAL:
            await app.OPTIONAL["finance"].check_finance_alerts()
    elif key == "uni_bag_pack":
        if "uni" in app.OPTIONAL:
            await app.OPTIONAL["uni"].send_bag_reminder(app.today().isoformat())
    elif key == "uni_leave":
        if "uni" in app.OPTIONAL:
            await app.OPTIONAL["uni"].send_leave_reminder(app.today().isoformat())
    elif key == "edu_evening_preview":
        if "education" in app.OPTIONAL:
            await app.OPTIONAL["education"].send_evening_preview()


def _job_id_for_system_key(key: str) -> str:
    if key == "edu_evening_preview":
        return "edu_evening_preview"
    return f"sys_sched_{key}"


def schedule_system_jobs(prayer_times: dict | None = None, d: date | None = None):
    """
    `system_schedules` jadvalidagi barcha faol tizim xabarlarini bugun uchun APScheduler'ga qo'yadi.
    `uni_bag_pack` va `uni_leave` universitet darslari bor kunlarda `feat_uni.schedule_today_uni` orqali qo'yiladi.
    """
    target_d = d or app.today()
    now = app.now()

    for job in list(app.scheduler.get_jobs()):
        if job.id.startswith("sys_sched_"):
            job.remove()

    for sched in list_system_schedules():
        key = sched["key"]
        if key in ("uni_bag_pack", "uni_leave"):
            continue
        jid = _job_id_for_system_key(key)
        if not int(sched["is_enabled"]):
            existing = app.scheduler.get_job(jid)
            if existing:
                existing.remove()
            continue
        run_dt = resolve_schedule_time(sched, target_d, prayer_times=prayer_times)
        if run_dt and run_dt > now:
            app.schedule_job(run_dt, _run_system_schedule, [key], jid)


async def reschedule_single_system_job(key: str, prayer_times: dict | None = None):
    """Bitta tizim vaqti o'zgarganda botni qayta yoqmasdan APScheduler jobini darhol yangilaydi."""
    if prayer_times is None:
        try:
            import feat_summary
            prayer_times = await feat_summary.prayer_times_today()
        except Exception:
            prayer_times = None

    if key in ("uni_bag_pack", "uni_leave"):
        if "uni" in app.OPTIONAL:
            await app.OPTIONAL["uni"].schedule_today_uni(prayer_times=prayer_times)
        return

    jid = _job_id_for_system_key(key)
    existing = app.scheduler.get_job(jid)
    if existing:
        existing.remove()

    sched = get_system_schedule(key)
    if not sched or not int(sched["is_enabled"]):
        return
    run_dt = resolve_schedule_time(sched, app.today(), prayer_times=prayer_times)
    if run_dt and run_dt > app.now():
        app.schedule_job(run_dt, _run_system_schedule, [key], jid)


def snooze_system_schedule(key: str, minutes: int) -> tuple[str, datetime]:
    """Tizim xabarini +N daqiqaga surish uchun bir martalik APScheduler job yaratadi."""
    run_dt = app.now() + timedelta(minutes=int(minutes))
    jid = f"snooze_sys_{key}_{int(run_dt.timestamp())}"
    app.schedule_job(run_dt, _run_system_schedule, [key], jid)
    return jid, run_dt


def snooze_dynamic_reminder(rem_id: int, minutes: int) -> tuple[str, datetime]:
    """Dinamik eslatmani +N daqiqaga surish uchun bir martalik APScheduler job yaratadi."""
    run_dt = app.now() + timedelta(minutes=int(minutes))
    jid = f"snooze_dyn_{rem_id}_{int(run_dt.timestamp())}"
    app.schedule_job(run_dt, send_dynamic_reminder, [rem_id], jid)
    return jid, run_dt


# ------------------------------------------------------------------ 5. Legacy Hardcode Migration Wizard

def needs_legacy_migration() -> bool:
    return db.get_ai_setting("legacy_migration_done", "0") != "1"


def get_migration_candidates() -> list[dict]:
    return list(LEGACY_MIGRATION_CATALOG)


def build_migration_wizard(selected_slugs: set[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    sel = selected_slugs if selected_slugs is not None else set()
    lines = [
        "🔄 <b>AltronBek v4.0.1 — Dinamik Jadval Migratsiyasi</b>\n",
        "Eski statik eslatmalar bekor qilindi. Quyidagilardan qaysilarini yangi Dinamik Jadvalga o‘tkazib saqlab qolaylik?",
        "",
        "<i>Keraklilarini belgilang va «💾 Tanlanganlarni saqlash» tugmasini bosing:</i>",
    ]
    rows = []
    for item in LEGACY_MIGRATION_CATALOG:
        slug = item["slug"]
        mark = "✅" if slug in sel else "⬜"
        span = f"{item['start_time']}–{item['end_time']}" if item.get("end_time") else item["start_time"]
        rows.append([btn(f"{mark} {item['title']} ({span})", f"sc:mig:tg:{slug}")])
    rows.append([
        btn("💾 Tanlanganlarni saqlash", "sc:mig:save"),
        btn("🗑 Hech birini saqlamaslik", "sc:mig:skip"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def apply_legacy_migration(selected_slugs: set[str] | list[str]) -> list[int]:
    """
    Faqat tanlangan eski hardcode eslatmalarni `dynamic_reminders` jadvaliga ko'chiradi,
    eski `reminders WHERE source = 'standard'` yozuvlarini o'chiradi va `legacy_migration_done = '1'` qiladi.
    """
    chosen = set(selected_slugs)
    created_ids: list[int] = []
    for item in LEGACY_MIGRATION_CATALOG:
        if item["slug"] in chosen:
            rid = add_dynamic_reminder(
                title=item["title"],
                kind=item["kind"],
                start_time=item["start_time"],
                end_time=item.get("end_time"),
                days_of_week=item.get("days_of_week", "all"),
                remind_before_min=0,
                is_enabled=1,
            )
            created_ids.append(rid)

    # Eski statik hardcode eslatmalarni tozalash (Namoz va foydalanuvchi maxsus eslatmalari daxlsiz!)
    try:
        db.ex(
            "DELETE FROM occurrences WHERE status = 'pending' AND prayer IS NULL "
            "AND reminder_id IN (SELECT id FROM reminders WHERE source = 'standard')"
        )
        db.ex("DELETE FROM reminders WHERE source = 'standard'")
    except Exception as e:
        app.log.warning(f"Eski statik eslatmalarni tozalashda xatolik: {e}")

    db.set_ai_setting("legacy_migration_done", "1")
    schedule_dynamic_reminders()
    return created_ids


async def send_migration_wizard_if_needed() -> bool:
    if not needs_legacy_migration():
        return False
    _MIGRATION_SELECTION[app.CHAT_ID] = set()
    text, kb = build_migration_wizard(_MIGRATION_SELECTION[app.CHAT_ID])
    await messenger.send(text, "system", kb, actionable=True, bind_trigger=False)
    return True


@router.callback_query(F.data.startswith("sc:mig:tg:"))
async def cb_mig_toggle(cb: CallbackQuery):
    slug = cb.data[len("sc:mig:tg:"):]
    sel = _MIGRATION_SELECTION.setdefault(app.CHAT_ID, set())
    if slug in sel:
        sel.remove(slug)
    else:
        sel.add(slug)
    await cb.answer()
    text, kb = build_migration_wizard(sel)
    await _safe_edit(cb.message, text, kb, add_delete=False)


@router.callback_query(F.data == "sc:mig:save")
async def cb_mig_save(cb: CallbackQuery):
    sel = _MIGRATION_SELECTION.pop(app.CHAT_ID, set())
    created = apply_legacy_migration(sel)
    await cb.answer("Saqlandi!")
    if created:
        names = [it["title"] for it in LEGACY_MIGRATION_CATALOG if it["slug"] in sel]
        summary = "✅ <b>Migratsiya yakunlandi!</b>\n\nDinamik jadvalga ko'chirildi:\n" + "\n".join(
            f"• {app.esc(n)}" for n in names
        )
    else:
        summary = "✅ <b>Migratsiya yakunlandi!</b>\n\nEski statik eslatmalarning barchasi o'chirildi."
    await _safe_edit(cb.message, summary, InlineKeyboardMarkup(inline_keyboard=[[btn("⏰ Tizim vaqtlari", "sc:home")]]))


@router.callback_query(F.data == "sc:mig:skip")
async def cb_mig_skip(cb: CallbackQuery):
    _MIGRATION_SELECTION.pop(app.CHAT_ID, None)
    apply_legacy_migration(set())
    await cb.answer("Eski eslatmalar o'chirildi")
    await _safe_edit(
        cb.message,
        "✅ <b>Migratsiya yakunlandi!</b>\n\nEski statik eslatmalar tozalangan. Yangi eslatmalarni AI Suhbat yoki Sozlamalar orqali qo'shishingiz mumkin.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⏰ Tizim vaqtlari", "sc:home")]]),
    )


# ------------------------------------------------------------------ 6. Snooze & Sticky Note Callbacks

@router.callback_query(F.data.startswith("sc:ndone:"))
async def cb_note_done(cb: CallbackQuery):
    note_id = int(cb.data.split(":")[2])
    mark_note_done(note_id)
    await cb.answer("✅ Izoh bajarildi!", show_alert=False)
    cur_text = cb.message.text or ""
    await _safe_edit(cb.message, cur_text + "\n\n✅ <i>Qo'shimcha izoh bajarildi deb belgilandi.</i>", None)


@router.callback_query(F.data.startswith("sc:snz:sys:"))
async def cb_snooze_sys(cb: CallbackQuery):
    parts = cb.data.split(":")
    key, mins = parts[3], int(parts[4])
    _, run_dt = snooze_system_schedule(key, mins)
    await cb.answer(f"⏳ +{mins} daqiqaga surildi ({run_dt.strftime('%H:%M')})", show_alert=False)
    cur_text = cb.message.text or ""
    await _safe_edit(cb.message, cur_text + f"\n\n⏳ <i>+{mins} daqiqaga surildi — {run_dt.strftime('%H:%M')} da qayta eslatiladi.</i>", None)


@router.callback_query(F.data.startswith("sc:snz:dyn:"))
async def cb_snooze_dyn(cb: CallbackQuery):
    parts = cb.data.split(":")
    rem_id, mins = int(parts[3]), int(parts[4])
    _, run_dt = snooze_dynamic_reminder(rem_id, mins)
    await cb.answer(f"⏳ +{mins} daqiqaga surildi ({run_dt.strftime('%H:%M')})", show_alert=False)
    cur_text = cb.message.text or ""
    await _safe_edit(cb.message, cur_text + f"\n\n⏳ <i>+{mins} daqiqaga surildi — {run_dt.strftime('%H:%M')} da qayta eslatiladi.</i>", None)


@router.callback_query(F.data.startswith("sc:ddone:"))
async def cb_dyn_done(cb: CallbackQuery):
    await cb.answer("✅ Bajarildi!")
    cur_text = cb.message.text or ""
    await _safe_edit(cb.message, cur_text + "\n\n✅ <i>Bajarildi.</i>", None)


# ------------------------------------------------------------------ 7. Manual UI: ⚙️ Sozlamalar -> ⏰ Tizim vaqtlari

class SchedEdit(StatesGroup):
    waiting_time = State()
    waiting_offset = State()
    waiting_note = State()


def render_schedules_home() -> tuple[str, InlineKeyboardMarkup]:
    scheds = list_system_schedules()
    lines = [
        "⏰ <b>Tizim vaqtlari (Dinamik Jadval)</b>\n",
        "Har bir tizim xabari vaqtini aniq soatga yoki namoz vaqtiga bog'lab sozlashingiz, "
        "shuningdek unga yopishqoq izohlar biriktirishingiz mumkin:\n",
    ]
    rows = []
    for s in scheds:
        st_icon = "✅" if int(s["is_enabled"]) else "🔕"
        t_desc = describe_schedule(s)
        ncnt = len(list_schedule_notes(s["key"], only_active=True))
        nbadge = f" 📌{ncnt}" if ncnt else ""
        rows.append([btn(f"{st_icon} {s['title'][:24]} — {t_desc}{nbadge}", f"sc:item:{s['key']}")])
    rows.append([btn("📋 Dinamik eslatmalar (Bandlik/Muddat)", "sc:dyn")])
    rows.append([btn("⬅️ Sozlamalar", "se:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_schedule_detail(key: str) -> tuple[str, InlineKeyboardMarkup]:
    s = get_system_schedule(key)
    if not s:
        return "Topilmadi.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "sc:home")]])
    notes = list_schedule_notes(key, only_active=True)
    status_s = "✅ Yoqilgan" if int(s["is_enabled"]) else "🔕 O'chirilgan"
    lines = [
        f"⏰ <b>{app.esc(s['title'])}</b>",
        f"Kalit: <code>{app.esc(key)}</code>",
        f"Holat: {status_s}",
        f"Jadval: <b>{app.esc(describe_schedule(s))}</b>",
    ]
    if s["mode"] == "relative_prayer" and s.get("time_hhmm"):
        lines.append(f"Zaxira soat (API bo'lmasa): <code>{s['time_hhmm']}</code>")
    if notes:
        lines.append("\n📌 <b>Faol izohlar:</b>")
        for n in notes:
            tag = "Doimiy" if n["note_type"] == "recurring" else "Bir martalik"
            lines.append(f"• [{tag}] {app.esc(n['note_text'])}")

    toggle_lbl = "🔕 O'chirish" if int(s["is_enabled"]) else "🔔 Yoqish"
    rows = [
        [btn("🕒 Aniq soat", f"sc:setfixed:{key}"), btn("🕌 Namozga bog'lash", f"sc:setpray:{key}")],
        [btn(toggle_lbl, f"sc:toggle:{key}"), btn(f"📌 Izohlar ({len(notes)})", f"sc:notes:{key}")],
        [btn("⬅️ Tizim vaqtlari", "sc:home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "sc:home")
async def cb_sched_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    text, kb = render_schedules_home()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:item:"))
async def cb_sched_item(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    key = cb.data[len("sc:item:"):]
    await cb.answer()
    text, kb = render_schedule_detail(key)
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:toggle:"))
async def cb_sched_toggle(cb: CallbackQuery):
    key = cb.data[len("sc:toggle:"):]
    s = get_system_schedule(key)
    if not s:
        await cb.answer("Topilmadi")
        return
    new_en = 0 if int(s["is_enabled"]) else 1
    update_system_schedule(key, is_enabled=new_en)
    await reschedule_single_system_job(key)
    await cb.answer("Yoqildi" if new_en else "O'chirildi")
    text, kb = render_schedule_detail(key)
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:setfixed:"))
async def cb_sched_setfixed(cb: CallbackQuery, state: FSMContext):
    key = cb.data[len("sc:setfixed:"):]
    await cb.answer()
    await state.clear()
    await state.set_state(SchedEdit.waiting_time)
    await state.update_data(sched_key=key)
    await common.ask(state, f"<b>{app.esc(key)}</b> uchun yangi soatni kiriting (masalan <code>06:40</code>):")


@router.message(SchedEdit.waiting_time)
async def msg_sched_time(message: Message, state: FSMContext):
    await common.track_user(state, message)
    raw = (message.text or "").strip()
    m = TIME_RE.match(raw)
    if not m:
        await messenger.send("Format noto'g'ri. <code>HH:MM</code> ko'rinishida yozing (masalan 07:15).", "confirm", bind_trigger=False)
        return
    hhmm = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
    data = await state.get_data()
    key = data["sched_key"]
    update_system_schedule(key, mode="fixed", time_hhmm=hhmm, prayer_ref=None, offset_minutes=0)
    await reschedule_single_system_job(key)
    await common.clear_prompts(state)
    await state.clear()
    text, kb = render_schedule_detail(key)
    await messenger.send(f"✅ Vaqt <b>{hhmm}</b> ga yangilandi.\n\n" + text, "info", kb, bind_trigger=False)


@router.callback_query(F.data.startswith("sc:setpray:"))
async def cb_sched_setpray(cb: CallbackQuery):
    key = cb.data[len("sc:setpray:"):]
    await cb.answer()
    rows = [[btn(lbl, f"sc:pray:{key}:{pref}")] for pref, lbl in PRAYER_LABELS.items()]
    rows.append([btn("⬅️ Orqaga", f"sc:item:{key}")])
    await _safe_edit(cb.message, "Qaysi namoz vaqtiga bog'laymiz?", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("sc:pray:"))
async def cb_sched_pray_pick(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, pref = parts[2], parts[3]
    await cb.answer()
    await state.clear()
    await state.set_state(SchedEdit.waiting_offset)
    await state.update_data(sched_key=key, prayer_ref=pref)
    pname = PRAYER_REF_TO_UZ.get(pref, pref)
    await common.ask(
        state,
        f"<b>{pname}</b> namozidan necha daqiqa keyin (yoki oldin) eslataylik?\n"
        "Masalan: <code>+20</code> (20 daqiqa keyin), <code>-15</code> (15 daqiqa oldin) yoki <code>0</code>:",
    )


@router.message(SchedEdit.waiting_offset)
async def msg_sched_offset(message: Message, state: FSMContext):
    await common.track_user(state, message)
    raw = (message.text or "").strip().replace(" ", "")
    try:
        offset = int(raw)
        if not (-240 <= offset <= 360):
            raise ValueError
    except ValueError:
        await messenger.send("Butun son kiriting (-240 dan +360 gacha, masalan <code>+20</code>).", "confirm", bind_trigger=False)
        return
    data = await state.get_data()
    key, pref = data["sched_key"], data["prayer_ref"]
    update_system_schedule(key, mode="relative_prayer", prayer_ref=pref, offset_minutes=offset)
    await reschedule_single_system_job(key)
    await common.clear_prompts(state)
    await state.clear()
    text, kb = render_schedule_detail(key)
    await messenger.send("✅ Namoz vaqtiga bog'landi va scheduler yangilandi.\n\n" + text, "info", kb, bind_trigger=False)


# ------------------------------------------------------------------ Sticky Notes UI

def render_notes_menu(key: str) -> tuple[str, InlineKeyboardMarkup]:
    s = get_system_schedule(key)
    title = s["title"] if s else key
    notes = list_schedule_notes(key, only_active=True)
    lines = [f"📌 <b>Izohlar — {app.esc(title)}</b>\n"]
    rows = []
    if not notes:
        lines.append("<i>Hozircha faol izoh yo'q.</i>")
    else:
        for n in notes:
            tag = "🔄 Doimiy" if n["note_type"] == "recurring" else "1️⃣ Bir martalik"
            lines.append(f"• [{tag}] {app.esc(n['note_text'])}")
            rows.append([btn(f"🗑 O'chirish: {n['note_text'][:22]}", f"sc:ndel:{key}:{n['id']}")])
    rows.append([
        btn("➕ Bir martalik izoh", f"sc:nadd:{key}:once"),
        btn("🔄 Doimiy izoh", f"sc:nadd:{key}:recurring"),
    ])
    rows.append([btn("⬅️ Orqaga", f"sc:item:{key}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("sc:notes:"))
async def cb_sched_notes(cb: CallbackQuery):
    key = cb.data[len("sc:notes:"):]
    await cb.answer()
    text, kb = render_notes_menu(key)
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:nadd:"))
async def cb_sched_note_add(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, ntype = parts[2], parts[3]
    await cb.answer()
    await state.clear()
    await state.set_state(SchedEdit.waiting_note)
    await state.update_data(sched_key=key, note_type=ntype)
    lbl = "doimiy" if ntype == "recurring" else "bir martalik"
    await common.ask(state, f"Yangi <b>{lbl}</b> izoh matnini yozing (masalan: <code>Magnit ham olvol</code>):")


@router.message(SchedEdit.waiting_note)
async def msg_sched_note(message: Message, state: FSMContext):
    await common.track_user(state, message)
    txt = (message.text or "").strip()
    if not txt:
        await messenger.send("Izoh matnini kiriting.", "confirm", bind_trigger=False)
        return
    data = await state.get_data()
    key, ntype = data["sched_key"], data["note_type"]
    add_schedule_note(key, txt, ntype)
    await common.clear_prompts(state)
    await state.clear()
    text, kb = render_notes_menu(key)
    await messenger.send("✅ Izoh qo'shildi!\n\n" + text, "info", kb, bind_trigger=False)


@router.callback_query(F.data.startswith("sc:ndel:"))
async def cb_sched_note_del(cb: CallbackQuery):
    parts = cb.data.split(":")
    key, nid = parts[2], int(parts[3])
    delete_schedule_note(nid)
    await cb.answer("Izoh o'chirildi")
    text, kb = render_notes_menu(key)
    await _safe_edit(cb.message, text, kb)


# ------------------------------------------------------------------ Dynamic Reminders UI

def render_dynamic_reminders_menu() -> tuple[str, InlineKeyboardMarkup]:
    items = list_dynamic_reminders(only_enabled=False)
    lines = ["📋 <b>Dinamik eslatmalar (Bandlik & Muddat)</b>\n"]
    rows = []
    if not items:
        lines.append("<i>Hozircha dinamik eslatmalar yo'q. AI Suhbat orqali qo'shishingiz mumkin.</i>")
    else:
        for r in items:
            st = "✅" if int(r["is_enabled"]) else "🔕"
            kind_ic = "🚧" if r["kind"] == "busy_block" else "⏰"
            span = f"{r['start_time']}–{r['end_time']}" if r.get("end_time") else r["start_time"]
            days_s = format_days_label(r.get("days_of_week"))
            lines.append(f"{st} {kind_ic} <b>{app.esc(r['title'])}</b> — {span} ({days_s})")
            rows.append([
                btn(f"{'🔕' if int(r['is_enabled']) else '🔔'} {r['title'][:18]}", f"sc:dyntg:{r['id']}"),
                btn("🗑", f"sc:dyndel:{r['id']}"),
            ])
    rows.append([btn("⬅️ Tizim vaqtlari", "sc:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "sc:dyn")
async def cb_dyn_home(cb: CallbackQuery):
    await cb.answer()
    text, kb = render_dynamic_reminders_menu()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:dyntg:"))
async def cb_dyn_toggle(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    r = db.q1("SELECT is_enabled FROM dynamic_reminders WHERE id = ?", (rid,))
    if r:
        update_dynamic_reminder(rid, is_enabled=0 if int(r["is_enabled"]) else 1)
        schedule_dynamic_reminders()
    await cb.answer("Yangilandi")
    text, kb = render_dynamic_reminders_menu()
    await _safe_edit(cb.message, text, kb)


@router.callback_query(F.data.startswith("sc:dyndel:"))
async def cb_dyn_delete(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    delete_dynamic_reminder(rid)
    schedule_dynamic_reminders()
    await cb.answer("O'chirildi")
    text, kb = render_dynamic_reminders_menu()
    await _safe_edit(cb.message, text, kb)
