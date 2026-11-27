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

def propose_actions(summary_text: str, actions: list[dict]) -> dict:
    """
    Bazaga yozadigan/o'zgartiradigan amallarni TO'G'RIDAN-TO'G'RI bajarmasdan,
    `ai_pending_actions` jadvaliga foydalanuvchi tasdig'i uchun navbatga qo'yadi.
    Multi-Action qo'llab-quvvatlanadi (bir nechta modul amali bitta tasdiqlash kartochkasida).
    """
    if not isinstance(actions, list) or not actions:
        return {"error": "Amallar ro'yxati bo'sh bo'lishi mumkin emas."}

    cleaned_actions = []
    for act in actions:
        if not isinstance(act, dict):
            continue
        atype = str(act.get("type") or "").strip()
        if atype not in SUPPORTED_WRITE_ACTIONS:
            return {"error": f"Qo'llab-quvvatlanmaydigan amal turi: {atype}"}
        params = act.get("params") if isinstance(act.get("params"), dict) else {
            k: v for k, v in act.items() if k != "type"
        }
        cleaned_actions.append({"type": atype, "params": params})

    if not cleaned_actions:
        return {"error": "Yaroqli amal topilmadi."}

    action_id = uuid.uuid4().hex[:10]
    summary = (summary_text or "").strip() or f"{len(cleaned_actions)} ta amal bajariladi."
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

def _find_account(query: str | None) -> dict | None:
    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    if not accs:
        return None
    if not query:
        return accs[0]
    q_low = str(query).strip().lower()
    for a in accs:
        if a.get("last_4") and a["last_4"] in q_low:
            return a
    for a in accs:
        if q_low in (a.get("name") or "").lower() or q_low == str(a["id"]):
            return a
    return None


def _normalize_amount(val: Any) -> int:
    num = float(str(val).replace(" ", "").replace(",", "."))
    return int(round(num))


def _exec_single_action(atype: str, p: dict) -> tuple[str, bool]:
    """Bitta tasdiqlangan amalni bajaradi. Returns: (human_result_line, needs_reschedule)"""

    # ---- 1. MOLIYA ----
    if atype in ("finance_expense", "finance_income", "finance_transfer"):
        import feat_finance as fin
        amount = _normalize_amount(p.get("amount", 0))
        if amount <= 0:
            raise ValueError("Summa musbat bo'lishi kerak.")
        note = str(p.get("note") or "")
        cat = str(p.get("category") or ("Boshqa kirim" if atype == "finance_income" else "Boshqa"))

        if atype == "finance_expense":
            acc = _find_account(p.get("account_query"))
            if not acc:
                raise ValueError("Faol moliya hisobi topilmadi.")
            fin.record_transaction(amount, cat, note, from_account_id=acc["id"])
            return f"💸 Chiqim yozildi: {fin.fmt_sum(amount)} so'm ({acc['name']}, {cat})", False

        if atype == "finance_income":
            acc = _find_account(p.get("account_query"))
            if not acc:
                raise ValueError("Faol moliya hisobi topilmadi.")
            fin.record_transaction(amount, cat, note, to_account_id=acc["id"])
            return f"💰 Kirim yozildi: +{fin.fmt_sum(amount)} so'm ({acc['name']}, {cat})", False

        if atype == "finance_transfer":
            a_from = _find_account(p.get("from_account_query"))
            a_to = _find_account(p.get("to_account_query"))
            if not a_from or not a_to:
                raise ValueError("O'tkazma hisoblari topilmadi.")
            fin.record_transaction(amount, "🔄 O'tkazma", note, from_account_id=a_from["id"], to_account_id=a_to["id"])
            return f"🔄 O'tkazma: {fin.fmt_sum(amount)} so'm ({a_from['name']} → {a_to['name']})", False

    if atype == "finance_add_debt":
        import feat_finance as fin
        person = str(p["person_name"]).strip()
        amt = _normalize_amount(p["amount"])
        fin.create_debt(person, amt, due_date=p.get("due_date"), note=str(p.get("note") or ""))
        kind_s = "Haq" if amt > 0 else "Qarz"
        return f"🤝 {kind_s} qo'shildi: {person} ({fin.fmt_sum(abs(amt))} so'm)", False

    if atype == "finance_settle_debt":
        import feat_finance as fin
        q_p = str(p.get("person_name_or_id") or "").strip()
        debt = None
        if q_p.isdigit():
            debt = db.q1("SELECT * FROM finance_debts WHERE id = ? AND status = 'active'", (int(q_p),))
        if not debt:
            debt = db.q1("SELECT * FROM finance_debts WHERE LOWER(person_name) LIKE LOWER(?) AND status = 'active' LIMIT 1", (f"%{q_p}%",))
        if not debt:
            raise ValueError(f"Faol qarz topilmadi: {q_p}")
        amt = _normalize_amount(p.get("amount", abs(debt["amount"])))
        acc = _find_account(p.get("account_query")) if p.get("account_query") else None
        fin.settle_debt(debt["id"], amt, account_id=acc["id"] if acc else None)
        return f"✅ Qarz yopildi: {debt['person_name']} ({fin.fmt_sum(amt)} so'm)", False

    # ---- 2. BOZORLIK ----
    if atype == "market_add_planned":
        import feat_market as mkt
        item_name = str(p["item_name"]).strip()
        qty = float(p.get("quantity", 1))
        unit = str(p.get("unit") or "dona").strip()
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
        item_name = str(p["item_name"]).strip()
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
        q_c = str(p.get("class_name_or_course_id") or "").strip()
        course = None
        if q_c.isdigit():
            course = edu.get_course(int(q_c))
        if not course:
            course = db.q1("SELECT * FROM edu_courses WHERE is_active = 1 AND LOWER(class_name) LIKE LOWER(?) LIMIT 1", (f"%{q_c}%",))
        if not course:
            raise ValueError(f"Sinf topilmadi: {q_c}")
        edu.add_class_note(course["id"], str(p["note_text"]))
        return f"🏫 {course['class_name']} sinfiga izoh qo'shildi: {p['note_text']}", False

    # ---- 4. UNIVERSITET ----
    if atype == "uni_add_homework":
        import feat_uni as uni
        q_s = str(p.get("subject_name_or_id") or "").strip()
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
        uni.add_homework(subj["id"], ltype, str(p["task_text"]), due_date=due)
        return f"🏛 Uy ishi qo'shildi: {subj['name']} — {p['task_text']}", False

    if atype == "uni_complete_homework":
        import feat_uni as uni
        q_h = str(p.get("homework_id_or_query") or "").strip()
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
        q_w = str(p.get("work_title_or_id") or "").strip()
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
        q_i = str(p.get("item_name_or_id") or "").strip()
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
        name = str(p["name"]).strip()
        qty = float(p.get("quantity", 1))
        unit = str(p.get("unit") or "dona").strip()
        loc = str(p.get("location") or "home").strip()
        form = str(p.get("form") or "tabletka").strip()
        notes = str(p.get("notes") or "").strip()
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
        q_t = str(p.get("task_title_or_id") or "").strip()
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
        val = float(p.get("value", task["target_value"] or 1))
        db.ex(
            "INSERT INTO challenge_logs (challenge_id, task_id, log_date, value, status) VALUES (?, ?, ?, ?, 'done') "
            "ON CONFLICT(task_id, log_date) DO UPDATE SET value = excluded.value, status = 'done'",
            (task["challenge_id"], task["id"], d.isoformat(), val),
        )
        if hasattr(ch, "evaluate_day"):
            ch.evaluate_day(task["challenge_id"], d)
        return f"🏆 Chelenj vazifasi bajarildi: {task['title']}", False

    if atype == "challenge_use_freeze":
        q_c = str(p.get("challenge_title_or_id") or "").strip()
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
        txt = str(p["text"]).strip()
        db.ins("INSERT INTO notes (date, text, created_at) VALUES (?, ?, ?)", (app.today().isoformat(), txt, app.now().isoformat()))
        return f"📝 Tezkor qayd qo'shildi: {txt}", False

    if atype == "daily_delete_note":
        q_n = str(p.get("note_id_or_query") or "").strip()
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
        kg = float(p["kg"])
        wt.save(kg, primary=False)
        return f"⚖️ Vazn yozildi: {kg:.1f} kg", False

    if atype == "daily_sleep_action":
        import feat_sleep as sl
        act = str(p.get("action") or "").lower()
        if act == "bed":
            sl.on_bedtime_confirmed(app.now())
            return "😴 Uyqu boshlandi (yotish vaqti qayd etildi)", False
        if act == "wake":
            res = sl.finish_sleep(app.now(), source="ai")
            return f"☀️ Uyg'onish qayd etildi ({sl.fmt_min(res.get('duration_min'))})", False
        raise ValueError("Uyqu amali 'bed' yoki 'wake' bo'lishi kerak.")

    # ---- 8. DINAMIK TIZIM VAQTLARI ----
    if atype == "schedule_update":
        key = str(p["schedule_key"]).strip()
        if p.get("only_today") and p.get("time_hhmm"):
            schedules.set_today_override(key, str(p["time_hhmm"]).strip())
            return f"⏰ {key} faqat bugun uchun {p['time_hhmm']} ga surildi", True
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
        key = str(p["schedule_key"]).strip()
        note_txt = str(p["note_text"]).strip()
        ntype = str(p.get("note_type") or "once").strip()
        schedules.add_schedule_note(key, note_txt, ntype)
        lbl = "doimiy" if ntype == "recurring" else "bir martalik"
        return f"📌 {key} ga {lbl} izoh biriktirildi: «{note_txt}»", False

    if atype == "schedule_remove_note":
        q_n = str(p.get("note_id_or_query") or "").strip()
        if q_n.isdigit():
            schedules.delete_schedule_note(int(q_n))
            return f"🗑 Izoh #{q_n} o'chirildi", False
        skey = p.get("schedule_key")
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
        rid = schedules.add_dynamic_reminder(
            title=str(p["title"]),
            kind=str(p.get("kind") or "deadline"),
            start_time=str(p["start_time"]),
            end_time=p.get("end_time"),
            days_of_week=str(p.get("days_of_week") or "all"),
            remind_before_min=int(p.get("remind_before_min") or 0),
        )
        span = f"{p['start_time']}–{p['end_time']}" if p.get("end_time") else p["start_time"]
        return f"📅 Yangi dinamik eslatma (#{rid}): {p['title']} ({span})", True

    if atype == "dynamic_reminder_update":
        q_r = str(p.get("reminder_id_or_title") or "").strip()
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
        q_r = str(p.get("reminder_id_or_title") or "").strip()
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
                "Hech qachon to'g'ridan-to'g'ri yozmaydi, faqat tasdiqlash kartochkasi yaratadi. Multi-Action qo'llab-quvvatlaydi."
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
