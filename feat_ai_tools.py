"""
AltronBek 4.0.1: To'liq Bot Asboblari (Full-Bot Tools — Barcha 9 modul).

Qat'iy xavfsizlik qoidasi (Human-in-the-Loop):
- READ-ONLY asboblar (`get_*`) darhol bazani o'qiydi va javob qaytaradi.
- Bazaga yozadigan/tahrirlaydigan/o'chiradigan yoki scheduler vaqtini o'zgartiradigan
  HAR QANDAY amal AI tomonidan TO'G'RIDAN-TO'G'RI bajarilmaydi!
  AI faqat `propose_actions(...)` orqali `ai_pending_actions` jadvaliga kutilayotgan amal yaratadi.
  Faqat foydalanuvchi `[✅ Tasdiqlayman]` bosgach `execute_pending_action(action_id)` ishga tushadi.
"""

import json
import re
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import app
import db
import feat_schedules as schedules
import qazo as qz
import services as sv

MEDICAL_DISCLAIMER = (
    "\n\n⚠️ <b>Tibbiy ogohlantirish:</b> Ushbu ma'lumot faqat axborot uchundir, shifokor tashxisi o'rnini bosmaydi. "
    "Jiddiy yoki xavfli belgilarda darhol <b>103</b> tez tibbiy yordamga murojaat qiling!"
)

SUPPORTED_WRITE_ACTIONS = {
    # 1. Moliya
    "finance_expense",
    "finance_income",
    "finance_transfer",
    "finance_add_debt",
    "finance_settle_debt",
    # 2. Bozorlik
    "market_add_planned",
    "market_remove_planned",
    # 3. Maktab
    "school_add_class_note",
    # 4. Universitet
    "uni_add_homework",
    "uni_complete_homework",
    "uni_complete_work_step",
    # 5. Salomatlik
    "health_log_intake",
    "health_add_item",
    # 6. Chelenj
    "challenge_complete_task",
    "challenge_use_freeze",
    # 7. Kundalik
    "daily_add_note",
    "daily_delete_note",
    "daily_log_weight",
    "daily_sleep_action",
    # 8. Dinamik tizim vaqtlari
    "schedule_update",
    # 9. Qo'shimcha izohlar (Sticky Notes)
    "schedule_add_note",
    "schedule_remove_note",
    # 10. Dinamik eslatmalar (busy_block / deadline)
    "dynamic_reminder_add",
    "dynamic_reminder_update",
    "dynamic_reminder_delete",
}


def _resolve_day_arg(day: str | None) -> date:
    d_str = (day or "today").strip().lower()
    if d_str in ("today", "bugun", ""):
        return app.today()
    if d_str in ("tomorrow", "ertaga"):
        return app.today() + timedelta(days=1)
    if d_str in ("yesterday", "kecha"):
        return app.today() - timedelta(days=1)
    try:
        return date.fromisoformat(d_str)
    except ValueError:
        return app.today()


# ==================================================================
# 1. READ-ONLY TOOLS (Darhol bajariladi)
# ==================================================================

def get_finance_summary() -> dict:
    """Kartalar/naqd balans, qarzlar, kreditlar, yaqin to'lovlar va oxirgi tranzaksiyalarni qaytaradi."""
    accounts = db.q("SELECT id, name, last_4, type, balance, credit_limit FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    debts = db.q("SELECT id, person_name, amount, due_date, note FROM finance_debts WHERE status = 'active' ORDER BY due_date ASC")
    loans = db.q("SELECT id, title, remaining_debt, monthly_payment, due_day, remaining_installments FROM finance_loans WHERE status = 'active'")
    recurring = db.q("SELECT id, title, target_amount, paid_amount_this_month, due_start_day, due_end_day FROM finance_recurring WHERE status = 'active'")
    recent_tx = db.q(
        "SELECT tx.id, tx.amount, tx.category, tx.note, tx.created_at, a1.name AS from_acc, a2.name AS to_acc "
        "FROM finance_transactions tx "
        "LEFT JOIN finance_accounts a1 ON a1.id = tx.from_account_id "
        "LEFT JOIN finance_accounts a2 ON a2.id = tx.to_account_id "
        "ORDER BY tx.id DESC LIMIT 8"
    )
    upcoming = []
    if "finance" in app.OPTIONAL:
        try:
            upcoming = app.OPTIONAL["finance"].upcoming_payments(app.today(), max_days=5)
        except Exception:
            pass
    return {
        "accounts": accounts,
        "debts": debts,
        "loans": loans,
        "recurring": recurring,
        "upcoming_payments": upcoming,
        "recent_transactions": recent_tx,
    }


def get_market_info(item_name: str = "") -> dict:
    """Rejalashtirilgan bozorlik ro'yxati va (agar item_name berilsa) savdo joylari narxlari tahlilini qaytaradi."""
    planned_basket = db.q1("SELECT id, title FROM market_baskets WHERE mode = 'planned' AND status = 'draft' ORDER BY created_at DESC LIMIT 1")
    planned_items = []
    if planned_basket:
        planned_items = db.q(
            "SELECT bi.id, mi.name, bi.quantity, bi.unit, ml.name AS location "
            "FROM market_basket_items bi "
            "JOIN market_items mi ON mi.id = bi.item_id "
            "LEFT JOIN market_locations ml ON ml.id = bi.location_id "
            "WHERE bi.basket_id = ? ORDER BY bi.id ASC",
            (planned_basket["id"],),
        )
    price_history = []
    query = (item_name or "").strip()
    if query:
        price_history = db.q(
            "SELECT mi.name, ml.name AS location, l.unit_price, l.unit, l.price_diff_percent, l.bought_at "
            "FROM market_logs l "
            "JOIN market_items mi ON mi.id = l.item_id "
            "LEFT JOIN market_locations ml ON ml.id = l.location_id "
            "WHERE LOWER(mi.name) LIKE LOWER(?) "
            "ORDER BY l.unit_price ASC, l.bought_at DESC LIMIT 10",
            (f"%{query}%",),
        )
    return {
        "planned_items": planned_items,
        "item_query": query,
        "price_history": price_history,
    }


def get_school_schedule(day: str = "today") -> dict:
    """Bugungi/ertangi maktab darslari, joriy TMR mavzusi va sinf izohlarini qaytaradi."""
    d = _resolve_day_arg(day)
    if "education" not in app.OPTIONAL:
        return {"date": d.isoformat(), "available": False, "lessons": []}
    edu = app.OPTIONAL["education"]
    is_off, off_title, off_type = edu.is_off_day(d)
    if is_off or d.weekday() >= 6:
        return {
            "date": d.isoformat(),
            "is_off_day": True,
            "off_reason": off_title or "Yakshanba (dam olish)",
            "lessons": [],
        }
    slots = [s for s in edu.get_timetable_for_day(d.weekday()) if s.get("course_id")]
    lessons = []
    for s in slots:
        p = s["period_number"]
        start_t, end_t = edu.EDU_BELLS.get(p, ("", ""))
        topic = db.q1(
            "SELECT topic_order, title, homework, topic_type FROM edu_topics WHERE course_id = ? AND topic_order = ?",
            (s["course_id"], s["current_topic_order"]),
        )
        notes = [n["note_text"] for n in edu.get_unresolved_notes(s["course_id"])]
        lessons.append({
            "period": p,
            "time": f"{start_t}-{end_t}",
            "course_id": s["course_id"],
            "class_name": s["class_name"],
            "subject_name": s["subject_name"],
            "current_topic": topic,
            "class_notes": notes,
        })
    return {"date": d.isoformat(), "is_off_day": False, "lessons": lessons}


def get_uni_schedule(day: str = "today") -> dict:
    """Bugungi/ertangi universitet paralari, uy ishlari va BMI/kurs ishi holatini qaytaradi."""
    d = _resolve_day_arg(day)
    if "uni" not in app.OPTIONAL:
        return {"date": d.isoformat(), "available": False, "lessons": []}
    uni = app.OPTIONAL["uni"]
    parity = uni.current_week_parity(d)
    lessons = uni.get_today_lessons(d)
    formatted_lessons = []
    for les in lessons:
        p = les["para_number"]
        st, en = uni.UNI_BELLS.get(p, ("", ""))
        formatted_lessons.append({
            "para": p,
            "time": f"{st}-{en}",
            "subject_id": les["subject_id"],
            "subject_name": les["subject_name"],
            "lesson_type": les["lesson_type"],
            "room": les.get("custom_room") or les.get("default_room") or "",
            "teacher": les.get("teacher") or "",
        })
    pending_hw = uni.get_homeworks(only_pending=True)
    works = db.q("SELECT id, work_type, title, subject_name, deadline, status FROM uni_works WHERE status = 'active'")
    for w in works:
        steps = db.q("SELECT step_order, step_title, is_done FROM uni_work_steps WHERE work_id = ? ORDER BY step_order", (w["id"],))
        w["steps"] = steps
    return {
        "date": d.isoformat(),
        "week_parity": parity,
        "lessons": formatted_lessons,
        "pending_homeworks": pending_hw,
        "academic_works": works,
    }


def get_health_status() -> dict:
    """Aptechkadagi dorilar, tugayotgan vositalar va faol davolanish kurslarini qaytaradi."""
    items = db.q(
        "SELECT id, name, kind, location, form, remaining_quantity, unit, low_stock_threshold, notes "
        "FROM health_items WHERE is_active = 1 AND (COALESCE(is_seed, 0) = 0 OR remaining_quantity > 0) "
        "ORDER BY name ASC"
    )
    low_stock = [it for it in items if float(it["remaining_quantity"] or 0) <= float(it["low_stock_threshold"] or 2)]
    courses = db.q("SELECT id, title, doctor_notes, start_date, end_date, status FROM health_courses WHERE is_active = 1")
    for c in courses:
        c["items"] = db.q(
            "SELECT id, item_name, schedule_times, dose_quantity, dose_unit, relation_to_food, duration_days "
            "FROM health_course_items WHERE course_id = ? AND is_active = 1",
            (c["id"],),
        )
    return {
        "pharmacy_items": items,
        "low_stock_items": low_stock,
        "active_courses": courses,
        "medical_disclaimer_required": True,
    }


def get_challenges_status() -> dict:
    """Faol chelenjlar va bugungi vazifalar progressini qaytaradi."""
    d = app.today()
    ch_rows = db.q("SELECT * FROM challenges WHERE status = 'active' ORDER BY id ASC")
    out = []
    ch_mod = app.OPTIONAL.get("challenges")
    for c in ch_rows:
        tasks = db.q("SELECT id, title, task_type, target_value, unit, schedule_days FROM challenge_tasks WHERE challenge_id = ? AND active = 1", (c["id"],))
        logs = {
            r["task_id"]: r
            for r in db.q("SELECT task_id, value, status FROM challenge_logs WHERE challenge_id = ? AND log_date = ?", (c["id"], d.isoformat()))
        }
        prog = ch_mod.challenge_progress(c["id"], d) if ch_mod and hasattr(ch_mod, "challenge_progress") else {}
        out.append({
            "id": c["id"],
            "title": c["title"],
            "start_date": c["start_date"],
            "end_date": c["end_date"],
            "freezes_used": c.get("freezes_used", 0),
            "max_freezes": c.get("max_freezes", 2),
            "progress": prog,
            "tasks": [{**t, "today_log": logs.get(t["id"])} for t in tasks],
        })
    return {"date": d.isoformat(), "active_challenges": out}


def get_daily_status() -> dict:
    """Namoz/qazo, uyqu, vazn, `!` qaydlar va ob-havo/kiyinish tavsiyasini qaytaradi."""
    d = app.today()
    prayers = None
    try:
        prayers = sv.fetch_prayer_times(sv.get_city(), app.COUNTRY, d)
    except Exception:
        prayers = None
    open_qazo = qz.open_qazo()
    last_sleep = db.q1("SELECT * FROM sleep_log ORDER BY id DESC LIMIT 1")
    last_weight = db.q1("SELECT * FROM weight_log ORDER BY at DESC LIMIT 1")
    notes = db.q("SELECT id, text, created_at FROM notes WHERE date = ? ORDER BY id ASC", (d.isoformat(),))
    weather_summary = None
    try:
        wdata = sv.get_weather_data()
        weather_summary = sv.analyze_weather_for_date(wdata["raw"], d)
        if weather_summary and "date" in weather_summary:
            weather_summary = {**weather_summary, "date": str(weather_summary["date"])}
    except Exception:
        weather_summary = None
    return {
        "date": d.isoformat(),
        "now": app.now().strftime("%H:%M"),
        "city": sv.get_city(),
        "prayer_times": prayers,
        "open_qazo": open_qazo,
        "last_sleep": last_sleep,
        "last_weight": last_weight,
        "today_notes": notes,
        "weather": weather_summary,
    }


def get_schedules_and_reminders() -> dict:
    """Barcha system_schedules, schedule_notes, dynamic_reminders va ayni damdagi busy_block holatini qaytaradi."""
    return {
        "now": app.now().strftime("%Y-%m-%d %H:%M"),
        "system_schedules": schedules.list_system_schedules(),
        "active_sticky_notes": schedules.list_schedule_notes(only_active=True),
        "dynamic_reminders": schedules.list_dynamic_reminders(only_enabled=False),
        "currently_busy_blocks": schedules.get_active_busy_blocks(app.now()),
    }


READ_ONLY_TOOLS = {
    "get_finance_summary": get_finance_summary,
    "get_market_info": get_market_info,
    "get_school_schedule": get_school_schedule,
    "get_uni_schedule": get_uni_schedule,
    "get_health_status": get_health_status,
    "get_challenges_status": get_challenges_status,
    "get_daily_status": get_daily_status,
    "get_schedules_and_reminders": get_schedules_and_reminders,
}


# ==================================================================
# 2. WRITE / MUTATING TOOLS — HUMAN-IN-THE-LOOP PENDING QUEUE
# ==================================================================

_CASH_EXACT_ALIASES = {
    "naqd", "naqt", "naqd pul", "naqt pul", "naqdpul", "cash", "kesh",
    "hamyon", "chontak", "cho'ntak", "qolda", "qo'lda", "nalichka", "nal",
}
_CASH_WORD_RE = re.compile(r"(?:^|\b)(naqd|naqt|cash|kesh|nalichka)(?:\b|$)", re.IGNORECASE)
_CREDIT_EXACT_ALIASES = {"kredit", "kredit karta", "credit", "credit card"}

_WEEKDAY_NAME_MAP = {
    "du": "0", "dushanba": "0", "mon": "0", "monday": "0", "пн": "0",
    "se": "1", "seshanba": "1", "tue": "1", "tuesday": "1", "вт": "1",
    "ch": "2", "chor": "2", "chorshanba": "2", "wed": "2", "wednesday": "2", "ср": "2",
    "pa": "3", "pay": "3", "payshanba": "3", "thu": "3", "thursday": "3", "чт": "3",
    "ju": "4", "juma": "4", "fri": "4", "friday": "4", "пт": "4",
    "sh": "5", "shanba": "5", "sat": "5", "saturday": "5", "сб": "5",
    "ya": "6", "yak": "6", "yakshanba": "6", "sun": "6", "sunday": "6", "вс": "6",
}

_SCHEDULE_KEY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "morning_summary": ("tonggi xulosa", "ertalabki xulosa", "tonggi reja", "morning summary", "morning_summary"),
    "morning_hygiene": ("tonggi gigiyena", "gigiyena", "skin care", "skincare", "parvarish", "morning_hygiene"),
    "daily_notice": ("tug'ilgan kun", "tugilgan kun", "tugilgan kunlar", "birthday", "daily_notice"),
    "finance_alerts": ("moliya ertalab", "ertalabki moliya", "moliya ogohlantirishlari ertalab", "finance_alerts"),
    "finance_alerts_pm": ("moliya tushdan keyin", "tushdan keyingi moliya", "kechki moliya", "finance_alerts_pm"),
    "uni_bag_pack": ("sumka", "sumka yig'ish", "sumka yigish", "uni_bag_pack"),
    "uni_leave": ("uydan chiqish", "universitetga chiqish", "chiqish vaqti", "uni_leave"),
    "edu_evening_preview": ("ertangi darslar", "darslar rejasi", "maktab reja", "edu_evening_preview"),
    "evening_summary": ("kechki xulosa", "kechki kun yakuni", "kun yakuni", "evening_summary"),
}

_PRAYER_ALIASES = {
    "fajr": "fajr", "bomdod": "fajr",
    "dhuhr": "dhuhr", "peshin": "dhuhr",
    "asr": "asr",
    "maghrib": "maghrib", "shom": "maghrib",
    "isha": "isha", "xufton": "isha",
}


def _clean_str(val: Any) -> str:
    if val is None or isinstance(val, bool):
        return ""
    s = str(val).strip()
    if s.lower() in ("none", "null", "undefined"):
        return ""
    return s


def _first_non_empty(p: dict, *keys: str) -> str:
    for k in keys:
        v = _clean_str(p.get(k))
        if v:
            return v
    return ""


def _norm_text(s: Any) -> str:
    txt = _clean_str(s).lower()
    for ch in ("`", "ʻ", "ʼ", "’", "‘", "´"):
        txt = txt.replace(ch, "'")
    return re.sub(r"\s+", " ", txt).strip()


def _parse_hhmm(val: Any) -> tuple[str | None, str | None]:
    """
    Vaqtni `HH:MM` formatiga keltiradi. Agar ISO sana+vaqt (`2026-11-28 14:30`) bo'lsa,
    sanani ham `(hhmm, 'YYYY-MM-DD')` ko'rinishida qaytaradi.
    """
    s = _clean_str(val)
    if not s:
        return None, None
    extracted_date: str | None = None
    iso_m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2})[:.](\d{2})(?::\d{2})?$", s)
    if iso_m:
        try:
            date.fromisoformat(iso_m.group(1))
            extracted_date = iso_m.group(1)
            hh, mm = int(iso_m.group(2)), int(iso_m.group(3))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return f"{hh:02d}:{mm:02d}", extracted_date
        except ValueError:
            return None, None
        return None, None

    tm = re.match(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?$", s)
    if not tm:
        return None, None
    hh, mm = int(tm.group(1)), int(tm.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return f"{hh:02d}:{mm:02d}", None
    return None, None


def _resolve_date_token(val: Any) -> str | None:
    s = _norm_text(val)
    if not s:
        return None
    if s in ("today", "bugun"):
        return app.today().isoformat()
    if s in ("tomorrow", "ertaga"):
        return (app.today() + timedelta(days=1)).isoformat()
    if s in ("yesterday", "kecha"):
        return (app.today() - timedelta(days=1)).isoformat()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None
    return None


def _normalize_days_of_week(days_val: Any, date_val: Any = None, extracted_date: str | None = None) -> str:
    if extracted_date:
        return f"once:{extracted_date}"
    d_tok = _resolve_date_token(date_val)
    if d_tok:
        return f"once:{d_tok}"

    if isinstance(days_val, (list, tuple, set)):
        parts = [_norm_text(x) for x in days_val if _clean_str(x)]
    else:
        raw = _norm_text(days_val)
        if not raw or raw in ("all", "har kuni", "everyday", "daily"):
            return "all"
        if raw.startswith("once:"):
            sub = _resolve_date_token(raw[5:])
            return f"once:{sub}" if sub else raw
        direct_date = _resolve_date_token(raw)
        if direct_date:
            return f"once:{direct_date}"
        parts = [x.strip() for x in re.split(r"[,;\s]+", raw) if x.strip()]

    nums: list[str] = []
    for part in parts:
        if part.isdigit() and 0 <= int(part) <= 6:
            if part not in nums:
                nums.append(part)
        elif part in _WEEKDAY_NAME_MAP:
            d_num = _WEEKDAY_NAME_MAP[part]
            if d_num not in nums:
                nums.append(d_num)
    return ",".join(nums) if nums else "all"


def _normalize_amount(val: Any) -> int:
    if val is None or isinstance(val, bool):
        raise ValueError("Summa ko'rsatilmagan.")
    if isinstance(val, (int, float)):
        return int(round(float(val)))
    s = _norm_text(val)
    if not s:
        raise ValueError("Summa ko'rsatilmagan.")
    s = re.sub(r"(so'm|som|uzs)", "", s).strip()
    mult = 1.0
    if re.search(r"\b(mln|million)\b", s) or s.endswith("mln"):
        mult = 1_000_000.0
        s = re.sub(r"\b(mln|million)\b|mln$", "", s).strip()
    elif re.search(r"\b(ming)\b", s) or re.search(r"\d\s*k$", s):
        mult = 1_000.0
        s = re.sub(r"\bming\b|k$", "", s).strip()
    s = s.replace(" ", "").replace("\u00a0", "")
    # Minglik ajratuvchi vergullar: "45,000" yoki "1,500,000"
    if re.match(r"^[+-]?\d{1,3}(,\d{3})+$", s):
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return int(round(float(s) * mult))
    except ValueError as exc:
        raise ValueError(f"Noto'g'ri summa formati: {val}") from exc


def _parse_positive_float(val: Any, default: float = 1.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        v = float(val)
        return v if v > 0 else default
    s = _clean_str(val).replace(",", ".")
    if not s:
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return default
    v = float(m.group(0))
    return v if v > 0 else default


def _parse_int_opt(val: Any, default: int | None = None) -> int | None:
    if val is None:
        return default
    if isinstance(val, bool):
        return int(val)
    try:
        return int(round(float(str(val).strip())))
    except (TypeError, ValueError):
        return default


def _format_accounts_list(accs: list[dict]) -> str:
    parts = []
    type_lbl = {"cash": "naqd", "debit": "karta", "credit": "kredit karta"}
    for a in accs:
        last4 = f" *{a['last_4']}" if a.get("last_4") else ""
        tl = type_lbl.get(a.get("type") or "", a.get("type") or "")
        parts.append(f"• {a['name']}{last4}" + (f" ({tl})" if tl else ""))
    return "\n".join(parts)


def _format_schedules_list(scheds: list[dict]) -> str:
    return "\n".join(
        f"• {s['title']} ({schedules.describe_schedule(s)})"
        for s in scheds
    )


def _extract_account_query(p: dict, keys: tuple[str, ...]) -> str:
    return _first_non_empty(p, *keys)


def _user_mentions_account(user_text: str | None, acc: dict) -> bool:
    ut = _norm_text(user_text)
    if not ut:
        return False
    last4 = _clean_str(acc.get("last_4"))
    if last4 and last4 in ut:
        return True
    name_norm = _norm_text(acc.get("name"))
    if name_norm and len(name_norm) >= 3 and re.search(rf"(?:^|\b){re.escape(name_norm)}(?:\b|$)", ut):
        return True
    return False


def _detect_account_from_user_text(user_text: str | None, accs: list[dict]) -> dict | None:
    ut = _norm_text(user_text)
    if not ut or not accs:
        return None
    # 1) last_4 or exact account name mentioned in user_text
    direct = [a for a in accs if _user_mentions_account(ut, a)]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        return None
    # 2) cash keyword in user_text when exactly one cash account exists
    if _CASH_WORD_RE.search(ut):
        cash_accs = [a for a in accs if (a.get("type") or "").lower() == "cash" or _CASH_WORD_RE.search(_norm_text(a.get("name")))]
        if len(cash_accs) == 1:
            return cash_accs[0]
    return None


def resolve_account(
    query: Any,
    *,
    allow_single_default: bool = True,
    user_text: str | None = None,
    check_user_conflict: bool = False,
) -> tuple[dict | None, str, list[dict]]:
    """
    Moliya hisobini yuqori aniqlik bilan topadi ("Naqd" vs "Asosiy" aralashib ketmaydi).
    Returns: (matched_account, status, candidates)
      status: 'ok' | 'no_accounts' | 'missing_query' | 'not_found' | 'ambiguous' | 'conflict_cash'
    """
    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    if not accs:
        return None, "no_accounts", []

    q_raw = _clean_str(query)
    if not q_raw:
        if len(accs) == 1 and allow_single_default:
            return accs[0], "ok", [accs[0]]
        from_text = _detect_account_from_user_text(user_text, accs)
        if from_text is not None:
            return from_text, "ok", [from_text]
        return None, "missing_query", accs

    q_norm = _norm_text(q_raw)

    # 1) 4 xonali karta raqami (`last_4`) bo'yicha aniq qidiruv
    digits_4 = re.findall(r"\b\d{4}\b", q_norm)
    if digits_4:
        l4_matches = [a for a in accs if a.get("last_4") and a["last_4"] in digits_4]
        if len(l4_matches) == 1:
            return l4_matches[0], "ok", l4_matches
        if len(l4_matches) > 1:
            return None, "ambiguous", l4_matches

    # 2) Aniq ID bo'yicha qidiruv
    if q_norm.isdigit():
        id_matches = [a for a in accs if str(a["id"]) == q_norm]
        if len(id_matches) == 1:
            return id_matches[0], "ok", id_matches

    # 3) Hisob nomi bilan to'liq (exact) moslik
    exact_matches = [a for a in accs if _norm_text(a.get("name")) == q_norm]
    if len(exact_matches) == 1:
        matched = exact_matches[0]
        if check_user_conflict and _has_cash_conflict(user_text, matched):
            return None, "conflict_cash", accs
        return matched, "ok", exact_matches
    if len(exact_matches) > 1:
        return None, "ambiguous", exact_matches

    # 4) "Naqd" / cash sinonimlari — HECH QACHON debit/Asosiy hisobga tushib qolmaydi!
    if q_norm in _CASH_EXACT_ALIASES or _CASH_WORD_RE.search(q_norm):
        cash_matches = [
            a for a in accs
            if (a.get("type") or "").lower() == "cash"
            or _CASH_WORD_RE.search(_norm_text(a.get("name")))
            or _norm_text(a.get("name")) in _CASH_EXACT_ALIASES
        ]
        if len(cash_matches) == 1:
            return cash_matches[0], "ok", cash_matches
        if len(cash_matches) > 1:
            # Qo'shimcha so'zlar bilan toraytirish (masalan "naqd usd")
            q_tokens = [t for t in q_norm.split() if t not in ("naqd", "naqt", "pul", "hisob", "hisobdan", "cash")]
            if q_tokens:
                narrowed = [a for a in cash_matches if all(t in _norm_text(a.get("name")) for t in q_tokens)]
                if len(narrowed) == 1:
                    return narrowed[0], "ok", narrowed
                if len(narrowed) > 1:
                    return None, "ambiguous", narrowed
            return None, "ambiguous", cash_matches
        return None, "not_found", accs

    # 5) Kredit karta sinonimlari
    if q_norm in _CREDIT_EXACT_ALIASES:
        cred_matches = [a for a in accs if (a.get("type") or "").lower() == "credit"]
        if len(cred_matches) == 1:
            return cred_matches[0], "ok", cred_matches
        if len(cred_matches) > 1:
            return None, "ambiguous", cred_matches
        return None, "not_found", accs

    # 6) Qisman moslik (nom ichida qidiruv yoki so'rov ichida hisob nomi)
    sub_matches = []
    for a in accs:
        aname = _norm_text(a.get("name"))
        if not aname:
            continue
        if q_norm in aname or (len(aname) >= 3 and re.search(rf"(?:^|\b){re.escape(aname)}(?:\b|$)", q_norm)):
            sub_matches.append(a)
    if len(sub_matches) == 1:
        matched = sub_matches[0]
        if check_user_conflict and _has_cash_conflict(user_text, matched):
            return None, "conflict_cash", accs
        return matched, "ok", sub_matches
    if len(sub_matches) > 1:
        return None, "ambiguous", sub_matches

    return None, "not_found", accs


def _has_cash_conflict(user_text: str | None, matched_acc: dict) -> bool:
    """Foydalanuvchi aniq 'naqd' deganida model 'Asosiy' (debit) hisobni yuborgan bo'lsa True qaytaradi."""
    ut = _norm_text(user_text)
    if not ut or not _CASH_WORD_RE.search(ut):
        return False
    if (matched_acc.get("type") or "").lower() == "cash":
        return False
    if _CASH_WORD_RE.search(_norm_text(matched_acc.get("name"))):
        return False
    if _user_mentions_account(ut, matched_acc):
        return False
    return True


def _account_clarification_message(query: str, status: str, candidates: list[dict], role_label: str = "hisob") -> str:
    if status == "no_accounts":
        return "Sizda hali faol moliya hisobi qo'shilmagan. Avval «💰 Moliya» bo'limidan hisob yarating."
    acc_list = _format_accounts_list(candidates)
    if status == "conflict_cash":
        return (
            "Siz «Naqd» hisobni nazarda tutdingizmi? Qaysi hisobdan bajarishimizni aniq yozing:\n"
            f"{acc_list}"
        )
    if status == "ambiguous":
        return (
            f"«{query}» bir nechta {role_label}ga mos keldi. Qaysi birini tanlaylik?\n"
            f"{acc_list}"
        )
    if status == "not_found":
        return (
            f"«{query}» nomli faol {role_label} topilmadi. Qaysi hisobni ishlataylik?\n"
            f"{acc_list}"
        )
    return (
        f"Qaysi {role_label}ni ishlatishimizni aniqlashtiring:\n"
        f"{acc_list}"
    )


def _find_account(query: str | None) -> dict | None:
    acc, status, _ = resolve_account(query, allow_single_default=True)
    return acc if status == "ok" else None


def resolve_schedule_key(p: dict) -> tuple[dict | None, str, list[dict]]:
    """
    `schedule_update` / `schedule_add_note` uchun `schedule_key` ni topadi.
    Agar aniq kalit berilmagan bo'lsa, nom/sinonim bo'yicha mos yagona jadvalni izlaydi.
    """
    all_scheds = schedules.list_system_schedules()
    if not all_scheds:
        return None, "empty", []

    raw = _first_non_empty(p, "schedule_key", "key", "schedule_name", "title", "name")
    if not raw:
        return None, "missing", all_scheds

    q_norm = _norm_text(raw)
    # 1) Exact key match
    for s in all_scheds:
        if _norm_text(s["key"]) == q_norm:
            return s, "ok", [s]

    # 2) Synonym match
    syn_matches = []
    for s in all_scheds:
        syns = _SCHEDULE_KEY_SYNONYMS.get(s["key"], ())
        if any(q_norm == _norm_text(syn) or _norm_text(syn) in q_norm for syn in syns):
            syn_matches.append(s)
    if len(syn_matches) == 1:
        return syn_matches[0], "ok", syn_matches
    if len(syn_matches) > 1:
        return None, "ambiguous", syn_matches

    # 3) Substring match on key or title
    sub_matches = []
    for s in all_scheds:
        k_norm = _norm_text(s["key"]).replace("_", " ")
        t_norm = _norm_text(s.get("title"))
        if q_norm in k_norm or q_norm in t_norm or (len(k_norm) >= 4 and k_norm in q_norm):
            sub_matches.append(s)
    if len(sub_matches) == 1:
        return sub_matches[0], "ok", sub_matches
    if len(sub_matches) > 1:
        return None, "ambiguous", sub_matches

    return None, "not_found", all_scheds


def validate_and_normalize_action(
    atype: str,
    params: dict | None,
    *,
    user_text: str | None = None,
) -> tuple[dict, str | None]:
    """
    Har bir write amal argumentlarini xavfsiz tekshiradi va normallashtiradi.
    Hech qachon KeyError chiqarmaydi.
    Returns: (normalized_params, clarification_question_if_invalid_or_missing)
    """
    p = dict(params) if isinstance(params, dict) else {}

    # ---- 1. MOLIYA ----
    if atype in ("finance_expense", "finance_income"):
        raw_amt = p.get("amount")
        try:
            amount = _normalize_amount(raw_amt)
        except ValueError:
            return p, "Summani aniq kiriting (masalan, 45 000 so'm)."
        if amount <= 0:
            return p, "Summa musbat bo'lishi kerak. Qancha so'm ekanini yozing."
        p["amount"] = amount
        acc_q = _extract_account_query(p, ("account_query", "account", "account_name", "account_id", "card", "account_type"))
        acc, st, cands = resolve_account(acc_q, allow_single_default=True, user_text=user_text, check_user_conflict=True)
        if st != "ok" or not acc:
            return p, _account_clarification_message(acc_q, st, cands, "hisob")
        p["account_query"] = str(acc["id"])
        p["resolved_account_name"] = acc["name"]
        return p, None

    if atype == "finance_transfer":
        try:
            amount = _normalize_amount(p.get("amount"))
        except ValueError:
            return p, "O'tkazma summasini aniq kiriting (masalan, 100 000 so'm)."
        if amount <= 0:
            return p, "O'tkazma summasi musbat bo'lishi kerak."
        p["amount"] = amount
        q_from = _extract_account_query(p, ("from_account_query", "from_account", "source_account", "from_account_id"))
        q_to = _extract_account_query(p, ("to_account_query", "to_account", "target_account", "to_account_id"))
        a_from, st_f, c_f = resolve_account(q_from, allow_single_default=False)
        if st_f != "ok" or not a_from:
            return p, _account_clarification_message(q_from, st_f, c_f, "chiqim hisobi")
        a_to, st_t, c_t = resolve_account(q_to, allow_single_default=False)
        if st_t != "ok" or not a_to:
            return p, _account_clarification_message(q_to, st_t, c_t, "kirim hisobi")
        if a_from["id"] == a_to["id"]:
            return p, f"O'tkazma uchun ikkita har xil hisob kerak ({a_from['name']} bir xil bo'lib qoldi). Qaysi hisobdan qaysi hisobga o'tkazaylik?"
        p["from_account_query"] = str(a_from["id"])
        p["to_account_query"] = str(a_to["id"])
        return p, None

    if atype == "finance_add_debt":
        person = _first_non_empty(p, "person_name", "person", "name")
        if not person:
            return p, "Qarz/haq kimga tegishli ekanini (ismni) yozing."
        try:
            amt = _normalize_amount(p.get("amount"))
        except ValueError:
            return p, f"{person} bo'yicha qarz/haq summasini aniq kiriting."
        if amt == 0:
            return p, "Qarz/haq summasi 0 bo'lishi mumkin emas."
        p["person_name"] = person
        p["amount"] = amt
        return p, None

    if atype == "finance_settle_debt":
        q_p = _first_non_empty(p, "person_name_or_id", "person_name", "person", "debt_id", "id")
        if not q_p:
            return p, "Qaysi shaxsning qarzini yopishimizni (ismini) yozing."
        p["person_name_or_id"] = q_p
        acc_q = _extract_account_query(p, ("account_query", "account", "account_name", "account_id", "card", "account_type"))
        if acc_q:
            acc, st, cands = resolve_account(acc_q, allow_single_default=True, user_text=user_text, check_user_conflict=True)
            if st != "ok" or not acc:
                return p, _account_clarification_message(acc_q, st, cands, "hisob")
            p["account_query"] = str(acc["id"])
        return p, None

    # ---- 2. BOZORLIK ----
    if atype in ("market_add_planned", "market_remove_planned"):
        item_name = _first_non_empty(p, "item_name", "name", "item", "product")
        if not item_name:
            return p, "Bozorlik mahsuloti nomini aniq yozing."
        p["item_name"] = item_name
        if atype == "market_add_planned":
            p["quantity"] = _parse_positive_float(p.get("quantity"), default=1.0)
        return p, None

    # ---- 3. MAKTAB ----
    if atype == "school_add_class_note":
        q_c = _first_non_empty(p, "class_name_or_course_id", "class_name", "course_id", "class")
        note_txt = _first_non_empty(p, "note_text", "note", "text")
        if not q_c:
            return p, "Qaysi sinfga izoh qo'shaylik?"
        if not note_txt:
            return p, f"{q_c} sinfiga qanday izoh qo'shaylik?"
        p["class_name_or_course_id"] = q_c
        p["note_text"] = note_txt
        return p, None

    # ---- 4. UNIVERSITET ----
    if atype == "uni_add_homework":
        q_s = _first_non_empty(p, "subject_name_or_id", "subject_name", "subject", "subject_id")
        task_txt = _first_non_empty(p, "task_text", "task", "text", "homework")
        if not q_s:
            return p, "Uy ishini qaysi universitet faniga qo'shaylik?"
        if not task_txt:
            return p, f"{q_s} fanidan uy ishi matnini yozing."
        p["subject_name_or_id"] = q_s
        p["task_text"] = task_txt
        return p, None

    if atype == "uni_complete_homework":
        q_h = _first_non_empty(p, "homework_id_or_query", "homework_id", "query", "task_text", "id")
        if not q_h:
            return p, "Qaysi uy ishini bajarildi deb belgilaylik?"
        p["homework_id_or_query"] = q_h
        return p, None

    if atype == "uni_complete_work_step":
        q_w = _first_non_empty(p, "work_title_or_id", "work_title", "work_id", "title", "id")
        if not q_w:
            return p, "Qaysi akademik ish (BMI/kurs ishi) bosqichini yopaylik?"
        p["work_title_or_id"] = q_w
        if p.get("step_order") is not None:
            parsed_step = _parse_int_opt(p.get("step_order"))
            if parsed_step is None or parsed_step <= 0:
                return p, "Bosqich raqami musbat butun son bo'lishi kerak."
            p["step_order"] = parsed_step
        return p, None

    # ---- 5. SALOMATLIK ----
    if atype == "health_log_intake":
        q_i = _first_non_empty(p, "item_name_or_id", "item_name", "name", "id")
        if not q_i:
            return p, "Qaysi dori/vosita qabulini qayd etaylik?"
        p["item_name_or_id"] = q_i
        return p, None

    if atype == "health_add_item":
        name = _first_non_empty(p, "name", "item_name", "medicine_name")
        if not name:
            return p, "Aptechkaga qo'shiladigan dori yoki vosita nomini yozing."
        p["name"] = name
        p["quantity"] = _parse_positive_float(p.get("quantity"), default=1.0)
        return p, None

    # ---- 6. CHELENJ ----
    if atype == "challenge_complete_task":
        q_t = _first_non_empty(p, "task_title_or_id", "task_title", "task_id", "title", "id")
        if not q_t:
            return p, "Qaysi chelenj vazifasini bajarildi deb belgilaylik?"
        p["task_title_or_id"] = q_t
        return p, None

    if atype == "challenge_use_freeze":
        q_c = _first_non_empty(p, "challenge_title_or_id", "challenge_title", "challenge_id", "title", "id")
        if not q_c:
            return p, "Qaysi chelenjda Skip Day (muzlatish) qo'llaylik?"
        p["challenge_title_or_id"] = q_c
        return p, None

    # ---- 7. KUNDALIK ----
    if atype == "daily_add_note":
        txt = _first_non_empty(p, "text", "note_text", "note")
        if not txt:
            return p, "Qanday tezkor qayd qo'shishni xohlaysiz?"
        p["text"] = txt
        return p, None

    if atype == "daily_delete_note":
        q_n = _first_non_empty(p, "note_id_or_query", "note_id", "query", "text", "id")
        if not q_n:
            return p, "Qaysi qayd o'chirilishini aniqlashtiring."
        p["note_id_or_query"] = q_n
        return p, None

    if atype == "daily_log_weight":
        raw_kg = p.get("kg") if p.get("kg") is not None else p.get("weight")
        kg = _parse_positive_float(raw_kg, default=0.0)
        if kg < 10.0 or kg > 350.0:
            return p, "Vazningizni kg ko'rinishida aniq kiriting (masalan, 72.5)."
        p["kg"] = kg
        return p, None

    if atype == "daily_sleep_action":
        act = _norm_text(p.get("action"))
        if act in ("yotdim", "uxladim", "sleep", "bed"):
            act = "bed"
        elif act in ("uyg'ondim", "uygondim", "turdim", "wake", "wakeup"):
            act = "wake"
        if act not in ("bed", "wake"):
            return p, "Uyqu holatini aniqlashtiring: yotish (`bed`) yoki uyg'onish (`wake`)?"
        p["action"] = act
        return p, None

    # ---- 8. DINAMIK TIZIM VAQTLARI ----
    if atype == "schedule_update":
        sched, st, cands = resolve_schedule_key(p)
        if st != "ok" or not sched:
            if st == "empty":
                return p, "Tizim jadvallari topilmadi."
            lst = _format_schedules_list(cands)
            if st == "ambiguous":
                return p, f"Bir nechta tizim jadvali mos keldi. Qaysi birini o'zgartiraylik?\n{lst}"
            return p, f"Qaysi tizim jadvalini yangilashimizni aniqlashtiring:\n{lst}"
        p["schedule_key"] = sched["key"]

        raw_time = _first_non_empty(p, "time_hhmm", "time", "new_time", "start_time")
        if raw_time:
            hhmm, _ = _parse_hhmm(raw_time)
            if not hhmm:
                return p, f"«{sched['title']}» uchun vaqtni `HH:MM` formatida kiriting (masalan, 07:30)."
            p["time_hhmm"] = hhmm

        raw_pref = _first_non_empty(p, "prayer_ref", "prayer")
        if raw_pref:
            mapped_pref = _PRAYER_ALIASES.get(_norm_text(raw_pref))
            if not mapped_pref:
                return p, "Namoz nomini aniqlashtiring: Bomdod, Peshin, Asr, Shom yoki Xufton?"
            p["prayer_ref"] = mapped_pref

        if p.get("offset_minutes") is not None:
            off = _parse_int_opt(p.get("offset_minutes"))
            if off is None:
                return p, "Namozga nisbatan siljish daqiqasini butun son bilan kiriting (masalan, +15 yoki -10)."
            p["offset_minutes"] = off

        if p.get("is_enabled") is not None:
            en = _parse_int_opt(p.get("is_enabled"))
            if en is None:
                return p, "Jadvalni yoqish (1) yoki o'chirish (0) kerakligini aniqlashtiring."
            p["is_enabled"] = 1 if en else 0

        if p.get("only_today") and not p.get("time_hhmm"):
            return p, f"«{sched['title']}» ni bugun uchun qaysi soatga suraylik? (masalan, 08:00)"

        if _norm_text(p.get("mode")) == "relative_prayer" and not (p.get("prayer_ref") or sched.get("prayer_ref")):
            return p, f"«{sched['title']}» qaysi namoz vaqtiga bog'lansin? (Bomdod, Peshin, Asr, Shom, Xufton)"

        has_change = any(
            p.get(k) is not None
            for k in ("mode", "time_hhmm", "prayer_ref", "offset_minutes", "is_enabled")
        )
        if not has_change:
            return p, f"«{sched['title']}» jadvalini qaysi soatga yoki holatga o'zgartiraylik?"
        return p, None

    # ---- 9. QO'SHIMCHA IZOHLAR (STICKY NOTES) ----
    if atype == "schedule_add_note":
        sched, st, cands = resolve_schedule_key(p)
        if st != "ok" or not sched:
            lst = _format_schedules_list(cands) if cands else ""
            return p, f"Izohni qaysi tizim jadvaliga biriktiraylik?\n{lst}".strip()
        note_txt = _first_non_empty(p, "note_text", "note", "text")
        if not note_txt:
            return p, f"«{sched['title']}» jadvaliga qanday izoh biriktiraylik?"
        p["schedule_key"] = sched["key"]
        p["note_text"] = note_txt
        p["note_type"] = "recurring" if _norm_text(p.get("note_type")) == "recurring" else "once"
        return p, None

    if atype == "schedule_remove_note":
        q_n = _first_non_empty(p, "note_id_or_query", "note_id", "query", "note_text", "id")
        if not q_n:
            return p, "Qaysi jadval izohini o'chirishimizni yozing."
        p["note_id_or_query"] = q_n
        if _first_non_empty(p, "schedule_key", "key"):
            sched, st, _ = resolve_schedule_key(p)
            if st == "ok" and sched:
                p["schedule_key"] = sched["key"]
        return p, None

    # ---- 10. DINAMIK ESLATMALAR ----
    if atype == "dynamic_reminder_add":
        title = _first_non_empty(p, "title", "name", "text", "label", "description")
        if not title:
            return p, "Eslatma matnini (sarlavhasini) va qaysi vaqt/sanada eslatishimizni yozing."
        raw_start = _first_non_empty(p, "start_time", "time", "time_hhmm", "hhmm", "at")
        start_hhmm, extracted_date = _parse_hhmm(raw_start)
        if not start_hhmm:
            return p, f"«{title}» eslatmasini qaysi vaqt va sanada qo'yaylik? (masalan, 14:30 yoki ertaga 09:00)"
        raw_end = _first_non_empty(p, "end_time")
        end_hhmm = None
        if raw_end:
            end_hhmm, _ = _parse_hhmm(raw_end)
            if not end_hhmm:
                return p, f"«{title}» uchun tugash vaqtini `HH:MM` formatida kiriting (masalan, 16:00)."
        p["title"] = title
        p["kind"] = "busy_block" if _norm_text(p.get("kind")) == "busy_block" else "deadline"
        p["start_time"] = start_hhmm
        p["end_time"] = end_hhmm
        p["days_of_week"] = _normalize_days_of_week(
            p.get("days_of_week"),
            date_val=p.get("date"),
            extracted_date=extracted_date,
        )
        p["remind_before_min"] = max(0, _parse_int_opt(p.get("remind_before_min"), default=0) or 0)
        return p, None

    if atype == "dynamic_reminder_update":
        q_r = _first_non_empty(p, "reminder_id_or_title", "reminder_id", "id", "target_title")
        if not q_r:
            # Agar faqat `title` berilgan va o'zgartirish maydonlari bo'lsa, uni qidiruv uchun ishlatamiz
            if _clean_str(p.get("title")) and any(p.get(k) is not None for k in ("start_time", "end_time", "days_of_week", "is_enabled")):
                q_r = _clean_str(p.get("title"))
            else:
                return p, "Qaysi dinamik eslatmani tahrirlashimizni (nomi yoki ID sini) yozing."
        p["reminder_id_or_title"] = q_r
        if p.get("start_time") is not None and _clean_str(p.get("start_time")):
            sh, ed = _parse_hhmm(p.get("start_time"))
            if not sh:
                return p, "Yangi boshlanish vaqtini `HH:MM` formatida kiriting (masalan, 15:00)."
            p["start_time"] = sh
            if ed and not p.get("days_of_week"):
                p["days_of_week"] = f"once:{ed}"
        elif "start_time" in p:
            p["start_time"] = None
        if p.get("end_time") is not None and _clean_str(p.get("end_time")):
            eh, _ = _parse_hhmm(p.get("end_time"))
            if not eh:
                return p, "Yangi tugash vaqtini `HH:MM` formatida kiriting."
            p["end_time"] = eh
        if p.get("days_of_week") is not None or p.get("date") is not None:
            p["days_of_week"] = _normalize_days_of_week(p.get("days_of_week"), date_val=p.get("date"))
        if p.get("is_enabled") is not None:
            en = _parse_int_opt(p.get("is_enabled"))
            if en is not None:
                p["is_enabled"] = 1 if en else 0
        return p, None

    if atype == "dynamic_reminder_delete":
        q_r = _first_non_empty(p, "reminder_id_or_title", "reminder_id", "id", "title")
        if not q_r:
            return p, "Qaysi dinamik eslatmani o'chirishimizni (nomi yoki ID sini) yozing."
        p["reminder_id_or_title"] = q_r
        return p, None

    return p, f"Qo'llab-quvvatlanmaydigan amal turi: {atype}"


def propose_actions(summary_text: str, actions: list[dict], *, user_text: str | None = None) -> dict:
    """
    Bazaga yozadigan/o'zgartiradigan amallarni TO'G'RIDAN-TO'G'RI bajarmasdan,
    avval barcha majburiy argumentlarni xavfsiz tekshiradi, so'ng `ai_pending_actions`
    jadvaliga foydalanuvchi tasdig'i uchun navbatga qo'yadi.
    Multi-Action qo'llab-quvvatlanadi (bir nechta modul amali bitta tasdiqlash kartochkasida).
    """
    if not isinstance(actions, list) or not actions:
        return {
            "needs_clarification": True,
            "clarification_question": "Qaysi amalni bajarishimizni biroz aniqroq yozing.",
            "error": "Amallar ro'yxati bo'sh bo'lishi mumkin emas.",
        }

    cleaned_actions = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        atype = _clean_str(act.get("type"))
        if atype not in SUPPORTED_WRITE_ACTIONS:
            return {"error": f"Qo'llab-quvvatlanmaydigan amal turi: {atype}"}
        raw_params = act.get("params") if isinstance(act.get("params"), dict) else {
            k: v for k, v in act.items() if k != "type"
        }
        norm_params, clarify_q = validate_and_normalize_action(atype, raw_params, user_text=user_text)
        if clarify_q:
            return {
                "needs_clarification": True,
                "clarification_question": clarify_q,
                "error": clarify_q,
                "action_type": atype,
            }
        cleaned_actions.append({"type": atype, "params": norm_params})

    if not cleaned_actions:
        return {
            "needs_clarification": True,
            "clarification_question": "Qaysi amalni bajarishimizni aniqroq yozing.",
            "error": "Yaroqli amal topilmadi.",
        }

    action_id = uuid.uuid4().hex[:10]
    summary = _clean_str(summary_text) or f"{len(cleaned_actions)} ta amal bajariladi."
    db.ex(
        "INSERT INTO ai_pending_actions (id, actions_json, summary_text, status, created_at) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (action_id, json.dumps(cleaned_actions, ensure_ascii=False), summary, app.now().isoformat()),
    )
    return {
        "pending_action_id": action_id,
        "summary_text": summary,
        "actions_count": len(cleaned_actions),
        "status": "pending_confirmation",
    }


def get_pending_action(action_id: str) -> dict | None:
    row = db.q1("SELECT * FROM ai_pending_actions WHERE id = ?", (action_id,))
    if not row:
        return None
    try:
        row["actions"] = json.loads(row["actions_json"])
    except Exception:
        row["actions"] = []
    return row


def cancel_pending_action(action_id: str) -> bool:
    return db.ex(
        "UPDATE ai_pending_actions SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
        (action_id,),
    ) > 0


# ------------------------------------------------------------------ Helpers for executing confirmed actions


def _exec_single_action(atype: str, p: dict) -> tuple[str, bool]:
    """Bitta tasdiqlangan amalni bajaradi. Returns: (human_result_line, needs_reschedule)"""
    p, err_q = validate_and_normalize_action(atype, p)
    if err_q:
        raise ValueError(err_q)

    # ---- 1. MOLIYA ----
    if atype in ("finance_expense", "finance_income", "finance_transfer"):
        import feat_finance as fin
        amount = _normalize_amount(p.get("amount", 0))
        if amount <= 0:
            raise ValueError("Summa musbat bo'lishi kerak.")
        note = _clean_str(p.get("note"))
        cat = _clean_str(p.get("category")) or ("Boshqa kirim" if atype == "finance_income" else "Boshqa")

        if atype == "finance_expense":
            acc, st, cands = resolve_account(p.get("account_query"), allow_single_default=True)
            if st != "ok" or not acc:
                raise ValueError(_account_clarification_message(_clean_str(p.get("account_query")), st, cands))
            fin.record_transaction(amount, cat, note, from_account_id=acc["id"])
            return f"💸 Chiqim yozildi: {fin.fmt_sum(amount)} so'm ({acc['name']}, {cat})", False

        if atype == "finance_income":
            acc, st, cands = resolve_account(p.get("account_query"), allow_single_default=True)
            if st != "ok" or not acc:
                raise ValueError(_account_clarification_message(_clean_str(p.get("account_query")), st, cands))
            fin.record_transaction(amount, cat, note, to_account_id=acc["id"])
            return f"💰 Kirim yozildi: +{fin.fmt_sum(amount)} so'm ({acc['name']}, {cat})", False

        if atype == "finance_transfer":
            a_from, st_f, c_f = resolve_account(p.get("from_account_query"), allow_single_default=False)
            a_to, st_t, c_t = resolve_account(p.get("to_account_query"), allow_single_default=False)
            if st_f != "ok" or not a_from:
                raise ValueError(_account_clarification_message(_clean_str(p.get("from_account_query")), st_f, c_f, "chiqim hisobi"))
            if st_t != "ok" or not a_to:
                raise ValueError(_account_clarification_message(_clean_str(p.get("to_account_query")), st_t, c_t, "kirim hisobi"))
            fin.record_transaction(amount, "🔄 O'tkazma", note, from_account_id=a_from["id"], to_account_id=a_to["id"])
            return f"🔄 O'tkazma: {fin.fmt_sum(amount)} so'm ({a_from['name']} → {a_to['name']})", False

    if atype == "finance_add_debt":
        import feat_finance as fin
        person = _clean_str(p.get("person_name"))
        amt = _normalize_amount(p.get("amount"))
        fin.create_debt(person, amt, due_date=p.get("due_date"), note=_clean_str(p.get("note")))
        kind_s = "Haq" if amt > 0 else "Qarz"
        return f"🤝 {kind_s} qo'shildi: {person} ({fin.fmt_sum(abs(amt))} so'm)", False

    if atype == "finance_settle_debt":
        import feat_finance as fin
        q_p = _clean_str(p.get("person_name_or_id"))
        debt = None
        if q_p.isdigit():
            debt = db.q1("SELECT * FROM finance_debts WHERE id = ? AND status = 'active'", (int(q_p),))
        if not debt:
            debt = db.q1("SELECT * FROM finance_debts WHERE LOWER(person_name) LIKE LOWER(?) AND status = 'active' LIMIT 1", (f"%{q_p}%",))
        if not debt:
            raise ValueError(f"Faol qarz topilmadi: {q_p}")
        raw_amt = p.get("amount")
        amt = _normalize_amount(raw_amt) if raw_amt is not None else abs(int(debt["amount"]))
        acc = _find_account(p.get("account_query")) if p.get("account_query") else None
        fin.settle_debt(debt["id"], amt, account_id=acc["id"] if acc else None)
        return f"✅ Qarz yopildi: {debt['person_name']} ({fin.fmt_sum(amt)} so'm)", False

    # ---- 2. BOZORLIK ----
    if atype == "market_add_planned":
        import feat_market as mkt
        item_name = _clean_str(p.get("item_name"))
        qty = _parse_positive_float(p.get("quantity"), default=1.0)
        unit = _clean_str(p.get("unit")) or "dona"
        loc_id = mkt.get_or_create_location(p.get("location")) if p.get("location") else None
        basket = db.q1("SELECT id FROM market_baskets WHERE mode = 'planned' AND status = 'draft' ORDER BY created_at DESC LIMIT 1")
        if basket:
            bid = basket["id"]
        else:
            bid = str(uuid.uuid4())
            db.ex("INSERT INTO market_baskets (id, title, mode, status, created_at) VALUES (?, 'Rejalashtirilgan', 'planned', 'draft', ?)",
                  (bid, app.now().isoformat()))
        item = mkt.get_or_create_item(item_name, unit=unit)
        db.ins(
            "INSERT INTO market_basket_items (basket_id, item_id, location_id, quantity, unit, unit_price, total_price, is_bought) "
            "VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
            (bid, item["id"], loc_id, qty, unit),
        )
        return f"🛒 Bozorlikka qo'shildi: {item['name']} ({mkt.fmt_qty(qty)} {unit})", False

    if atype == "market_remove_planned":
        item_name = _clean_str(p.get("item_name"))
        row = db.q1(
            "SELECT bi.id, mi.name FROM market_basket_items bi "
            "JOIN market_items mi ON mi.id = bi.item_id "
            "JOIN market_baskets b ON b.id = bi.basket_id "
            "WHERE b.mode = 'planned' AND b.status = 'draft' AND LOWER(mi.name) LIKE LOWER(?) LIMIT 1",
            (f"%{item_name}%",),
        )
        if not row:
            raise ValueError(f"Rejalashtirilgan bozorlikda topilmadi: {item_name}")
        db.ex("DELETE FROM market_basket_items WHERE id = ?", (row["id"],))
        return f"🗑 Bozorlikdan o'chirildi: {row['name']}", False

    # ---- 3. MAKTAB ----
    if atype == "school_add_class_note":
        import feat_education as edu
        q_c = _clean_str(p.get("class_name_or_course_id"))
        note_txt = _clean_str(p.get("note_text"))
        course = None
        if q_c.isdigit():
            course = edu.get_course(int(q_c))
        if not course:
            course = db.q1("SELECT * FROM edu_courses WHERE is_active = 1 AND LOWER(class_name) LIKE LOWER(?) LIMIT 1", (f"%{q_c}%",))
        if not course:
            raise ValueError(f"Sinf topilmadi: {q_c}")
        edu.add_class_note(course["id"], note_txt)
        return f"🏫 {course['class_name']} sinfiga izoh qo'shildi: {note_txt}", False

    # ---- 4. UNIVERSITET ----
    if atype == "uni_add_homework":
        import feat_uni as uni
        q_s = _clean_str(p.get("subject_name_or_id"))
        task_txt = _clean_str(p.get("task_text"))
        subj = None
        if q_s.isdigit():
            subj = db.q1("SELECT * FROM uni_subjects WHERE id = ? AND is_active = 1", (int(q_s),))
        if not subj:
            subj = db.q1("SELECT * FROM uni_subjects WHERE is_active = 1 AND LOWER(name) LIKE LOWER(?) LIMIT 1", (f"%{q_s}%",))
        if not subj:
            raise ValueError(f"Universitet fani topilmadi: {q_s}")
        ltype = p.get("lesson_type")
        due = p.get("due_date")
        if not due:
            nxt = uni.find_next_lesson_date(subj["id"], ltype)
            due = nxt.isoformat() if nxt else None
        uni.add_homework(subj["id"], ltype, task_txt, due_date=due)
        return f"🏛 Uy ishi qo'shildi: {subj['name']} — {task_txt}", False

    if atype == "uni_complete_homework":
        import feat_uni as uni
        q_h = _clean_str(p.get("homework_id_or_query"))
        hw = None
        if q_h.isdigit():
            hw = db.q1("SELECT * FROM uni_homeworks WHERE id = ?", (int(q_h),))
        if not hw:
            hw = db.q1("SELECT * FROM uni_homeworks WHERE is_done = 0 AND LOWER(task_text) LIKE LOWER(?) LIMIT 1", (f"%{q_h}%",))
        if not hw:
            raise ValueError(f"Uy ishi topilmadi: {q_h}")
        if not hw["is_done"]:
            uni.toggle_homework(hw["id"])
        return f"✅ Uy ishi bajarildi: {hw['task_text']}", False

    if atype == "uni_complete_work_step":
        import feat_uni as uni
        q_w = _clean_str(p.get("work_title_or_id"))
        work = None
        if q_w.isdigit():
            work = db.q1("SELECT * FROM uni_works WHERE id = ?", (int(q_w),))
        if not work:
            work = db.q1("SELECT * FROM uni_works WHERE status = 'active' AND LOWER(title) LIKE LOWER(?) LIMIT 1", (f"%{q_w}%",))
        if not work:
            raise ValueError(f"Akademik ish topilmadi: {q_w}")
        step_order = p.get("step_order")
        if step_order is not None:
            step = db.q1("SELECT * FROM uni_work_steps WHERE work_id = ? AND step_order = ?", (work["id"], int(step_order)))
        else:
            step = db.q1("SELECT * FROM uni_work_steps WHERE work_id = ? AND is_done = 0 ORDER BY step_order ASC LIMIT 1", (work["id"],))
        if not step:
            raise ValueError("Yopiladigan bosqich topilmadi.")
        if not step["is_done"]:
            uni.toggle_work_step(step["id"])
        return f"🎓 {work['title']}: «{step['step_title']}» bosqichi bajarildi", False

    # ---- 5. SALOMATLIK ----
    if atype == "health_log_intake":
        import feat_health as hl
        q_i = _clean_str(p.get("item_name_or_id"))
        item = None
        if q_i.isdigit():
            item = db.q1("SELECT * FROM health_items WHERE id = ?", (int(q_i),))
        if not item:
            item = db.q1("SELECT * FROM health_items WHERE is_active = 1 AND LOWER(name) LIKE LOWER(?) ORDER BY remaining_quantity DESC LIMIT 1", (f"%{q_i}%",))
        if not item:
            raise ValueError(f"Aptechkada topilmadi: {q_i}")
        hl.execute_intake(item["id"])
        return f"💊 Qabul qayd etildi: {item['name']}", False

    if atype == "health_add_item":
        name = _clean_str(p.get("name"))
        qty = _parse_positive_float(p.get("quantity"), default=1.0)
        unit = _clean_str(p.get("unit")) or "dona"
        loc = _clean_str(p.get("location")) or "home"
        form = _clean_str(p.get("form")) or "tabletka"
        notes = _clean_str(p.get("notes"))
        existing = db.q1("SELECT * FROM health_items WHERE LOWER(name) = LOWER(?) AND is_active = 1", (name,))
        if existing:
            db.ex("UPDATE health_items SET remaining_quantity = COALESCE(remaining_quantity, 0) + ?, is_seed = 0 WHERE id = ?", (qty, existing["id"]))
            return f"💊 Aptechka yangilandi: {existing['name']} (+{qty:g} {unit})", False
        db.ins(
            "INSERT INTO health_items (name, kind, location, form, remaining_quantity, unit, low_stock_threshold, is_active, is_seed, notes) "
            "VALUES (?, 'medicine', ?, ?, ?, ?, 2, 1, 0, ?)",
            (name, loc, form, qty, unit, notes),
        )
        return f"💊 Aptechkaga qo'shildi: {name} ({qty:g} {unit})", False

    # ---- 6. CHELENJ ----
    if atype == "challenge_complete_task":
        import feat_challenges as ch
        q_t = _clean_str(p.get("task_title_or_id"))
        task = None
        if q_t.isdigit():
            task = db.q1("SELECT * FROM challenge_tasks WHERE id = ?", (int(q_t),))
        if not task:
            task = db.q1(
                "SELECT t.* FROM challenge_tasks t JOIN challenges c ON c.id = t.challenge_id "
                "WHERE c.status = 'active' AND t.active = 1 AND LOWER(t.title) LIKE LOWER(?) LIMIT 1",
                (f"%{q_t}%",),
            )
        if not task:
            raise ValueError(f"Faol chelenj vazifasi topilmadi: {q_t}")
        d = app.today()
        val = _parse_positive_float(p.get("value"), default=float(task.get("target_value") or 1))
        db.ex(
            "INSERT INTO challenge_logs (challenge_id, task_id, log_date, value, status) VALUES (?, ?, ?, ?, 'done') "
            "ON CONFLICT(task_id, log_date) DO UPDATE SET value = excluded.value, status = 'done'",
            (task["challenge_id"], task["id"], d.isoformat(), val),
        )
        if hasattr(ch, "evaluate_day"):
            ch.evaluate_day(task["challenge_id"], d)
        return f"🏆 Chelenj vazifasi bajarildi: {task['title']}", False

    if atype == "challenge_use_freeze":
        q_c = _clean_str(p.get("challenge_title_or_id"))
        c = None
        if q_c.isdigit():
            c = db.q1("SELECT * FROM challenges WHERE id = ? AND status = 'active'", (int(q_c),))
        if not c:
            c = db.q1("SELECT * FROM challenges WHERE status = 'active' AND LOWER(title) LIKE LOWER(?) LIMIT 1", (f"%{q_c}%",))
        if not c:
            raise ValueError(f"Faol chelenj topilmadi: {q_c}")
        if int(c.get("freezes_used") or 0) >= int(c.get("max_freezes") or 2):
            raise ValueError("Muzlatish (Skip Day) limiti tugagan.")
        d_iso = app.today().isoformat()
        db.ex("INSERT OR IGNORE INTO challenge_freezes (challenge_id, freeze_date) VALUES (?, ?)", (c["id"], d_iso))
        db.ex("UPDATE challenges SET freezes_used = freezes_used + 1 WHERE id = ?", (c["id"],))
        return f"🧊 Skip Day (Muzlatish) qo'llandi: {c['title']} ({d_iso})", False

    # ---- 7. KUNDALIK ----
    if atype == "daily_add_note":
        txt = _clean_str(p.get("text"))
        db.ins("INSERT INTO notes (date, text, created_at) VALUES (?, ?, ?)", (app.today().isoformat(), txt, app.now().isoformat()))
        return f"📝 Tezkor qayd qo'shildi: {txt}", False

    if atype == "daily_delete_note":
        q_n = _clean_str(p.get("note_id_or_query"))
        if q_n.isdigit():
            db.ex("DELETE FROM notes WHERE id = ?", (int(q_n),))
            return f"🗑 Qayd #{q_n} o'chirildi", False
        row = db.q1("SELECT id, text FROM notes WHERE LOWER(text) LIKE LOWER(?) ORDER BY id DESC LIMIT 1", (f"%{q_n}%",))
        if not row:
            raise ValueError(f"Qayd topilmadi: {q_n}")
        db.ex("DELETE FROM notes WHERE id = ?", (row["id"],))
        return f"🗑 Qayd o'chirildi: {row['text']}", False

    if atype == "daily_log_weight":
        import feat_weight as wt
        kg = float(p.get("kg"))
        wt.save(kg, primary=False)
        return f"⚖️ Vazn yozildi: {kg:.1f} kg", False

    if atype == "daily_sleep_action":
        import feat_sleep as sl
        act = _norm_text(p.get("action"))
        if act == "bed":
            sl.on_bedtime_confirmed(app.now())
            return "😴 Uyqu boshlandi (yotish vaqti qayd etildi)", False
        if act == "wake":
            res = sl.finish_sleep(app.now(), source="ai")
            return f"☀️ Uyg'onish qayd etildi ({sl.fmt_min(res.get('duration_min'))})", False
        raise ValueError("Uyqu amali 'bed' yoki 'wake' bo'lishi kerak.")

    # ---- 8. DINAMIK TIZIM VAQTLARI ----
    if atype == "schedule_update":
        key = _clean_str(p.get("schedule_key"))
        if p.get("only_today") and p.get("time_hhmm"):
            schedules.set_today_override(key, _clean_str(p.get("time_hhmm")))
            return f"⏰ {key} faqat bugun uchun {p.get('time_hhmm')} ga surildi", True
        ok = schedules.update_system_schedule(
            key,
            mode=p.get("mode"),
            time_hhmm=p.get("time_hhmm"),
            prayer_ref=p.get("prayer_ref"),
            offset_minutes=p.get("offset_minutes"),
            is_enabled=p.get("is_enabled"),
        )
        if not ok:
            raise ValueError(f"Tizim jadvali topilmadi: {key}")
        s = schedules.get_system_schedule(key)
        return f"⏰ Tizim vaqti yangilandi: {s['title']} → {schedules.describe_schedule(s)}", True

    # ---- 9. QO'SHIMCHA IZOHLAR (STICKY NOTES) ----
    if atype == "schedule_add_note":
        key = _clean_str(p.get("schedule_key"))
        note_txt = _clean_str(p.get("note_text"))
        ntype = _clean_str(p.get("note_type")) or "once"
        schedules.add_schedule_note(key, note_txt, ntype)
        lbl = "doimiy" if ntype == "recurring" else "bir martalik"
        return f"📌 {key} ga {lbl} izoh biriktirildi: «{note_txt}»", False

    if atype == "schedule_remove_note":
        q_n = _clean_str(p.get("note_id_or_query"))
        if q_n.isdigit():
            schedules.delete_schedule_note(int(q_n))
            return f"🗑 Izoh #{q_n} o'chirildi", False
        skey = _clean_str(p.get("schedule_key"))
        if skey:
            row = db.q1("SELECT id, note_text FROM schedule_notes WHERE schedule_key = ? AND is_done = 0 AND LOWER(note_text) LIKE LOWER(?) LIMIT 1", (skey, f"%{q_n}%"))
        else:
            row = db.q1("SELECT id, note_text FROM schedule_notes WHERE is_done = 0 AND LOWER(note_text) LIKE LOWER(?) LIMIT 1", (f"%{q_n}%",))
        if not row:
            raise ValueError(f"Izoh topilmadi: {q_n}")
        schedules.delete_schedule_note(row["id"])
        return f"🗑 Izoh o'chirildi: {row['note_text']}", False

    # ---- 10. DINAMIK ESLATMALAR ----
    if atype == "dynamic_reminder_add":
        title = _clean_str(p.get("title"))
        start_time = _clean_str(p.get("start_time"))
        end_time = _clean_str(p.get("end_time")) or None
        rid = schedules.add_dynamic_reminder(
            title=title,
            kind=_clean_str(p.get("kind")) or "deadline",
            start_time=start_time,
            end_time=end_time,
            days_of_week=_clean_str(p.get("days_of_week")) or "all",
            remind_before_min=int(p.get("remind_before_min") or 0),
        )
        span = f"{start_time}–{end_time}" if end_time else start_time
        return f"📅 Yangi dinamik eslatma (#{rid}): {title} ({span})", True

    if atype == "dynamic_reminder_update":
        q_r = _clean_str(p.get("reminder_id_or_title"))
        row = db.q1("SELECT * FROM dynamic_reminders WHERE id = ?", (int(q_r),)) if q_r.isdigit() else db.q1(
            "SELECT * FROM dynamic_reminders WHERE LOWER(title) LIKE LOWER(?) LIMIT 1", (f"%{q_r}%",)
        )
        if not row:
            raise ValueError(f"Dinamik eslatma topilmadi: {q_r}")
        schedules.update_dynamic_reminder(
            row["id"],
            title=p.get("title"),
            start_time=p.get("start_time"),
            end_time=p.get("end_time"),
            days_of_week=p.get("days_of_week"),
            is_enabled=p.get("is_enabled"),
        )
        return f"✏️ Dinamik eslatma yangilandi: {row['title']}", True

    if atype == "dynamic_reminder_delete":
        q_r = _clean_str(p.get("reminder_id_or_title"))
        row = db.q1("SELECT * FROM dynamic_reminders WHERE id = ?", (int(q_r),)) if q_r.isdigit() else db.q1(
            "SELECT * FROM dynamic_reminders WHERE LOWER(title) LIKE LOWER(?) LIMIT 1", (f"%{q_r}%",)
        )
        if not row:
            raise ValueError(f"Dinamik eslatma topilmadi: {q_r}")
        schedules.delete_dynamic_reminder(row["id"])
        return f"🗑 Dinamik eslatma o'chirildi: {row['title']}", True

    raise ValueError(f"Noma'lum amal: {atype}")


async def execute_pending_action(action_id: str) -> dict:
    """
    Foydalanuvchi `[✅ Tasdiqlayman]` bosgach `ai_pending_actions` dagi amallarni bajaradi.
    """
    row = get_pending_action(action_id)
    if not row:
        return {"ok": False, "error": "Amal topilmadi."}
    if row["status"] != "pending":
        return {"ok": False, "error": f"Bu amal allaqachon yakunlangan ({row['status']})."}

    results: list[str] = []
    errors: list[str] = []
    needs_reschedule = False

    for act in row["actions"]:
        atype = act.get("type", "")
        params = act.get("params") or {}
        try:
            line, resched = _exec_single_action(atype, params)
            results.append(line)
            if resched:
                needs_reschedule = True
        except Exception as exc:
            errors.append(f"❌ {atype}: {exc}")

    if needs_reschedule:
        try:
            import jobs
            await jobs.schedule_today()
        except Exception as exc:
            app.log.warning(f"AI action reschedule xatoligi: {exc}")

    db.ex("UPDATE ai_pending_actions SET status = 'confirmed' WHERE id = ?", (action_id,))
    return {
        "ok": len(errors) == 0,
        "results": results,
        "errors": errors,
        "summary": "\n".join(results + errors),
    }


def get_gemini_tool_declarations() -> list[dict]:
    """Gemini Function Calling uchun to'liq asboblar sxemasi (JSON Schema / FunctionDeclaration)."""
    return [
        {
            "name": "get_finance_summary",
            "description": "Kartalar/naqd balans, qarzlar, yaqin kredit to'lovlari va oxirgi tranzaksiyalarni o'qish (read-only).",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_market_info",
            "description": "Rejalashtirilgan bozorlik ro'yxati va mahsulot narxlari tarixini o'qish (read-only).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "item_name": {"type": "STRING", "description": "Ixtiyoriy mahsulot nomi (narx solishtirish uchun)."},
                },
            },
        },
        {
            "name": "get_school_schedule",
            "description": "Maktab darslari jadvali, joriy TMR mavzusi va sinf izohlarini o'qish (read-only).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "day": {"type": "STRING", "description": "'today', 'tomorrow' yoki 'YYYY-MM-DD'."},
                },
            },
        },
        {
            "name": "get_uni_schedule",
            "description": "Universitet paralari, uy vazifalari va BMI/kurs ishi bosqichlarini o'qish (read-only).",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "day": {"type": "STRING", "description": "'today', 'tomorrow' yoki 'YYYY-MM-DD'."},
                },
            },
        },
        {
            "name": "get_health_status",
            "description": "Aptechka dorilari, tugayotgan dorilar va faol davolanish kurslarini o'qish (read-only).",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_challenges_status",
            "description": "Faol chelenjlar va bugungi vazifalar progressini o'qish (read-only).",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_daily_status",
            "description": "Namoz vaqtlari, qazolar, uyqu, vazn, bugungi qaydlar va ob-havo tavsiyasini o'qish (read-only).",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_schedules_and_reminders",
            "description": "Barcha tizim vaqtlari, yopishqoq izohlar, dinamik eslatmalar va ayni damdagi bandlik (busy_block) holatini o'qish (read-only).",
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "propose_actions",
            "description": (
                "Bazaga yozish, tahrirlash, o'chirish yoki jadval vaqtini o'zgartirish uchun FOYDALANUVCHI TASDIG'INI SO'RASH. "
                "Hech qachon to'g'ridan-to'g'ri yozmaydi, faqat tasdiqlash kartochkasi yaratadi. Multi-Action qo'llab-quvvatlaydi. "
                "Muhim params: finance_expense/finance_income={amount, account_query, category, note}; "
                "finance_transfer={amount, from_account_query, to_account_query, note}; "
                "schedule_update={schedule_key, time_hhmm, mode, prayer_ref, offset_minutes, only_today, is_enabled}; "
                "dynamic_reminder_add={title, start_time ('HH:MM'), end_time, kind ('deadline'|'busy_block'), days_of_week ('all'|'0,2,4'|'once:YYYY-MM-DD')}."
            ),
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "summary_text": {"type": "STRING", "description": "Foydalanuvchiga ko'rsatiladigan aniq o'zbekcha xulosa."},
                    "actions": {
                        "type": "ARRAY",
                        "description": "Bajariladigan amallar ro'yxati. Har biri {type, params} ko'rinishida.",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "type": {"type": "STRING"},
                                "params": {"type": "OBJECT"},
                            },
                            "required": ["type", "params"],
                        },
                    },
                },
                "required": ["summary_text", "actions"],
            },
        },
    ]
