"""
Universal Moliya Tizimi (3.5.1 — feat_finance.py):
- 6 ta jadval: `finance_accounts`, `finance_categories`, `finance_debts`, `finance_loans`,
  `finance_recurring`, `finance_transactions`.
- Standart va tizimli kategoriyalar (`Oziq-ovqat/Bozorlik`, `Kredit to'lovi`, `Ijara/Kvartira`,
  `Qarzni qaytarish`, `Qarz qaytishi` va b.) + dinamik CRUD (`[➕ Yangi kategoriya]`, `[⚙️ Tahrirlash]`).
- Tizimli kategoriyalar integratsiyasi: Chiqim/Kirim ichidan turib aktiv kreditlar, oylik kvartira rejasi
  hamda qarz/haqlar balansini avtomatik kamaytirish.
- Tranzaksiyalar tarixi (`📜 Tarix`) va In-place sahifalash (`⬅️ Oldingi` / `Keyingi ➡️`).
- Oxirgi amaliyotni 1 daqiqa ichida bekor qilish (`↩️ Bekor qilish` — Undo: tranzaksiyani o'chirib, hisob
  hamda bog'langan kredit/ijara/qarz balansini 100% tiklaydi).
- Har 90 kunda Avto-Arxiv (90 kundan eski tranzaksiya va chelenj loglarini JSON/CSV fayl qilib Telegramga
  yuborish, o'chirish va `VACUUM;` bilan bazani fizik qisqartirish; karta/naqd balanslari daxlsiz qoladi).
"""

import csv
import io
import json
import re
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import app
import db
import messenger
import services as sv

router = Router()

ACC_TYPE_LABELS = {
    "cash": "💵 Naqd",
    "debit": "💳 Debet karta",
    "credit": "🏦 Kredit karta",
}
AMBIGUOUS_MIN = 400
AMBIGUOUS_MAX = 999
UNDO_WINDOW_SEC = 60
HISTORY_PAGE_SIZE = 8
ARCHIVE_DAYS = 90

_AMT_RE = re.compile(r"^([+-]?\d+(?:[.,]\d+)?)([kK]|ming|[mM]|mln)?$", re.IGNORECASE)

# (name, icon, system_key, is_system)
SEED_EXPENSE_CATEGORIES = [
    ("Oziq-ovqat/Bozorlik", "🛒", None, 0),
    ("Kafe/Tushlik", "🍽", None, 0),
    ("Yo'l/Transport", "🚕", None, 0),
    ("Ilm/Ta'lim", "📚", None, 0),
    ("Sport/Sog'liq", "🏋️", None, 0),
    ("Kiyim", "👕", None, 0),
    ("Ehson", "🤲", None, 0),
    ("Kredit to'lovi", "🏦", "sys_loan", 1),
    ("Ijara/Kvartira", "🏠", "sys_rent", 1),
    ("Qarzni qaytarish", "🤝", "sys_debt_out", 1),
    ("Boshqa", "📦", None, 0),
]

SEED_INCOME_CATEGORIES = [
    ("Stipendiya", "🎓", None, 0),
    ("Ish haqi (Maktab)", "🏫", None, 0),
    ("Qo'shimcha ish", "💼", None, 0),
    ("Oila", "👨‍👩‍👦", None, 0),
    ("Qarz qaytishi", "🤝", "sys_debt_in", 1),
    ("Boshqa", "📥", None, 0),
]


class FinFSM(StatesGroup):
    confirm_amount = State()

    acc_name = State()
    acc_type = State()
    acc_last4 = State()
    acc_balance = State()
    acc_limit = State()
    acc_edit_bal = State()

    tx_amount = State()
    tx_note = State()
    tx_sys_amount = State()

    cat_add = State()
    cat_rename = State()

    debt_person = State()
    debt_dir = State()
    debt_amount = State()
    debt_due = State()
    debt_pay_amt = State()

    loan_title = State()
    loan_total = State()
    loan_rem = State()
    loan_monthly = State()
    loan_due_day = State()
    loan_grace = State()
    loan_inst = State()
    loan_pay_amt = State()

    rec_title = State()
    rec_target = State()
    rec_window = State()
    rec_pay_amt = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_sum(n: int | float) -> str:
    val = int(round(n))
    sign = "−" if val < 0 else ""
    return f"{sign}{abs(val):,}".replace(",", " ")


# ------------------------------------------------------------------ Kategoriyalar (Seed va CRUD)

def seed_finance_categories():
    """Standart va tizimli kategoriyalarni bazaga bir marta (idempotent) yuklaydi."""
    existing_cnt = db.q1("SELECT COUNT(*) AS c FROM finance_categories")["c"]
    if db.get_setting("fin_cats_seeded") == "1" and existing_cnt > 0:
        return
    for name, icon, sys_key, is_sys in SEED_EXPENSE_CATEGORIES:
        if sys_key:
            exists = db.q1("SELECT id FROM finance_categories WHERE system_key = ?", (sys_key,))
        else:
            exists = db.q1("SELECT id FROM finance_categories WHERE type = 'expense' AND name = ?", (name,))
        if not exists:
            db.ins(
                "INSERT INTO finance_categories (name, type, icon, system_key, is_system, is_default, is_active, usage_count) "
                "VALUES (?,?,?,?,?,1,1,0)",
                (name, "expense", icon, sys_key, is_sys),
            )
    for name, icon, sys_key, is_sys in SEED_INCOME_CATEGORIES:
        if sys_key:
            exists = db.q1("SELECT id FROM finance_categories WHERE system_key = ?", (sys_key,))
        else:
            exists = db.q1("SELECT id FROM finance_categories WHERE type = 'income' AND name = ?", (name,))
        if not exists:
            db.ins(
                "INSERT INTO finance_categories (name, type, icon, system_key, is_system, is_default, is_active, usage_count) "
                "VALUES (?,?,?,?,?,1,1,0)",
                (name, "income", icon, sys_key, is_sys),
            )
    db.set_setting("fin_cats_seeded", "1")


def list_categories(cat_type: str) -> list[dict]:
    seed_finance_categories()
    return db.q(
        "SELECT * FROM finance_categories WHERE type = ? AND is_active = 1 "
        "ORDER BY usage_count DESC, is_system DESC, id ASC",
        (cat_type,),
    )


def parse_icon_and_name(raw: str, default_icon: str = "📌") -> tuple[str, str]:
    s = (raw or "").strip()
    if not s:
        return default_icon, ""
    parts = s.split(maxsplit=1)
    if len(parts) == 2 and len(parts[0]) <= 4 and not any(ch.isalnum() for ch in parts[0]):
        return parts[0], parts[1].strip()
    return default_icon, s


def create_category(name: str, cat_type: str, icon: str = "📌") -> int:
    if cat_type not in ("expense", "income"):
        raise ValueError("Kategoriya turi 'expense' yoki 'income' bo'lishi kerak")
    return db.ins(
        "INSERT INTO finance_categories (name, type, icon, system_key, is_system, is_default, is_active, usage_count) "
        "VALUES (?,?,?,NULL,0,0,1,0)",
        (name.strip(), cat_type, (icon or "📌").strip()),
    )


def update_category(cat_id: int, name: str, icon: str | None = None):
    cat = db.q1("SELECT * FROM finance_categories WHERE id = ?", (cat_id,))
    if not cat:
        return
    new_icon = (icon or cat["icon"] or "📌").strip()
    db.ex("UPDATE finance_categories SET name = ?, icon = ? WHERE id = ?", (name.strip(), new_icon, cat_id))


def delete_category(cat_id: int) -> bool:
    """Faqat tizimli bo'lmagan (`is_system = 0`) kategoriyalarni o'chiradi (arxivlaydi)."""
    cat = db.q1("SELECT * FROM finance_categories WHERE id = ?", (cat_id,))
    if not cat or int(cat["is_system"]) == 1:
        return False
    db.ex("UPDATE finance_categories SET is_active = 0 WHERE id = ?", (cat_id,))
    return True


# ------------------------------------------------------------------ Minglik qoidasi va sanalar

def parse_fin_amount(raw: str, allow_zero: bool = False) -> dict | None:
    """
    Summani Minglik qoidasi bilan tahlil qiladi:
    - `< 1000` (va `> 0`) bo'lsa avtomatik `1000` ga ko'paytiriladi (`25 -> 25 000`).
    - `400..999` oralig'idagi butun sonlarda (`500` kabi) `ambiguous=True` qaytaradi.
    """
    clean = (raw or "").strip().replace(" ", "").replace("_", "")
    m = _AMT_RE.match(clean)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()

    if val == 0:
        return {"amount": 0, "ambiguous": False, "raw_num": 0} if allow_zero else None
    if val < 0:
        return None

    if suffix in ("m", "mln"):
        return {"amount": int(round(val * 1_000_000)), "ambiguous": False, "raw_num": val}
    if suffix in ("k", "ming"):
        return {"amount": int(round(val * 1_000)), "ambiguous": False, "raw_num": val}
    if val < 1000:
        amb = val.is_integer() and (AMBIGUOUS_MIN <= int(val) <= AMBIGUOUS_MAX)
        return {"amount": int(round(val * 1000)), "ambiguous": amb, "raw_num": int(val) if val.is_integer() else val}
    return {"amount": int(round(val)), "ambiguous": False, "raw_num": val}


def _parse_optional_date(raw: str) -> str | None | bool:
    """`-` bo'lsa None, to'g'ri sana bo'lsa ISO string, xato bo'lsa False."""
    s = (raw or "").strip()
    if s in ("-", "0", "yo'q", "yoq"):
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return False


# ------------------------------------------------------------------ Biznes mantiq (CRUD, Smart Lifecycle va Undo)

def credit_card_debt(acc: dict) -> int:
    """Kredit kartadagi joriy ishlatilgan qarz miqdori."""
    if acc.get("type") != "credit" or not acc.get("is_active", 1):
        return 0
    lim = int(acc.get("credit_limit") or 0)
    bal = int(acc.get("balance") or 0)
    if lim > 0:
        return max(0, lim - bal)
    return abs(bal) if bal < 0 else 0


def create_account(
    name: str,
    acc_type: str,
    balance: int = 0,
    credit_limit: int = 0,
    last_4: str | None = None,
) -> int:
    if acc_type not in ACC_TYPE_LABELS:
        raise ValueError(f"Noto'g'ri hisob turi: {acc_type}")
    l4 = (last_4 or "").strip()[-4:] or None
    return db.ins(
        "INSERT INTO finance_accounts (name, last_4, type, balance, credit_limit, is_active, usage_count) "
        "VALUES (?,?,?,?,?,1,0)",
        (name.strip(), l4, acc_type, int(balance), int(credit_limit if acc_type == "credit" else 0)),
    )


def update_account_balance(acc_id: int, new_balance: int):
    db.ex("UPDATE finance_accounts SET balance = ? WHERE id = ?", (int(new_balance), acc_id))


def archive_account(acc_id: int):
    db.ex("UPDATE finance_accounts SET is_active = 0 WHERE id = ?", (acc_id,))


def record_transaction(
    amount: int,
    category: str,
    note: str = "",
    from_account_id: int | None = None,
    to_account_id: int | None = None,
    category_id: int | None = None,
    linked_kind: str | None = None,
    linked_id: int | None = None,
    undo_meta: dict | None = None,
) -> int:
    amt = abs(int(amount))
    if from_account_id:
        db.ex(
            "UPDATE finance_accounts SET balance = balance - ?, usage_count = usage_count + 1 WHERE id = ?",
            (amt, from_account_id),
        )
    if to_account_id:
        db.ex(
            "UPDATE finance_accounts SET balance = balance + ?, usage_count = usage_count + 1 WHERE id = ?",
            (amt, to_account_id),
        )
    if category_id:
        db.ex("UPDATE finance_categories SET usage_count = usage_count + 1 WHERE id = ?", (category_id,))
    meta_json = json.dumps(undo_meta, ensure_ascii=False) if undo_meta else None
    return db.ins(
        "INSERT INTO finance_transactions "
        "(from_account_id, to_account_id, amount, category, note, created_at, category_id, linked_kind, linked_id, undo_meta) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            from_account_id, to_account_id, amt, category, note.strip(),
            app.now().isoformat(), category_id, linked_kind, linked_id, meta_json,
        ),
    )


def undo_transaction(tx_id: int) -> dict:
    """
    Kiritilgan tranzaksiyani 1 daqiqa (60 soniya) ichida bekor qiladi:
    - Tranzaksiyani `finance_transactions` dan o'chiradi;
    - Karta/naqd balansini avvalgi holatiga qaytaradi;
    - Tizimli kategoriya (kredit, kvartira, qarz/haq) orqali bajarilgan bo'lsa, o'sha ob'ekt qoldig'ini ham tiklaydi.
    """
    tx = db.q1("SELECT * FROM finance_transactions WHERE id = ?", (tx_id,))
    if not tx:
        return {"ok": False, "error": "Amaliyot topilmadi yoki allaqachon bekor qilingan."}
    created = datetime.fromisoformat(tx["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=app.TZ)
    age_sec = (app.now() - created).total_seconds()
    if age_sec > UNDO_WINDOW_SEC:
        return {"ok": False, "error": "Bekor qilish muddati (1 daqiqa) tugagan.", "expired": True}

    amt = int(tx["amount"])
    if tx.get("from_account_id"):
        db.ex(
            "UPDATE finance_accounts SET balance = balance + ?, usage_count = MAX(0, usage_count - 1) WHERE id = ?",
            (amt, tx["from_account_id"]),
        )
    if tx.get("to_account_id"):
        db.ex(
            "UPDATE finance_accounts SET balance = balance - ?, usage_count = MAX(0, usage_count - 1) WHERE id = ?",
            (amt, tx["to_account_id"]),
        )
    if tx.get("category_id"):
        db.ex(
            "UPDATE finance_categories SET usage_count = MAX(0, usage_count - 1) WHERE id = ?",
            (tx["category_id"],),
        )

    l_kind = tx.get("linked_kind")
    l_id = tx.get("linked_id")
    if l_kind and l_id and tx.get("undo_meta"):
        meta = json.loads(tx["undo_meta"])
        if l_kind == "loan":
            db.ex(
                "UPDATE finance_loans SET remaining_debt = ?, remaining_installments = ?, "
                "last_paid_month = ?, status = ? WHERE id = ?",
                (meta["remaining_debt"], meta["remaining_installments"], meta.get("last_paid_month"), meta["status"], l_id),
            )
        elif l_kind == "recurring":
            db.ex(
                "UPDATE finance_recurring SET paid_amount_this_month = ?, last_paid_month = ?, current_month = ? WHERE id = ?",
                (meta["paid_amount_this_month"], meta.get("last_paid_month"), meta.get("current_month"), l_id),
            )
        elif l_kind == "debt":
            db.ex(
                "UPDATE finance_debts SET amount = ?, status = ? WHERE id = ?",
                (meta["amount"], meta["status"], l_id),
            )

    db.ex("DELETE FROM finance_transactions WHERE id = ?", (tx_id,))
    job = app.scheduler.get_job(f"undo_exp_{tx_id}")
    if job:
        job.remove()
    return {"ok": True, "amount": amt, "category": tx["category"]}


def create_debt(person_name: str, amount: int, due_date: str | None = None, note: str = "") -> int:
    """`amount > 0` — haq (menga berishlari kerak), `amount < 0` — qarz (men berishim kerak)."""
    if int(amount) == 0:
        raise ValueError("Qarz summasi 0 bo'lishi mumkin emas")
    return db.ins(
        "INSERT INTO finance_debts (person_name, amount, due_date, note, status) VALUES (?,?,?,?,'active')",
        (person_name.strip(), int(amount), due_date, note.strip()),
    )


def settle_debt(
    debt_id: int,
    pay_amount: int | None = None,
    account_id: int | None = None,
    category_id: int | None = None,
    category_label: str | None = None,
) -> dict:
    """
    Qarz yoki haqni qisman/to'liq yopadi.
    Qoldiq 0 ga yetganda `status = 'settled'` ga o'tadi va qayta eslatilmaydi.
    """
    d_row = db.q1("SELECT * FROM finance_debts WHERE id = ?", (debt_id,))
    if not d_row or d_row["status"] == "settled":
        return {"ok": False}
    cur = int(d_row["amount"])
    pay = abs(int(pay_amount)) if pay_amount is not None else abs(cur)
    pay = min(pay, abs(cur))

    undo_meta = {"amount": cur, "status": d_row["status"]}
    tx_id = None
    if cur > 0:
        new_amt = cur - pay
        if account_id:
            tx_id = record_transaction(
                pay,
                category_label or "🤝 Qarz qaytishi",
                f"Haq qaytdi: {d_row['person_name']}",
                to_account_id=account_id,
                category_id=category_id,
                linked_kind="debt",
                linked_id=debt_id,
                undo_meta=undo_meta,
            )
    else:
        new_amt = cur + pay
        if account_id:
            tx_id = record_transaction(
                pay,
                category_label or "🤝 Qarzni qaytarish",
                f"Qarz yopildi: {d_row['person_name']}",
                from_account_id=account_id,
                category_id=category_id,
                linked_kind="debt",
                linked_id=debt_id,
                undo_meta=undo_meta,
            )

    new_status = "settled" if new_amt == 0 else "active"
    db.ex("UPDATE finance_debts SET amount = ?, status = ? WHERE id = ?", (new_amt, new_status, debt_id))
    return {"ok": True, "remaining": new_amt, "status": new_status, "paid": pay, "tx_id": tx_id, "person": d_row["person_name"]}


def create_loan(
    title: str,
    total_debt: int,
    remaining_debt: int,
    monthly_payment: int,
    due_day: int,
    total_installments: int,
    remaining_installments: int | None = None,
    grace_deadline_day: int | None = None,
    grace_deadline_time: str | None = None,
    end_date: str | None = None,
) -> int:
    rem_inst = int(remaining_installments) if remaining_installments is not None else int(total_installments)
    due_d = min(31, max(1, int(due_day)))
    if end_date is None:
        ref = app.today()
        first_due = sv.next_monthly_due_date(ref, due_d, skip_this_month=False)
        end_date = sv.add_months(first_due, max(0, rem_inst - 1)).isoformat()
    status = "paid" if (int(remaining_debt) <= 0 or rem_inst <= 0) else "active"
    return db.ins(
        "INSERT INTO finance_loans (title, total_debt, remaining_debt, monthly_payment, due_day, "
        "grace_deadline_day, grace_deadline_time, total_installments, remaining_installments, end_date, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            title.strip(), int(total_debt), max(0, int(remaining_debt)), int(monthly_payment),
            due_d, grace_deadline_day, grace_deadline_time, max(1, int(total_installments)),
            max(0, rem_inst), end_date, status,
        ),
    )


def pay_loan(
    loan_id: int,
    pay_amount: int | None = None,
    from_account_id: int | None = None,
    ref_today: date | None = None,
    category_id: int | None = None,
    category_label: str | None = None,
) -> dict:
    """
    Kredit to'lovi (Smart Lifecycle):
    - `remaining_debt` kamayadi va `remaining_installments` bittaga kamayadi.
    - `remaining_debt <= 0` yoki `remaining_installments <= 0` bo'lgach `status = 'paid'` bo'ladi
      va u bo'yicha hech qachon qayta eslatma yuborilmaydi.
    """
    loan = db.q1("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    if not loan or loan["status"] == "paid":
        return {"ok": False}
    d = ref_today or app.today()
    pay = abs(int(pay_amount)) if pay_amount is not None else min(int(loan["monthly_payment"]), int(loan["remaining_debt"]))

    undo_meta = {
        "remaining_debt": int(loan["remaining_debt"]),
        "remaining_installments": int(loan["remaining_installments"]),
        "last_paid_month": loan.get("last_paid_month"),
        "status": loan["status"],
    }

    new_rem_debt = max(0, int(loan["remaining_debt"]) - pay)
    new_rem_inst = max(0, int(loan["remaining_installments"]) - 1)

    if new_rem_debt <= 0 or new_rem_inst <= 0:
        new_status = "paid"
        new_rem_debt = 0
        new_rem_inst = 0
    else:
        new_status = "active"

    db.ex(
        "UPDATE finance_loans SET remaining_debt = ?, remaining_installments = ?, "
        "last_paid_month = ?, status = ? WHERE id = ?",
        (new_rem_debt, new_rem_inst, d.strftime("%Y-%m"), new_status, loan_id),
    )
    tx_id = None
    if from_account_id and pay > 0:
        tx_id = record_transaction(
            pay,
            category_label or "🏦 Kredit to'lovi",
            f"Kredit: {loan['title']}",
            from_account_id=from_account_id,
            category_id=category_id,
            linked_kind="loan",
            linked_id=loan_id,
            undo_meta=undo_meta,
        )

    for job in list(app.scheduler.get_jobs()):
        if job.id.startswith(f"fin_loan_{loan_id}_"):
            job.remove()

    return {
        "ok": True,
        "paid": pay,
        "remaining_debt": new_rem_debt,
        "remaining_installments": new_rem_inst,
        "status": new_status,
        "tx_id": tx_id,
        "title": loan["title"],
    }


def sync_recurring_months(ref_today: date | None = None):
    """Yangi oy boshlanganda davriy to'lovlarning `paid_amount_this_month` hisoblagichini 0 ga qaytaradi."""
    ym = (ref_today or app.today()).strftime("%Y-%m")
    db.ex(
        "UPDATE finance_recurring SET paid_amount_this_month = 0, current_month = ? "
        "WHERE current_month IS NULL OR current_month != ?",
        (ym, ym),
    )


def create_recurring(title: str, target_amount: int, due_start_day: int, due_end_day: int) -> int:
    s_day = min(31, max(1, int(due_start_day)))
    e_day = min(31, max(s_day, int(due_end_day)))
    ym = app.today().strftime("%Y-%m")
    return db.ins(
        "INSERT INTO finance_recurring (title, target_amount, paid_amount_this_month, due_start_day, "
        "due_end_day, last_paid_month, current_month, status) VALUES (?,?,0,?,?,NULL,?,'active')",
        (title.strip(), int(target_amount), s_day, e_day, ym),
    )


def pay_recurring(
    rec_id: int,
    pay_amount: int,
    from_account_id: int | None = None,
    ref_today: date | None = None,
    category_id: int | None = None,
    category_label: str | None = None,
) -> dict:
    """
    Davriy to'lovni qisman yoki to'liq yopish (Partial Payment):
    - Shu oy ichida to'langan qismlar `paid_amount_this_month` da jamlanadi.
    - `target_amount` ga yetganda `last_paid_month = YYYY-MM` yoziladi va shu oy uchun eslatmalar to'xtaydi.
    """
    d = ref_today or app.today()
    ym = d.strftime("%Y-%m")
    sync_recurring_months(d)
    rec = db.q1("SELECT * FROM finance_recurring WHERE id = ?", (rec_id,))
    if not rec:
        return {"ok": False}

    undo_meta = {
        "paid_amount_this_month": int(rec["paid_amount_this_month"]),
        "last_paid_month": rec.get("last_paid_month"),
        "current_month": rec.get("current_month"),
    }

    pay = abs(int(pay_amount))
    new_paid = int(rec["paid_amount_this_month"]) + pay
    target = int(rec["target_amount"])
    closed_this_month = new_paid >= target
    last_m = ym if closed_this_month else rec["last_paid_month"]

    db.ex(
        "UPDATE finance_recurring SET paid_amount_this_month = ?, last_paid_month = ?, current_month = ? WHERE id = ?",
        (new_paid, last_m, ym, rec_id),
    )
    tx_id = None
    if from_account_id and pay > 0:
        tx_id = record_transaction(
            pay,
            category_label or "🏠 Ijara/Kvartira",
            f"Davriy: {rec['title']}",
            from_account_id=from_account_id,
            category_id=category_id,
            linked_kind="recurring",
            linked_id=rec_id,
            undo_meta=undo_meta,
        )

    return {
        "ok": True,
        "paid_this_month": new_paid,
        "target_amount": target,
        "remaining_this_month": max(0, target - new_paid),
        "closed_this_month": closed_this_month,
        "tx_id": tx_id,
        "title": rec["title"],
    }


def toggle_recurring_pause(rec_id: int) -> str:
    rec = db.q1("SELECT status FROM finance_recurring WHERE id = ?", (rec_id,))
    if not rec:
        return ""
    new_st = "paused" if rec["status"] == "active" else "active"
    db.ex("UPDATE finance_recurring SET status = ? WHERE id = ?", (new_st, rec_id))
    return new_st


# ------------------------------------------------------------------ Har 90 kunda Avto-Arxiv va VACUUM

def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


async def run_auto_archive_and_vacuum(cutoff_days: int = ARCHIVE_DAYS) -> dict:
    """
    90 kundan eski `finance_transactions` va `challenge_logs` yozuvlarini JSON hamda CSV fayl ko'rinishida
    Telegramga zaxira qilib yuboradi, jadvaldan o'chiradi va `VACUUM;` bilan bazani fizik qisqartiradi.
    Asosiy kartalar va naqd pul balansi (`finance_accounts`) daxlsiz saqlanadi!
    """
    cutoff_dt = (app.now() - timedelta(days=cutoff_days)).isoformat()
    cutoff_date = (app.today() - timedelta(days=cutoff_days)).isoformat()

    old_tx = db.q(
        "SELECT tx.*, a1.name AS from_name, a2.name AS to_name "
        "FROM finance_transactions tx "
        "LEFT JOIN finance_accounts a1 ON a1.id = tx.from_account_id "
        "LEFT JOIN finance_accounts a2 ON a2.id = tx.to_account_id "
        "WHERE tx.created_at < ? ORDER BY tx.id",
        (cutoff_dt,),
    )
    old_ch = db.q(
        "SELECT l.*, c.title AS challenge_title, t.title AS task_title "
        "FROM challenge_logs l "
        "LEFT JOIN challenges c ON c.id = l.challenge_id "
        "LEFT JOIN challenge_tasks t ON t.id = l.task_id "
        "WHERE l.log_date < ? ORDER BY l.id",
        (cutoff_date,),
    )

    if not old_tx and not old_ch:
        return {"archived_tx": 0, "archived_ch_logs": 0, "vacuumed": False}

    stamp = app.today().isoformat()
    payload = {
        "archived_at": app.now().isoformat(),
        "cutoff_days": cutoff_days,
        "finance_transactions": old_tx,
        "challenge_logs": old_ch,
    }
    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    # Avval Telegramga zaxira fayllarni yuboramiz (xato bo'lsa bazadan o'chirmaymiz!)
    await app.bot.send_document(
        app.CHAT_ID,
        BufferedInputFile(json_bytes, filename=f"altronbek_arxiv_{stamp}.json"),
        caption=(
            f"📦 <b>Avto-Arxiv ({cutoff_days} kundan eski yozuvlar)</b>\n"
            f"• Moliya tranzaksiyalari: {len(old_tx)} ta\n"
            f"• Chelenj loglari: {len(old_ch)} ta"
        ),
    )
    if old_tx:
        tx_csv = _csv_bytes(
            ["id", "vaqt", "summa", "kategoriya", "qayerdan", "qayerga", "izoh"],
            [[r["id"], r["created_at"], r["amount"], r["category"], r["from_name"] or "", r["to_name"] or "", r["note"] or ""]
             for r in old_tx],
        )
        await app.bot.send_document(
            app.CHAT_ID,
            BufferedInputFile(tx_csv, filename=f"arxiv_moliya_{stamp}.csv"),
            caption=f"📜 Moliya arxivi (CSV): {len(old_tx)} qator",
        )
    if old_ch:
        ch_csv = _csv_bytes(
            ["id", "sana", "chelenj", "vazifa", "qiymat", "holat"],
            [[r["id"], r["log_date"], r["challenge_title"] or "", r["task_title"] or "", r["value"], r["status"]]
             for r in old_ch],
        )
        await app.bot.send_document(
            app.CHAT_ID,
            BufferedInputFile(ch_csv, filename=f"arxiv_chelenj_{stamp}.csv"),
            caption=f"🏆 Chelenj loglari arxivi (CSV): {len(old_ch)} qator",
        )

    # Zaxira muvaffaqiyatli ketgach eski yozuvlarni o'chiramiz va fizik VACUUM qilamiz
    if old_tx:
        db.ex("DELETE FROM finance_transactions WHERE created_at < ?", (cutoff_dt,))
    if old_ch:
        db.ex("DELETE FROM challenge_logs WHERE log_date < ?", (cutoff_date,))
    db.set_setting("last_archive", app.now().strftime("%d.%m.%Y %H:%M"))
    db.vacuum()
    return {"archived_tx": len(old_tx), "archived_ch_logs": len(old_ch), "vacuumed": True}


# ------------------------------------------------------------------ Xulosa hisob-kitoblari va `<= 3 kun` filtri

def summary_totals() -> dict:
    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    cash = sum(int(a["balance"]) for a in accs if a["type"] == "cash")
    cards_total = sum(int(a["balance"]) for a in accs if a["type"] == "debit")
    credit_debt_total = sum(credit_card_debt(a) for a in accs if a["type"] == "credit")

    debts = db.q("SELECT * FROM finance_debts WHERE status = 'active'")
    receivables = sum(int(d["amount"]) for d in debts if int(d["amount"]) > 0)
    payables = sum(abs(int(d["amount"])) for d in debts if int(d["amount"]) < 0)

    return {
        "accounts": accs,
        "cash": cash,
        "cards_total": cards_total,
        "credit_debt": credit_debt_total,
        "receivables": receivables,
        "payables": payables,
    }


def upcoming_payments(ref_today: date | None = None, max_days: int = 3) -> list[dict]:
    """
    FAQAT keyingi `0 <= kun <= max_days` (standart `<= 3 kun`) ichidagi to'lovlarni qaytaradi.
    `status = 'paid'` kreditlar, `status = 'settled'` qarzlar va shu oyda to'liq yopilgan davriy to'lovlar kirmaydi!
    """
    d = ref_today or app.today()
    ym = d.strftime("%Y-%m")
    sync_recurring_months(d)
    items: list[dict] = []

    for acc in db.q("SELECT * FROM finance_accounts WHERE is_active = 1 AND type = 'credit'"):
        c_debt = credit_card_debt(acc)
        if c_debt <= 0:
            continue
        past_8_cutoff = (d.day > 8) or (
            d.day == 8 and d == app.today() and app.now().strftime("%H:%M") > "17:00"
        )
        due_dt = sv.next_monthly_due_date(d, 8, skip_this_month=past_8_cutoff)
        left = (due_dt - d).days
        if 0 <= left <= max_days:
            items.append({
                "kind": "credit_card",
                "id": acc["id"],
                "title": f"Kredit karta: {acc['name']}",
                "amount": c_debt,
                "due_date": due_dt.isoformat(),
                "days_left": left,
                "deadline_note": "17:00 gacha",
            })

    for ln in db.q("SELECT * FROM finance_loans WHERE status = 'active'"):
        if int(ln["remaining_debt"]) <= 0 or int(ln["remaining_installments"]) <= 0:
            continue
        paid_this_m = (ln.get("last_paid_month") == ym)
        due_dt = sv.next_monthly_due_date(d, int(ln["due_day"]), skip_this_month=paid_this_m)
        if not paid_this_m and ln.get("grace_deadline_day"):
            g_cand = sv.clamp_day(d.year, d.month, int(ln["grace_deadline_day"]))
            if g_cand >= d and g_cand < due_dt:
                due_dt = g_cand
        left = (due_dt - d).days
        if 0 <= left <= max_days:
            pay_amt = min(int(ln["monthly_payment"]), int(ln["remaining_debt"]))
            items.append({
                "kind": "loan",
                "id": ln["id"],
                "title": ln["title"],
                "amount": pay_amt,
                "due_date": due_dt.isoformat(),
                "days_left": left,
                "deadline_note": f"{ln['grace_deadline_time']} gacha" if ln.get("grace_deadline_time") else "",
            })

    for rc in db.q("SELECT * FROM finance_recurring WHERE status = 'active'"):
        rem = int(rc["target_amount"]) - int(rc["paid_amount_this_month"])
        paid_this_m = rem <= 0 or (rc.get("last_paid_month") == ym)
        s_dt = sv.clamp_day(d.year, d.month, int(rc["due_start_day"]))
        e_dt = sv.clamp_day(d.year, d.month, int(rc["due_end_day"]))
        if not paid_this_m and s_dt <= d <= e_dt:
            due_dt = d
        else:
            due_dt = sv.next_monthly_due_date(
                d, int(rc["due_start_day"]), skip_this_month=(paid_this_m or d > e_dt)
            )
            rem = int(rc["target_amount"]) if paid_this_m else rem
        left = (due_dt - d).days
        if 0 <= left <= max_days:
            items.append({
                "kind": "recurring",
                "id": rc["id"],
                "title": rc["title"],
                "amount": rem,
                "due_date": due_dt.isoformat(),
                "days_left": left,
                "deadline_note": f"{rc['due_start_day']}–{rc['due_end_day']} sanalar",
            })

    for db_row in db.q("SELECT * FROM finance_debts WHERE status = 'active' AND amount < 0 AND due_date IS NOT NULL"):
        try:
            due_dt = date.fromisoformat(db_row["due_date"])
        except ValueError:
            continue
        left = (due_dt - d).days
        if 0 <= left <= max_days:
            items.append({
                "kind": "debt",
                "id": db_row["id"],
                "title": f"Qarz: {db_row['person_name']}",
                "amount": abs(int(db_row["amount"])),
                "due_date": due_dt.isoformat(),
                "days_left": left,
                "deadline_note": "",
            })

    items.sort(key=lambda x: (x["days_left"], x["title"]))
    return items


# ------------------------------------------------------------------ Eslatmalar va APScheduler job'lari

def _once_guard(key: str) -> bool:
    if db.get_setting(key):
        return False
    db.set_setting(key, "1")
    return True


async def check_finance_alerts(now_dt: datetime | None = None) -> int:
    now_dt = now_dt or app.now()
    d = now_dt.date()
    d_iso = d.isoformat()
    hm = now_dt.strftime("%H:%M")
    ym = d.strftime("%Y-%m")
    sync_recurring_months(d)
    sent_count = 0
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("💰 Moliya panelini ochish", "fn:home")]])

    # 1) Kredit kartalar: 5, 7 va 8-sanalar (8-sanada faqat <= 17:00!)
    if d.day in (5, 7) or (d.day == 8 and hm <= "17:00"):
        slot = f"d{d.day}" if d.day in (5, 7) else ("d8_urgent" if hm >= "13:00" else "d8_am")
        for acc in db.q("SELECT * FROM finance_accounts WHERE is_active = 1 AND type = 'credit'"):
            c_debt = credit_card_debt(acc)
            if c_debt <= 0:
                continue
            gkey = f"fwarn_cc_{acc['id']}_{d_iso}_{slot}"
            if not _once_guard(gkey):
                continue
            prefix = "🔥 <b>QAT'IY ESLATMA (bugun 17:00 gacha!)</b>" if d.day == 8 else f"⚠️ <b>Kredit karta eslatmasi ({8 - d.day} kun qoldi)</b>"
            await messenger.send(
                f"{prefix}\n💳 <b>{app.esc(acc['name'])}</b> bo'yicha yopilishi kerak: <b>{fmt_sum(c_debt)} so'm</b>\n"
                "Muddat: <b>8-sana soat 17:00 gacha</b>.",
                "reminder", kb, actionable=True, bind_trigger=False,
            )
            sent_count += 1

    # 2) Kreditlar (`status = 'active'`)
    for ln in db.q("SELECT * FROM finance_loans WHERE status = 'active'"):
        if int(ln["remaining_debt"]) <= 0 or int(ln["remaining_installments"]) <= 0:
            continue
        if ln.get("last_paid_month") == ym:
            continue
        g_day = ln.get("grace_deadline_day")
        g_time = ln.get("grace_deadline_time") or "17:00"
        if g_day:
            g_dt = sv.clamp_day(d.year, d.month, int(g_day))
            left = (g_dt - d).days
            if left in (3, 1) or (left == 0 and hm <= g_time):
                slot = f"g{left}" if left > 0 else ("g0_pm" if hm >= "13:00" else "g0_am")
                if _once_guard(f"fwarn_ln_{ln['id']}_{d_iso}_{slot}"):
                    pay_amt = min(int(ln["monthly_payment"]), int(ln["remaining_debt"]))
                    urg = f"🔥 <b>Bugun {g_time} gacha!</b>" if left == 0 else f"⚠️ <b>{left} kun qoldi</b>"
                    await messenger.send(
                        f"{urg} — Kredit: <b>{app.esc(ln['title'])}</b>\n"
                        f"To'lov: <b>{fmt_sum(pay_amt)} so'm</b> (qoldiq: {ln['remaining_installments']} oy, {fmt_sum(ln['remaining_debt'])} so'm)",
                        "reminder", kb, actionable=True, bind_trigger=False,
                    )
                    sent_count += 1
        else:
            due_dt = sv.clamp_day(d.year, d.month, int(ln["due_day"]))
            left = (due_dt - d).days
            if left in (1, 0) and _once_guard(f"fwarn_lnd_{ln['id']}_{d_iso}"):
                pay_amt = min(int(ln["monthly_payment"]), int(ln["remaining_debt"]))
                await messenger.send(
                    f"🏦 <b>Kredit to'lovi ({'bugun' if left == 0 else 'ertaga'}): {app.esc(ln['title'])}</b>\n"
                    f"Summa: <b>{fmt_sum(pay_amt)} so'm</b>",
                    "reminder", kb, actionable=True, bind_trigger=False,
                )
                sent_count += 1

    # 3) Davriy to'lovlar
    for rc in db.q("SELECT * FROM finance_recurring WHERE status = 'active'"):
        rem = int(rc["target_amount"]) - int(rc["paid_amount_this_month"])
        if rem <= 0 or rc.get("last_paid_month") == ym:
            continue
        s_dt = sv.clamp_day(d.year, d.month, int(rc["due_start_day"]))
        e_dt = sv.clamp_day(d.year, d.month, int(rc["due_end_day"]))
        if d in (s_dt, e_dt) and _once_guard(f"fwarn_rc_{rc['id']}_{d_iso}"):
            await messenger.send(
                f"🏠 <b>Davriy to'lov: {app.esc(rc['title'])}</b> ({rc['due_start_day']}–{rc['due_end_day']} sanalar)\n"
                f"Shu oy to'landi: {fmt_sum(rc['paid_amount_this_month'])} / {fmt_sum(rc['target_amount'])} so'm\n"
                f"Qoldiq: <b>{fmt_sum(rem)} so'm</b>",
                "reminder", kb, actionable=True, bind_trigger=False,
            )
            sent_count += 1

    return sent_count


def register_jobs(scheduler):
    scheduler.add_job(check_finance_alerts, CronTrigger(hour=9, minute=30), id="finance_am", replace_existing=True)
    scheduler.add_job(check_finance_alerts, CronTrigger(hour=15, minute=0), id="finance_pm", replace_existing=True)
    scheduler.add_job(
        run_auto_archive_and_vacuum,
        IntervalTrigger(days=ARCHIVE_DAYS),
        id="quarterly_archive",
        replace_existing=True,
    )


# ------------------------------------------------------------------ 06:30 Xulosa va Start-up Sync

def morning_finance_section(ref_today: date | None = None) -> str:
    ref_today = ref_today or app.today()
    tot = summary_totals()
    upc = upcoming_payments(ref_today, max_days=3)
    lines = [
        "\n\n💰 <b>Moliya holati:</b>",
        f"• 💵 Naqd: <b>{fmt_sum(tot['cash'])} so'm</b>",
        f"• 💳 Kartalarda (jami): <b>{fmt_sum(tot['cards_total'])} so'm</b>",
        f"• 📥 Haqlar: <b>{fmt_sum(tot['receivables'])} so'm</b>",
    ]
    if upc:
        lines.append("• ⏳ <b>Yaqin to'lovlar (≤3 kun):</b>")
        for it in upc:
            when = "bugun" if it["days_left"] == 0 else f"{it['days_left']} kunda"
            extra = f", {it['deadline_note']}" if it["deadline_note"] else ""
            lines.append(f"   — {app.esc(it['title'])}: <b>{fmt_sum(it['amount'])} so'm</b> ({when}{extra})")
    else:
        lines.append("• ⏳ Yaqin to'lovlar (≤3 kun): yo'q")
    return "\n".join(lines)


def build_startup_sync_text_kb() -> tuple[str, InlineKeyboardMarkup]:
    tot = summary_totals()
    upc = upcoming_payments(app.today(), max_days=3)
    lines = [
        "🔄 <b>Moliyaviy holat tekshiruvi (Start-up sync)</b>\n",
        f"💵 Naqd: <b>{fmt_sum(tot['cash'])} so'm</b>",
        f"💳 Kartalarda (jami): <b>{fmt_sum(tot['cards_total'])} so'm</b>",
    ]
    if tot["credit_debt"] > 0:
        lines.append(f"🏦 Kredit karta qarzi: <b>{fmt_sum(tot['credit_debt'])} so'm</b>")
    lines.append(f"📥 Haqlar: <b>{fmt_sum(tot['receivables'])} so'm</b> · 📤 Qarzlar: <b>{fmt_sum(tot['payables'])} so'm</b>")
    if upc:
        lines.append("\n⏳ <b>Yaqin to'lovlar (≤3 kun):</b>")
        for it in upc[:5]:
            when = "bugun" if it["days_left"] == 0 else f"{it['days_left']} kunda"
            lines.append(f"• {app.esc(it['title'])}: <b>{fmt_sum(it['amount'])} so'm</b> ({when})")
    lines.append("\nJoriy qoldiqlar to'g'rimi?")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ To'g'ri", "fn:sync:ok"), btn("➕ Qo'shish", "fn:add"), btn("✏️ Tahrirlash", "fn:home")],
    ])
    return "\n".join(lines), kb


async def startup_sync():
    seed_finance_categories()
    text, kb = build_startup_sync_text_kb()
    await messenger.send(text, "system", kb, actionable=True, bind_trigger=False)


# ------------------------------------------------------------------ In-place UI yordamchilari va Minglik tasdig'i

async def _edit_or_send(target_msg, text: str, kb: InlineKeyboardMarkup | None, state: FSMContext | None = None):
    markup = messenger.with_delete(kb)
    mid = target_msg.message_id if target_msg is not None else None
    if mid is None and state is not None:
        mid = (await state.get_data()).get("panel_mid")
    if mid is not None:
        try:
            await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=markup)
            db.ex("UPDATE msg_log SET category = 'info', actionable = 0, text = ? WHERE message_id = ? AND deleted_at IS NULL",
                  (text, mid))
            if state is not None:
                await state.update_data(panel_mid=mid)
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return mid
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    if sent and state is not None:
        await state.update_data(panel_mid=sent.message_id)
    return sent.message_id if sent else None


async def _edit_direct(mid: int | None, text: str, kb: InlineKeyboardMarkup) -> int | None:
    if mid is not None:
        try:
            await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=messenger.with_delete(kb))
            db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, mid))
            return mid
        except Exception:
            pass
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    return sent.message_id if sent else None


def _ambiguous_kb(raw_num: int) -> InlineKeyboardMarkup:
    rows = [[btn(f"✅ {fmt_sum(raw_num * 1000)} so'm", f"fn:cf:{raw_num * 1000}")]]
    if raw_num % 10 == 0:
        rows.append([btn(f"{fmt_sum(raw_num * 100)} so'm", f"fn:cf:{raw_num * 100}")])
    rows.append([btn(f"{fmt_sum(raw_num)} so'm", f"fn:cf:{raw_num}"), btn("❌ Bekor", "fn:cf:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _handle_amount_step(
    state: FSMContext,
    raw_text: str,
    step_key: str,
    allow_zero: bool = False,
) -> int | None:
    parsed = parse_fin_amount(raw_text, allow_zero=allow_zero)
    if parsed is None:
        return None
    if parsed["ambiguous"]:
        raw_int = int(parsed["raw_num"])
        await state.set_state(FinFSM.confirm_amount)
        await state.update_data(amb_step=step_key)
        await _edit_or_send(
            None,
            f"🤔 <code>{raw_int}</code> kiritildi. Qaysi summani nazarda tutdingiz?",
            _ambiguous_kb(raw_int),
            state,
        )
        return -1
    return int(parsed["amount"])


# ------------------------------------------------------------------ Asosiy ekranlar va «📜 Tarix» (Pagination)

def home_view() -> tuple[str, InlineKeyboardMarkup]:
    seed_finance_categories()
    tot = summary_totals()
    upc = upcoming_payments(app.today(), max_days=3)
    lines = [
        "💰 <b>Moliya boshqaruv paneli</b>\n",
        f"💵 Naqd: <b>{fmt_sum(tot['cash'])} so'm</b>",
        f"💳 Kartalarda (jami): <b>{fmt_sum(tot['cards_total'])} so'm</b>",
    ]
    if tot["credit_debt"] > 0:
        lines.append(f"🏦 Kredit karta qarzi: <b>{fmt_sum(tot['credit_debt'])} so'm</b>")
    lines.append(f"📥 Haqlar: <b>{fmt_sum(tot['receivables'])}</b> · 📤 Qarzlar: <b>{fmt_sum(tot['payables'])}</b>")

    if tot["accounts"]:
        lines.append("\n<b>Hisoblar:</b>")
        for a in tot["accounts"]:
            l4 = f" (*{a['last_4']})" if a.get("last_4") else ""
            extra = f" [qarz: {fmt_sum(credit_card_debt(a))}]" if a["type"] == "credit" else ""
            lines.append(f"• {ACC_TYPE_LABELS[a['type']]} <b>{app.esc(a['name'])}{app.esc(l4)}</b>: {fmt_sum(a['balance'])}{extra}")

    if upc:
        lines.append("\n⏳ <b>Yaqin to'lovlar (≤3 kun):</b>")
        for it in upc:
            when = "bugun" if it["days_left"] == 0 else f"{it['days_left']} kunda"
            lines.append(f"• {app.esc(it['title'])}: <b>{fmt_sum(it['amount'])} so'm</b> ({when})")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("➖ Chiqim", "fn:tx:exp"), btn("➕ Kirim", "fn:tx:inc"), btn("🔄 O'tkazma", "fn:tx:trf")],
        [btn("💳 Hisoblar", "fn:accs"), btn("🤝 Qarz / Haqlar", "fn:debts")],
        [btn("🏦 Kreditlar", "fn:loans"), btn("🏠 Davriy to'lovlar", "fn:recs")],
        [btn("📜 Tarix", "fn:hist:0"), btn("🗂 Kategoriyalar", "fn:cmng:exp:home")],
    ])
    return "\n".join(lines), kb


def history_view(page: int = 0, per_page: int = HISTORY_PAGE_SIZE) -> tuple[str, InlineKeyboardMarkup]:
    """Tranzaksiyalar tarixi (oxirgi 7-10 tadan sahifalab ko'rsatish)."""
    total = db.q1("SELECT COUNT(*) AS c FROM finance_transactions")["c"]
    if total == 0:
        return (
            "📜 <b>Tranzaksiyalar tarixi</b>\n\nHozircha tranzaksiyalar yo'q.",
            InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Moliya paneli", "fn:home")]]),
        )
    max_page = max(0, (total - 1) // per_page)
    page = min(max(0, int(page)), max_page)
    offset = page * per_page

    rows_db = db.q(
        "SELECT tx.*, a1.name AS from_name, a2.name AS to_name "
        "FROM finance_transactions tx "
        "LEFT JOIN finance_accounts a1 ON a1.id = tx.from_account_id "
        "LEFT JOIN finance_accounts a2 ON a2.id = tx.to_account_id "
        "ORDER BY tx.id DESC LIMIT ? OFFSET ?",
        (per_page, offset),
    )

    lines = [f"📜 <b>Tranzaksiyalar tarixi</b> (sahifa {page + 1}/{max_page + 1}, jami {total} ta)\n"]
    for r in rows_db:
        try:
            dt_s = datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
        except ValueError:
            dt_s = (r["created_at"] or "")[:16]

        if r["from_account_id"] and r["to_account_id"]:
            sign_s = f"🔄 <b>{fmt_sum(r['amount'])}</b>"
            acc_s = f"{app.esc(r['from_name'] or '?')} → {app.esc(r['to_name'] or '?')}"
        elif r["from_account_id"]:
            sign_s = f"➖ <b>{fmt_sum(r['amount'])}</b>"
            acc_s = app.esc(r["from_name"] or "?")
        else:
            sign_s = f"➕ <b>{fmt_sum(r['amount'])}</b>"
            acc_s = app.esc(r["to_name"] or "?")

        cat_s = app.esc(r["category"])
        note_s = f" — <i>{app.esc(r['note'])}</i>" if r.get("note") else ""
        lines.append(f"• <code>{dt_s}</code> {sign_s} so'm · {cat_s} [{acc_s}]{note_s}")

    nav_row = []
    if page > 0:
        nav_row.append(btn("⬅️ Oldingi", f"fn:hist:{page - 1}"))
    if page < max_page:
        nav_row.append(btn("Keyingi ➡️", f"fn:hist:{page + 1}"))
    kb_rows = []
    if nav_row:
        kb_rows.append(nav_row)
    kb_rows.append([btn("⬅️ Moliya paneli", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.startswith("fn:hist:"))
async def cb_history(cb: CallbackQuery, state: FSMContext):
    page = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    text, kb = history_view(page)
    await _edit_or_send(cb.message, text, kb)


def add_hub_view() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("💳 Yangi hisob/karta", "fn:anew"), btn("🤝 Yangi qarz/haq", "fn:dnew")],
        [btn("🏦 Yangi kredit", "fn:lnew"), btn("🏠 Yangi davriy to'lov", "fn:rnew")],
        [btn("➖ Chiqim yozish", "fn:tx:exp"), btn("➕ Kirim yozish", "fn:tx:inc")],
        [btn("⬅️ Moliya paneli", "fn:home")],
    ])
    return "➕ <b>Moliya: nimani qo'shamiz?</b>", kb


async def entry(message: Message):
    text, kb = home_view()
    await messenger.send(text, "info", kb)


@router.message(Command("moliya"))
async def cmd_finance(message: Message, state: FSMContext):
    await state.clear()
    await entry(message)


@router.callback_query(F.data == "add:fin")
async def cb_from_main_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = add_hub_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "fn:sync:ok")
async def cb_sync_ok(cb: CallbackQuery):
    await cb.answer("Tasdiqlandi!")
    await messenger.edit_result(
        cb.message,
        f"✅ Moliyaviy holat tasdiqlandi ({app.now().strftime('%d.%m %H:%M')}).",
        "confirm",
    )


@router.callback_query(F.data == "fn:add")
async def cb_add_hub(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = add_hub_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "fn:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = home_view()
    await _edit_or_send(cb.message, text, kb)


# ------------------------------------------------------------------ Undo tasdiq xabari va `fn:undo:<tx_id>`

def _tx_confirm_kb(tx_id: int | None, with_undo: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if with_undo and tx_id is not None:
        rows.append([btn("↩️ Bekor qilish (1 daq)", f"fn:undo:{tx_id}")])
    rows.append([btn("💰 Moliya paneli", "fn:home"), btn("📜 Tarix", "fn:hist:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def expire_undo_markup(msg_id: int, tx_id: int):
    """1 daqiqa o'tgach tasdiq xabaridan «↩️ Bekor qilish» tugmasini olib tashlaydi."""
    try:
        await app.bot.edit_message_reply_markup(
            chat_id=app.CHAT_ID,
            message_id=msg_id,
            reply_markup=messenger.with_delete(_tx_confirm_kb(tx_id, with_undo=False)),
        )
    except Exception:
        pass


async def _show_tx_confirmation(p_mid: int | None, summary_html: str, tx_id: int | None):
    kb = _tx_confirm_kb(tx_id, with_undo=(tx_id is not None))
    mid = await _edit_direct(p_mid, summary_html, kb)
    if mid is not None and tx_id is not None:
        app.schedule_job(
            app.now() + timedelta(seconds=UNDO_WINDOW_SEC),
            expire_undo_markup,
            [mid, tx_id],
            f"undo_exp_{tx_id}",
        )


@router.callback_query(F.data.startswith("fn:undo:"))
async def cb_undo_tx(cb: CallbackQuery, state: FSMContext):
    tx_id = int(cb.data.split(":")[2])
    res = undo_transaction(tx_id)
    if not res["ok"]:
        await cb.answer(res["error"], show_alert=True)
        if res.get("expired"):
            try:
                await cb.message.edit_reply_markup(
                    reply_markup=messenger.with_delete(_tx_confirm_kb(tx_id, with_undo=False))
                )
            except Exception:
                pass
        return
    await state.clear()
    await cb.answer("↩️ Amaliyot bekor qilindi!")
    text, kb = home_view()
    banner = (
        f"↩️ <b>Amaliyot bekor qilindi:</b> {fmt_sum(res['amount'])} so'm ({app.esc(res['category'])}) — "
        "balans avvalgi holatiga qaytarildi.\n\n"
    )
    await _edit_or_send(cb.message, banner + text, kb)


# ------------------------------------------------------------------ 1) Hisoblar (`finance_accounts`) CRUD

def _accounts_view() -> tuple[str, InlineKeyboardMarkup]:
    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    lines = ["💳 <b>Hisoblar va kartalar</b>\n"]
    rows = []
    if not accs:
        lines.append("Hali hisob qo'shilmagan.")
    for a in accs:
        l4 = f" *{a['last_4']}" if a.get("last_4") else ""
        lim_s = f" / limit {fmt_sum(a['credit_limit'])}" if a["type"] == "credit" and a["credit_limit"] else ""
        lines.append(f"• {ACC_TYPE_LABELS[a['type']]} <b>{app.esc(a['name'])}{app.esc(l4)}</b>: <b>{fmt_sum(a['balance'])}{lim_s} so'm</b>")
        rows.append([
            btn(f"✏️ Qoldiq: {a['name'][:16]}", f"fn:aeb:{a['id']}"),
            btn("🗑", f"fn:adel:{a['id']}"),
        ])
    rows.append([btn("➕ Hisob qo'shish", "fn:anew"), btn("⬅️ Orqaga", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "fn:accs")
async def cb_accs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = _accounts_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "fn:anew")
async def cb_acc_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(FinFSM.acc_name)
    await _edit_or_send(
        cb.message,
        "💳 <b>Yangi hisob qo'shish</b>\n\nHisob yoki karta nomini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
        state,
    )


@router.message(FinFSM.acc_name)
async def msg_acc_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not name:
        return
    await state.update_data(acc_name=name)
    await state.set_state(FinFSM.acc_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("💵 Naqd (cash)", "fn:at:cash")],
        [btn("💳 Debet karta (debit)", "fn:at:debit")],
        [btn("🏦 Kredit karta (credit)", "fn:at:credit")],
        [btn("⬅️ Bekor", "fn:accs")],
    ])
    await _edit_or_send(None, f"💳 <b>{app.esc(name)}</b> — hisob turini tanlang:", kb, state)


@router.callback_query(FinFSM.acc_type, F.data.startswith("fn:at:"))
async def cb_acc_type(cb: CallbackQuery, state: FSMContext):
    atype = cb.data.split(":")[2]
    await cb.answer()
    await state.update_data(acc_type=atype)
    if atype == "cash":
        await state.update_data(acc_last4=None)
        await state.set_state(FinFSM.acc_balance)
        await _edit_or_send(
            cb.message,
            "💵 Joriy naqd qoldiqni yozing (masalan: <code>250</code> -> 250 000 yoki <code>0</code>):",
            InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
            state,
        )
    else:
        await state.set_state(FinFSM.acc_last4)
        await _edit_or_send(
            cb.message,
            "🔢 Kartaning oxirgi 4 raqamini yozing (yoki o'tkazib yuborish uchun <code>-</code>):",
            InlineKeyboardMarkup(inline_keyboard=[[btn("⏭ O'tkazib yuborish", "fn:al4skip")], [btn("⬅️ Bekor", "fn:accs")]]),
            state,
        )


@router.callback_query(FinFSM.acc_last4, F.data == "fn:al4skip")
async def cb_acc_l4_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(acc_last4=None)
    await state.set_state(FinFSM.acc_balance)
    await _edit_or_send(
        cb.message,
        "💰 Kartadagi joriy qoldiq summani yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
        state,
    )


@router.message(FinFSM.acc_last4)
async def msg_acc_last4(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    l4 = None if raw == "-" else (raw[-4:] if raw.isdigit() and len(raw) >= 4 else None)
    await state.update_data(acc_last4=l4)
    await state.set_state(FinFSM.acc_balance)
    await _edit_or_send(
        None,
        "💰 Kartadagi joriy qoldiq summani yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
        state,
    )


@router.message(FinFSM.acc_balance)
async def msg_acc_balance(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "acc_balance", allow_zero=True)
    if amt is None or amt == -1:
        return
    await _finish_acc_balance(state, amt)


async def _finish_acc_balance(state: FSMContext, amt: int):
    data = await state.get_data()
    if data["acc_type"] == "credit":
        await state.update_data(acc_balance=amt)
        await state.set_state(FinFSM.acc_limit)
        await _edit_or_send(
            None,
            "🏦 Kredit karta umumiy limitini yozing (masalan: <code>5m</code> yoki <code>5000000</code>):",
            InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
            state,
        )
        return
    create_account(data["acc_name"], data["acc_type"], balance=amt, last_4=data.get("acc_last4"))
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _accounts_view()
    await _edit_direct(p_mid, text, kb)


@router.message(FinFSM.acc_limit)
async def msg_acc_limit(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "acc_limit", allow_zero=True)
    if amt is None or amt == -1:
        return
    await _finish_acc_limit(state, amt)


async def _finish_acc_limit(state: FSMContext, lim: int):
    data = await state.get_data()
    create_account(
        data["acc_name"], "credit", balance=int(data["acc_balance"]),
        credit_limit=lim, last_4=data.get("acc_last4"),
    )
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _accounts_view()
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data.startswith("fn:aeb:"))
async def cb_acc_edit_bal(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ?", (aid,))
    if not acc:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.set_state(FinFSM.acc_edit_bal)
    await state.update_data(edit_acc_id=aid, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        f"✏️ <b>{app.esc(acc['name'])}</b> (hozir: {fmt_sum(acc['balance'])} so'm)\nYangi qoldiqni yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:accs")]]),
        state,
    )


@router.message(FinFSM.acc_edit_bal)
async def msg_acc_edit_bal(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "acc_edit_bal", allow_zero=True)
    if amt is None or amt == -1:
        return
    await _finish_acc_edit_bal(state, amt)


async def _finish_acc_edit_bal(state: FSMContext, amt: int):
    data = await state.get_data()
    update_account_balance(data["edit_acc_id"], amt)
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _accounts_view()
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data.startswith("fn:adel:"))
async def cb_acc_del(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    archive_account(aid)
    await cb.answer("Hisob arxivlandi")
    text, kb = _accounts_view()
    await _edit_or_send(cb.message, text, kb)


# ------------------------------------------------------------------ 2) Kategoriyalar va Amaliyotlar (`finance_transactions`)

def _category_picker_view(cat_type: str, acc_name: str) -> tuple[str, InlineKeyboardMarkup]:
    short_t = "exp" if cat_type == "expense" else "inc"
    cats = list_categories(cat_type)
    rows, pair = [], []
    for c in cats:
        pair.append(btn(f"{c['icon']} {c['name'][:18]}", f"fn:txc:{c['id']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("➕ Yangi kategoriya", f"fn:cnew:{short_t}:tx"), btn("⚙️ Tahrirlash", f"fn:cmng:{short_t}:tx")])
    rows.append([btn("⬅️ Bekor", "fn:home")])
    hdr = "➖ Chiqim kategoriyasini tanlang" if cat_type == "expense" else "➕ Kirim kategoriyasini tanlang"
    return f"<b>{hdr}</b>\nHisob: <b>{app.esc(acc_name)}</b>", InlineKeyboardMarkup(inline_keyboard=rows)


def _category_manage_view(short_t: str, origin: str) -> tuple[str, InlineKeyboardMarkup]:
    cat_type = "expense" if short_t == "exp" else "income"
    other_t = "inc" if short_t == "exp" else "exp"
    cats = list_categories(cat_type)
    title_t = "Chiqim kategoriyalari" if cat_type == "expense" else "Kirim kategoriyalari"
    lines = [f"🗂 <b>{title_t}</b> (🔒 tizimli — o'chirilmaydi)\n"]
    rows = [
        [
            btn(f"{'• ' if short_t == 'exp' else ''}➖ Chiqim", f"fn:cmng:exp:{origin}"),
            btn(f"{'• ' if short_t == 'inc' else ''}➕ Kirim", f"fn:cmng:inc:{origin}"),
        ]
    ]
    for c in cats:
        sys_mark = " 🔒" if int(c["is_system"]) else ""
        lines.append(f"• {c['icon']} <b>{app.esc(c['name'])}</b>{sys_mark} ({c['usage_count']} marta)")
        r = [btn(f"✏️ {c['icon']} {c['name'][:16]}", f"fn:cren:{c['id']}:{short_t}:{origin}")]
        if not int(c["is_system"]):
            r.append(btn("🗑", f"fn:cdel:{c['id']}:{short_t}:{origin}"))
        rows.append(r)
    rows.append([btn("➕ Yangi kategoriya", f"fn:cnew:{short_t}:{origin}")])
    back_cb = "fn:txbackcat" if origin == "tx" else "fn:home"
    rows.append([btn("⬅️ Orqaga", back_cb)])
    _ = other_t
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("fn:cmng:"))
async def cb_cat_manage(cb: CallbackQuery, state: FSMContext):
    _, _, short_t, origin = cb.data.split(":")
    await cb.answer()
    text, kb = _category_manage_view(short_t, origin)
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data == "fn:txbackcat")
async def cb_tx_back_to_cat(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    mode = data.get("tx_mode")
    aid = data.get("from_acc") or data.get("to_acc")
    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ?", (aid,)) if aid else None
    if not acc or mode not in ("exp", "inc"):
        text, kb = home_view()
        await _edit_or_send(cb.message, text, kb)
        return
    cat_type = "expense" if mode == "exp" else "income"
    text, kb = _category_picker_view(cat_type, acc["name"])
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("fn:cnew:"))
async def cb_cat_new(cb: CallbackQuery, state: FSMContext):
    _, _, short_t, origin = cb.data.split(":")
    await cb.answer()
    await state.set_state(FinFSM.cat_add)
    await state.update_data(cat_short=short_t, cat_origin=origin, panel_mid=cb.message.message_id)
    back_cb = "fn:txbackcat" if origin == "tx" else f"fn:cmng:{short_t}:{origin}"
    await _edit_or_send(
        cb.message,
        "➕ <b>Yangi kategoriya</b>\n\nIkonka va nomini yozing (masalan: <code>☕ Qahva</code> yoki <code>Texnika</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", back_cb)]]),
        state,
    )


@router.message(FinFSM.cat_add)
async def msg_cat_add(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    short_t = data.get("cat_short", "exp")
    origin = data.get("cat_origin", "home")
    icon, name = parse_icon_and_name(raw, "📌")
    if not name:
        return
    cat_type = "expense" if short_t == "exp" else "income"
    create_category(name, cat_type, icon)
    p_mid = data.get("panel_mid")
    if origin == "tx":
        aid = data.get("from_acc") or data.get("to_acc")
        acc = db.q1("SELECT * FROM finance_accounts WHERE id = ?", (aid,)) if aid else None
        if acc:
            await state.set_state(None)
            text, kb = _category_picker_view(cat_type, acc["name"])
            await _edit_direct(p_mid, text, kb)
            return
    await state.clear()
    text, kb = _category_manage_view(short_t, "home")
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data.startswith("fn:cren:"))
async def cb_cat_rename(cb: CallbackQuery, state: FSMContext):
    _, _, cid_s, short_t, origin = cb.data.split(":")
    cid = int(cid_s)
    cat = db.q1("SELECT * FROM finance_categories WHERE id = ?", (cid,))
    if not cat:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.set_state(FinFSM.cat_rename)
    await state.update_data(edit_cat_id=cid, cat_short=short_t, cat_origin=origin, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        f"✏️ <b>{cat['icon']} {app.esc(cat['name'])}</b> — yangi ikonka va nomni yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", f"fn:cmng:{short_t}:{origin}")]]),
        state,
    )


@router.message(FinFSM.cat_rename)
async def msg_cat_rename(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    cat = db.q1("SELECT * FROM finance_categories WHERE id = ?", (data["edit_cat_id"],))
    if cat and raw:
        icon, name = parse_icon_and_name(raw, cat["icon"])
        if name:
            update_category(cat["id"], name, icon)
    short_t = data.get("cat_short", "exp")
    origin = data.get("cat_origin", "home")
    p_mid = data.get("panel_mid")
    if origin != "tx":
        await state.clear()
    else:
        await state.set_state(None)
    text, kb = _category_manage_view(short_t, origin)
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data.startswith("fn:cdel:"))
async def cb_cat_delete(cb: CallbackQuery, state: FSMContext):
    _, _, cid_s, short_t, origin = cb.data.split(":")
    ok = delete_category(int(cid_s))
    await cb.answer("Kategoriya o'chirildi" if ok else "Tizimli kategoriyani o'chirib bo'lmaydi", show_alert=not ok)
    text, kb = _category_manage_view(short_t, origin)
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("fn:tx:"))
async def cb_tx_start(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]  # exp | inc | trf
    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    if not accs or (mode == "trf" and len(accs) < 2):
        await cb.answer("Avval kerakli hisob(lar)ni qo'shing!", show_alert=True)
        return
    await cb.answer()
    await state.clear()
    await state.update_data(tx_mode=mode, panel_mid=cb.message.message_id)
    title_map = {"exp": "➖ Chiqim: qaysi hisobdan?", "inc": "➕ Kirim: qaysi hisobga?", "trf": "🔄 O'tkazma: qaysi hisobdan?"}
    rows = [[btn(f"{ACC_TYPE_LABELS[a['type']]} {a['name'][:18]} ({fmt_sum(a['balance'])})", f"fn:txa1:{a['id']}")] for a in accs]
    rows.append([btn("⬅️ Bekor", "fn:home")])
    await _edit_or_send(cb.message, f"<b>{title_map[mode]}</b>", InlineKeyboardMarkup(inline_keyboard=rows), state)


@router.callback_query(F.data.startswith("fn:txa1:"))
async def cb_tx_acc1(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ?", (aid,))
    if not acc:
        await cb.answer("Hisob topilmadi")
        return
    data = await state.get_data()
    mode = data.get("tx_mode", "exp")
    await cb.answer()
    if mode == "trf":
        await state.update_data(from_acc=aid, cat_id=None, cat_label="🔄 O'tkazma")
        accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 AND id != ? ORDER BY usage_count DESC, id ASC", (aid,))
        rows = [[btn(f"➡️ {a['name'][:20]} ({fmt_sum(a['balance'])})", f"fn:txa2:{a['id']}")] for a in accs]
        rows.append([btn("⬅️ Bekor", "fn:home")])
        await _edit_or_send(cb.message, "<b>🔄 Qaysi hisobga o'tkazamiz?</b>", InlineKeyboardMarkup(inline_keyboard=rows), state)
        return

    if mode == "exp":
        await state.update_data(from_acc=aid, to_acc=None)
        text, kb = _category_picker_view("expense", acc["name"])
    else:
        await state.update_data(from_acc=None, to_acc=aid)
        text, kb = _category_picker_view("income", acc["name"])
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("fn:txc:"))
async def cb_tx_pick_category(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    cat = db.q1("SELECT * FROM finance_categories WHERE id = ?", (cid,))
    if not cat:
        await cb.answer("Kategoriya topilmadi")
        return
    await cb.answer()
    cat_label = f"{cat['icon']} {cat['name']}"
    sys_key = cat.get("system_key")
    await state.update_data(cat_id=cid, cat_label=cat_label, sys_key=sys_key)

    # 1) Tizimli: 🏦 Kredit to'lovi
    if sys_key == "sys_loan":
        loans = db.q("SELECT * FROM finance_loans WHERE status = 'active' ORDER BY id")
        if not loans:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [btn("➕ Yangi kredit qo'shish", "fn:lnew")],
                [btn("⬅️ Orqaga", "fn:txbackcat")],
            ])
            await _edit_or_send(cb.message, "🏦 Faol kreditlar topilmadi.", kb, state)
            return
        rows = [
            [btn(f"🏦 {ln['title'][:18]} ({fmt_sum(min(ln['monthly_payment'], ln['remaining_debt']))})", f"fn:txln:{ln['id']}")]
            for ln in loans
        ]
        rows.append([btn("⬅️ Orqaga", "fn:txbackcat")])
        await _edit_or_send(cb.message, "🏦 <b>Qaysi kredit uchun to'lov qilamiz?</b>", InlineKeyboardMarkup(inline_keyboard=rows), state)
        return

    # 2) Tizimli: 🏠 Ijara/Kvartira
    if sys_key == "sys_rent":
        sync_recurring_months()
        recs = db.q("SELECT * FROM finance_recurring WHERE status = 'active' ORDER BY id")
        if not recs:
            # Foydalanuvchi oldin ochmagan bo'lsa ham 500 000 so'mlik oylik rejani avto-tayyorlaymiz
            new_rid = create_recurring("Ijara/Kvartira", 500_000, 8, 15)
            recs = db.q("SELECT * FROM finance_recurring WHERE id = ?", (new_rid,))
        if len(recs) == 1:
            await _prompt_sys_recurring_amount(cb.message, state, recs[0])
            return
        rows = [
            [btn(f"🏠 {r['title'][:18]} (qoldi: {fmt_sum(max(0, r['target_amount'] - r['paid_amount_this_month']))})", f"fn:txrc:{r['id']}")]
            for r in recs
        ]
        rows.append([btn("⬅️ Orqaga", "fn:txbackcat")])
        await _edit_or_send(cb.message, "🏠 <b>Qaysi oylik to'lovni yopamiz?</b>", InlineKeyboardMarkup(inline_keyboard=rows), state)
        return

    # 3) Tizimli: 🤝 Qarzni qaytarish (men berishim kerak, amount < 0) yoki 🤝 Qarz qaytishi (menga berishadi, amount > 0)
    if sys_key in ("sys_debt_out", "sys_debt_in"):
        cond = "amount < 0" if sys_key == "sys_debt_out" else "amount > 0"
        debts = db.q(f"SELECT * FROM finance_debts WHERE status = 'active' AND {cond} ORDER BY id")
        if not debts:
            empty_msg = (
                "🤝 Hozir siz qaytarishingiz kerak bo'lgan faol qarz yo'q."
                if sys_key == "sys_debt_out"
                else "🤝 Hozir sizga qaytarilishi kerak bo'lgan faol haq yo'q."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [btn("➕ Yangi qarz / haq", "fn:dnew")],
                [btn("⬅️ Orqaga", "fn:txbackcat")],
            ])
            await _edit_or_send(cb.message, empty_msg, kb, state)
            return
        icon_d = "📤" if sys_key == "sys_debt_out" else "📥"
        rows = [
            [btn(f"{icon_d} {d_r['person_name'][:18]} ({fmt_sum(abs(d_r['amount']))})", f"fn:txdb:{d_r['id']}")]
            for d_r in debts
        ]
        rows.append([btn("⬅️ Orqaga", "fn:txbackcat")])
        hdr = "🤝 <b>Kimga qarz qaytaryapsiz?</b>" if sys_key == "sys_debt_out" else "🤝 <b>Kimdan haq qaytdi?</b>"
        await _edit_or_send(cb.message, hdr, InlineKeyboardMarkup(inline_keyboard=rows), state)
        return

    # 4) Oddiy kategoriya
    await state.set_state(FinFSM.tx_amount)
    await _edit_or_send(
        cb.message,
        f"{cat_label}\n💰 Summani yozing (masalan: <code>45</code> -> 45 000):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "fn:txbackcat")]]),
        state,
    )


@router.callback_query(F.data.startswith("fn:txln:"))
async def cb_tx_pick_loan(cb: CallbackQuery, state: FSMContext):
    lid = int(cb.data.split(":")[2])
    ln = db.q1("SELECT * FROM finance_loans WHERE id = ? AND status = 'active'", (lid,))
    if not ln:
        await cb.answer("Kredit topilmadi")
        return
    await cb.answer()
    std_amt = min(int(ln["monthly_payment"]), int(ln["remaining_debt"]))
    await state.set_state(FinFSM.tx_sys_amount)
    await state.update_data(sys_target_id=lid, sys_std_amt=std_amt)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"✅ Oylik to'lov ({fmt_sum(std_amt)} so'm)", "fn:txsok")],
        [btn("⬅️ Orqaga", "fn:txbackcat")],
    ])
    await _edit_or_send(
        cb.message,
        f"🏦 <b>{app.esc(ln['title'])}</b>\n"
        f"Qoldiq: <b>{fmt_sum(ln['remaining_debt'])} so'm</b> ({ln['remaining_installments']} oy)\n\n"
        "Tugmani bosing yoki to'langan summani yozing:",
        kb,
        state,
    )


async def _prompt_sys_recurring_amount(target_msg, state: FSMContext, rec: dict):
    rem = max(0, int(rec["target_amount"]) - int(rec["paid_amount_this_month"]))
    std_amt = rem if rem > 0 else int(rec["target_amount"])
    await state.set_state(FinFSM.tx_sys_amount)
    await state.update_data(sys_target_id=rec["id"], sys_std_amt=std_amt)
    kb_rows = []
    if rem > 0:
        kb_rows.append([btn(f"✅ Qoldiqni to'lash ({fmt_sum(rem)} so'm)", "fn:txsok")])
    kb_rows.append([btn("⬅️ Orqaga", "fn:txbackcat")])
    await _edit_or_send(
        target_msg,
        f"🏠 <b>{app.esc(rec['title'])}</b>\n"
        f"Shu oy to'langan: <b>{fmt_sum(rec['paid_amount_this_month'])} / {fmt_sum(rec['target_amount'])} so'm</b>\n"
        f"Qolgan: <b>{fmt_sum(rem)} so'm</b>\n\n"
        "To'langan summani yozing yoki tugmani bosing:",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
        state,
    )


@router.callback_query(F.data.startswith("fn:txrc:"))
async def cb_tx_pick_rec(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[2])
    rec = db.q1("SELECT * FROM finance_recurring WHERE id = ?", (rid,))
    if not rec:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await _prompt_sys_recurring_amount(cb.message, state, rec)


@router.callback_query(F.data.startswith("fn:txdb:"))
async def cb_tx_pick_debt(cb: CallbackQuery, state: FSMContext):
    did = int(cb.data.split(":")[2])
    d_row = db.q1("SELECT * FROM finance_debts WHERE id = ? AND status = 'active'", (did,))
    if not d_row:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    full_amt = abs(int(d_row["amount"]))
    await state.set_state(FinFSM.tx_sys_amount)
    await state.update_data(sys_target_id=did, sys_std_amt=full_amt)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"✅ To'liq ({fmt_sum(full_amt)} so'm)", "fn:txsok")],
        [btn("⬅️ Orqaga", "fn:txbackcat")],
    ])
    await _edit_or_send(
        cb.message,
        f"🤝 <b>{app.esc(d_row['person_name'])}</b> — joriy qoldiq: <b>{fmt_sum(full_amt)} so'm</b>\n\n"
        "To'liq yopish tugmasini bosing yoki qisman summani yozing:",
        kb,
        state,
    )


@router.callback_query(FinFSM.tx_sys_amount, F.data == "fn:txsok")
async def cb_tx_sys_std(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Bajarildi!")
    data = await state.get_data()
    await _finish_sys_tx(state, int(data["sys_std_amt"]))


@router.message(FinFSM.tx_sys_amount)
async def msg_tx_sys_amt(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "tx_sys_amount")
    if amt is None or amt == -1:
        return
    await _finish_sys_tx(state, amt)


async def _finish_sys_tx(state: FSMContext, amt: int):
    data = await state.get_data()
    sys_key = data.get("sys_key")
    target_id = int(data["sys_target_id"])
    cat_id = data.get("cat_id")
    cat_label = data.get("cat_label")
    p_mid = data.get("panel_mid")

    tx_id = None
    summary_line = ""
    if sys_key == "sys_loan":
        res = pay_loan(
            target_id,
            pay_amount=amt,
            from_account_id=data.get("from_acc"),
            category_id=cat_id,
            category_label=cat_label,
        )
        tx_id = res.get("tx_id")
        st_note = "🎉 Kredit to'liq yopildi!" if res.get("status") == "paid" else f"Qoldi: {fmt_sum(res['remaining_debt'])} so'm ({res['remaining_installments']} oy)"
        summary_line = f"✅ <b>{app.esc(res.get('title', 'Kredit'))}</b> uchun <b>{fmt_sum(amt)} so'm</b> to'landi.\n{st_note}"
    elif sys_key == "sys_rent":
        res = pay_recurring(
            target_id,
            pay_amount=amt,
            from_account_id=data.get("from_acc"),
            category_id=cat_id,
            category_label=cat_label,
        )
        tx_id = res.get("tx_id")
        st_note = "🎉 Shu oy rejasi to'liq yopildi!" if res.get("closed_this_month") else f"Shu oy qoldi: {fmt_sum(res['remaining_this_month'])} so'm"
        summary_line = f"✅ <b>{app.esc(res.get('title', 'Ijara'))}</b> uchun <b>{fmt_sum(amt)} so'm</b> to'landi.\n{st_note}"
    elif sys_key in ("sys_debt_out", "sys_debt_in"):
        acc_id = data.get("from_acc") if sys_key == "sys_debt_out" else data.get("to_acc")
        res = settle_debt(
            target_id,
            pay_amount=amt,
            account_id=acc_id,
            category_id=cat_id,
            category_label=cat_label,
        )
        tx_id = res.get("tx_id")
        st_note = "🎉 To'liq yopildi!" if res.get("status") == "settled" else f"Qoldiq: {fmt_sum(abs(res['remaining']))} so'm"
        act_w = "ga qaytarildi" if sys_key == "sys_debt_out" else "dan haq olindi"
        summary_line = f"✅ <b>{app.esc(res.get('person', ''))}</b>{act_w}: <b>{fmt_sum( res.get('paid', amt) )} so'm</b>.\n{st_note}"

    await state.clear()
    await _show_tx_confirmation(p_mid, summary_line, tx_id)


@router.callback_query(F.data.startswith("fn:txa2:"))
async def cb_tx_acc2(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.update_data(to_acc=aid)
    await state.set_state(FinFSM.tx_amount)
    await _edit_or_send(
        cb.message,
        "💰 O'tkazma summasini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:home")]]),
        state,
    )


@router.message(FinFSM.tx_amount)
async def msg_tx_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "tx_amount")
    if amt is None or amt == -1:
        return
    await _after_tx_amount(state, amt)


async def _after_tx_amount(state: FSMContext, amt: int):
    await state.update_data(tx_amt=amt)
    await state.set_state(FinFSM.tx_note)
    data = await state.get_data()
    cat_lbl = data.get("cat_label") or "Amaliyot"
    await _edit_or_send(
        None,
        f"{cat_lbl} — <b>{fmt_sum(amt)} so'm</b>\n📝 Izoh yozing yoki o'tkazib yuboring:",
        InlineKeyboardMarkup(inline_keyboard=[
            [btn("⏭ Izohsiz saqlash", "fn:txsave")],
            [btn("⬅️ Bekor", "fn:home")],
        ]),
        state,
    )


@router.callback_query(FinFSM.tx_note, F.data == "fn:txsave")
async def cb_tx_save_no_note(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Saqlandi")
    await _finalize_tx(state, "")


@router.message(FinFSM.tx_note)
async def msg_tx_note(message: Message, state: FSMContext):
    note = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    await _finalize_tx(state, "" if note == "-" else note)


async def _finalize_tx(state: FSMContext, note: str):
    data = await state.get_data()
    mode = data.get("tx_mode", "exp")
    default_cat = {"exp": "➖ Chiqim", "inc": "➕ Kirim", "trf": "🔄 O'tkazma"}.get(mode, "➖ Chiqim")
    cat_label = data.get("cat_label") or default_cat
    amt = int(data["tx_amt"])
    tx_id = record_transaction(
        amt,
        cat_label,
        note=note,
        from_account_id=data.get("from_acc"),
        to_account_id=data.get("to_acc"),
        category_id=data.get("cat_id"),
    )
    p_mid = data.get("panel_mid")
    await state.clear()
    note_s = f"\n📝 Izoh: <i>{app.esc(note)}</i>" if note else ""
    summary_html = f"✅ Saqlandi: <b>{fmt_sum(amt)} so'm</b> ({app.esc(cat_label)}){note_s}"
    await _show_tx_confirmation(p_mid, summary_html, tx_id)


# ------------------------------------------------------------------ 3) Qarz va Haqlar (`finance_debts`)

def _debts_view() -> tuple[str, InlineKeyboardMarkup]:
    rows_db = db.q("SELECT * FROM finance_debts WHERE status = 'active' ORDER BY id")
    lines = ["🤝 <b>Qarzlar va Haqlar</b>\n"]
    kb_rows = []
    if not rows_db:
        lines.append("Faol qarz yoki haq yo'q.")
    for r in rows_db:
        amt = int(r["amount"])
        due_s = f" (muddat: {r['due_date']})" if r.get("due_date") else ""
        if amt > 0:
            lines.append(f"📥 <b>{app.esc(r['person_name'])}</b> menga berishi kerak: <b>{fmt_sum(amt)} so'm</b>{due_s}")
        else:
            lines.append(f"📤 <b>{app.esc(r['person_name'])}</b>ga berishim kerak: <b>{fmt_sum(abs(amt))} so'm</b>{due_s}")
        kb_rows.append([
            btn(f"✅ To'liq: {r['person_name'][:14]}", f"fn:dfull:{r['id']}"),
            btn("💸 Qisman", f"fn:dpart:{r['id']}"),
        ])
    kb_rows.append([btn("➕ Yangi qarz / haq", "fn:dnew"), btn("⬅️ Orqaga", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "fn:debts")
async def cb_debts(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = _debts_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "fn:dnew")
async def cb_debt_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(FinFSM.debt_person)
    await _edit_or_send(
        cb.message,
        "🤝 <b>Yangi qarz yoki haq</b>\n\nShaxs ismini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:debts")]]),
        state,
    )


@router.message(FinFSM.debt_person)
async def msg_debt_person(message: Message, state: FSMContext):
    person = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not person:
        return
    await state.update_data(debt_person=person)
    await state.set_state(FinFSM.debt_dir)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📥 Haq (menga qaytarishadi)", "fn:dd:pos")],
        [btn("📤 Qarz (men qaytaraman)", "fn:dd:neg")],
        [btn("⬅️ Bekor", "fn:debts")],
    ])
    await _edit_or_send(None, f"🤝 <b>{app.esc(person)}</b> — yo'nalishni tanlang:", kb, state)


@router.callback_query(FinFSM.debt_dir, F.data.startswith("fn:dd:"))
async def cb_debt_dir(cb: CallbackQuery, state: FSMContext):
    sign = 1 if cb.data.endswith("pos") else -1
    await cb.answer()
    await state.update_data(debt_sign=sign)
    await state.set_state(FinFSM.debt_amount)
    await _edit_or_send(
        cb.message,
        "💰 Summani yozing (masalan: <code>300</code> -> 300 000):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:debts")]]),
        state,
    )


@router.message(FinFSM.debt_amount)
async def msg_debt_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "debt_amount")
    if amt is None or amt == -1:
        return
    await _after_debt_amount(state, amt)


async def _after_debt_amount(state: FSMContext, amt: int):
    await state.update_data(debt_amt=amt)
    await state.set_state(FinFSM.debt_due)
    await _edit_or_send(
        None,
        "📅 Qaytarish muddatini yozing (<code>2026-11-20</code> yoki <code>20.11.2026</code>), "
        "muddat bo'lmasa <code>-</code>:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⏭ Muddatsiz", "fn:ddskip")], [btn("⬅️ Bekor", "fn:debts")]]),
        state,
    )


@router.callback_query(FinFSM.debt_due, F.data == "fn:ddskip")
async def cb_debt_due_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Saqlandi")
    await _finalize_new_debt(state, None)


@router.message(FinFSM.debt_due)
async def msg_debt_due(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    parsed = _parse_optional_date(raw)
    if parsed is False:
        return
    await _finalize_new_debt(state, parsed)


async def _finalize_new_debt(state: FSMContext, due_iso: str | None):
    data = await state.get_data()
    signed = int(data["debt_amt"]) * int(data["debt_sign"])
    create_debt(data["debt_person"], signed, due_date=due_iso)
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _debts_view()
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data.startswith("fn:dfull:"))
async def cb_debt_full(cb: CallbackQuery):
    did = int(cb.data.split(":")[2])
    settle_debt(did)
    await cb.answer("To'liq yopildi!")
    text, kb = _debts_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("fn:dpart:"))
async def cb_debt_part(cb: CallbackQuery, state: FSMContext):
    did = int(cb.data.split(":")[2])
    d_row = db.q1("SELECT * FROM finance_debts WHERE id = ?", (did,))
    if not d_row:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.set_state(FinFSM.debt_pay_amt)
    await state.update_data(pay_debt_id=did, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        f"💸 <b>{app.esc(d_row['person_name'])}</b> (qoldiq: {fmt_sum(abs(d_row['amount']))} so'm)\n"
        "Qancha qismi yopildi?",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:debts")]]),
        state,
    )


@router.message(FinFSM.debt_pay_amt)
async def msg_debt_part_amt(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "debt_pay_amt")
    if amt is None or amt == -1:
        return
    await _finish_debt_part(state, amt)


async def _finish_debt_part(state: FSMContext, amt: int):
    data = await state.get_data()
    settle_debt(data["pay_debt_id"], pay_amount=amt)
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _debts_view()
    await _edit_direct(p_mid, text, kb)


# ------------------------------------------------------------------ 4) Kreditlar (`finance_loans`) CRUD

def _loans_view() -> tuple[str, InlineKeyboardMarkup]:
    rows_db = db.q("SELECT * FROM finance_loans WHERE status = 'active' ORDER BY id")
    lines = ["🏦 <b>Faol kreditlar</b>\n"]
    kb_rows = []
    if not rows_db:
        lines.append("Faol kreditlar yo'q.")
    for ln in rows_db:
        g_s = f", imtiyoz {ln['grace_deadline_day']}-kun {ln['grace_deadline_time'] or ''}".strip() if ln.get("grace_deadline_day") else ""
        lines.append(
            f"• <b>{app.esc(ln['title'])}</b>\n"
            f"  Qoldiq: <b>{fmt_sum(ln['remaining_debt'])} so'm</b> ({ln['remaining_installments']}/{ln['total_installments']} oy)\n"
            f"  Oylik: {fmt_sum(ln['monthly_payment'])} so'm · Har oy {ln['due_day']}-sana{g_s}"
        )
        kb_rows.append([
            btn(f"✅ Oylik to'lash ({ln['title'][:14]})", f"fn:lpay:{ln['id']}"),
            btn("💸 Boshqa summa", f"fn:lpc:{ln['id']}"),
        ])
    kb_rows.append([btn("➕ Yangi kredit", "fn:lnew"), btn("⬅️ Orqaga", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "fn:loans")
async def cb_loans(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = _loans_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("fn:lpay:"))
async def cb_loan_pay_std(cb: CallbackQuery):
    lid = int(cb.data.split(":")[2])
    res = pay_loan(lid)
    if not res.get("ok"):
        await cb.answer("Allaqachon yopilgan")
        return
    msg = "🎉 Kredit to'liq yopildi!" if res["status"] == "paid" else f"To'landi! Qoldi: {res['remaining_installments']} oy"
    await cb.answer(msg)
    text, kb = _loans_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("fn:lpc:"))
async def cb_loan_pay_custom(cb: CallbackQuery, state: FSMContext):
    lid = int(cb.data.split(":")[2])
    ln = db.q1("SELECT * FROM finance_loans WHERE id = ?", (lid,))
    if not ln:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    await state.set_state(FinFSM.loan_pay_amt)
    await state.update_data(pay_loan_id=lid, panel_mid=cb.message.message_id)
    await _edit_or_send(
        cb.message,
        f"🏦 <b>{app.esc(ln['title'])}</b> — to'langan summani yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_pay_amt)
async def msg_loan_pay_amt(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "loan_pay_amt")
    if amt is None or amt == -1:
        return
    await _finish_loan_pay(state, amt)


async def _finish_loan_pay(state: FSMContext, amt: int):
    data = await state.get_data()
    pay_loan(data["pay_loan_id"], pay_amount=amt)
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _loans_view()
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data == "fn:lnew")
async def cb_loan_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(FinFSM.loan_title)
    await _edit_or_send(
        cb.message,
        "🏦 <b>Yangi kredit qo'shish</b>\n\nKredit nomini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_title)
async def msg_loan_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not title:
        return
    await state.update_data(loan_title=title)
    await state.set_state(FinFSM.loan_total)
    await _edit_or_send(
        None,
        f"🏦 <b>{app.esc(title)}</b>\nUmumiy boshlang'ich kredit summasini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_total)
async def msg_loan_total(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "loan_total")
    if amt is None or amt == -1:
        return
    await _after_loan_total(state, amt)


async def _after_loan_total(state: FSMContext, amt: int):
    await state.update_data(loan_total=amt)
    await state.set_state(FinFSM.loan_rem)
    await _edit_or_send(
        None,
        "💰 Hozirgi qolgan qarz summasini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[
            [btn(f"Bir xil ({fmt_sum(amt)})", "fn:lrem_same")],
            [btn("⬅️ Bekor", "fn:loans")],
        ]),
        state,
    )


@router.callback_query(FinFSM.loan_rem, F.data == "fn:lrem_same")
async def cb_loan_rem_same(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await _after_loan_rem(state, int(data["loan_total"]))


@router.message(FinFSM.loan_rem)
async def msg_loan_rem(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "loan_rem")
    if amt is None or amt == -1:
        return
    await _after_loan_rem(state, amt)


async def _after_loan_rem(state: FSMContext, amt: int):
    await state.update_data(loan_rem=amt)
    await state.set_state(FinFSM.loan_monthly)
    await _edit_or_send(
        None,
        "📅 Oylik to'lov summasini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_monthly)
async def msg_loan_monthly(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "loan_monthly")
    if amt is None or amt == -1:
        return
    await _after_loan_monthly(state, amt)


async def _after_loan_monthly(state: FSMContext, amt: int):
    await state.update_data(loan_monthly=amt)
    await state.set_state(FinFSM.loan_due_day)
    await _edit_or_send(
        None,
        "🗓 Oyning qaysi kunida to'lanadi? (1..31 son yozing):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_due_day)
async def msg_loan_due_day(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not raw.isdigit() or not (1 <= int(raw) <= 31):
        return
    await state.update_data(loan_due_day=int(raw))
    await state.set_state(FinFSM.loan_grace)
    await _edit_or_send(
        None,
        "⏰ Imtiyozli oxirgi muddat bormi? (masalan: <code>8 17:00</code> yoki bo'lmasa <code>-</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⏭ Yo'q", "fn:lgskip")], [btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.callback_query(FinFSM.loan_grace, F.data == "fn:lgskip")
async def cb_loan_grace_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(grace_day=None, grace_time=None)
    await _ask_loan_inst(state)


@router.message(FinFSM.loan_grace)
async def msg_loan_grace(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if raw == "-":
        await state.update_data(grace_day=None, grace_time=None)
    else:
        parts = raw.split()
        if not parts[0].isdigit() or not (1 <= int(parts[0]) <= 31):
            return
        g_time = parts[1] if len(parts) > 1 and ":" in parts[1] else "17:00"
        await state.update_data(grace_day=int(parts[0]), grace_time=g_time)
    await _ask_loan_inst(state)


async def _ask_loan_inst(state: FSMContext):
    await state.set_state(FinFSM.loan_inst)
    await _edit_or_send(
        None,
        "🔢 Jami va qolgan oylar sonini yozing (masalan: <code>12 8</code> — jami 12 oy, qolgani 8 oy; "
        "yoki faqat <code>12</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:loans")]]),
        state,
    )


@router.message(FinFSM.loan_inst)
async def msg_loan_inst(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    parts = raw.split()
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return
    tot_i = int(parts[0])
    rem_i = int(parts[1]) if len(parts) > 1 else tot_i
    if tot_i <= 0 or rem_i <= 0:
        return
    data = await state.get_data()
    create_loan(
        title=data["loan_title"],
        total_debt=int(data["loan_total"]),
        remaining_debt=int(data["loan_rem"]),
        monthly_payment=int(data["loan_monthly"]),
        due_day=int(data["loan_due_day"]),
        total_installments=tot_i,
        remaining_installments=rem_i,
        grace_deadline_day=data.get("grace_day"),
        grace_deadline_time=data.get("grace_time"),
    )
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _loans_view()
    await _edit_direct(p_mid, text, kb)


# ------------------------------------------------------------------ 5) Davriy to'lovlar (`finance_recurring`) CRUD

def _recs_view() -> tuple[str, InlineKeyboardMarkup]:
    sync_recurring_months()
    rows_db = db.q("SELECT * FROM finance_recurring ORDER BY id")
    lines = ["🏠 <b>Davriy oylik to'lovlar</b>\n"]
    kb_rows = []
    if not rows_db:
        lines.append("Davriy to'lovlar yo'q.")
    ym = app.today().strftime("%Y-%m")
    for r in rows_db:
        paid = int(r["paid_amount_this_month"])
        tgt = int(r["target_amount"])
        rem = max(0, tgt - paid)
        done_m = (rem == 0 or r.get("last_paid_month") == ym)
        st_icon = "⏸" if r["status"] == "paused" else ("✅" if done_m else "⏳")
        lines.append(
            f"{st_icon} <b>{app.esc(r['title'])}</b> ({r['due_start_day']}–{r['due_end_day']} sanalar)\n"
            f"  Shu oy: <b>{fmt_sum(paid)} / {fmt_sum(tgt)} so'm</b>"
            + ("" if done_m else f" (qoldi: {fmt_sum(rem)})")
        )
        row_btns = []
        if r["status"] == "active" and not done_m:
            row_btns.append(btn(f"💸 To'lov ({r['title'][:14]})", f"fn:rpay:{r['id']}"))
        row_btns.append(btn("▶️ Yoqish" if r["status"] == "paused" else "⏸ Pauza", f"fn:rtog:{r['id']}"))
        kb_rows.append(row_btns)
    kb_rows.append([btn("➕ Yangi davriy to'lov", "fn:rnew"), btn("⬅️ Orqaga", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "fn:recs")
async def cb_recs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = _recs_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("fn:rtog:"))
async def cb_rec_tog(cb: CallbackQuery):
    rid = int(cb.data.split(":")[2])
    st = toggle_recurring_pause(rid)
    await cb.answer("Pauza qilindi" if st == "paused" else "Faollashtirildi")
    text, kb = _recs_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("fn:rpay:"))
async def cb_rec_pay(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[2])
    sync_recurring_months()
    rec = db.q1("SELECT * FROM finance_recurring WHERE id = ?", (rid,))
    if not rec:
        await cb.answer("Topilmadi")
        return
    rem = max(0, int(rec["target_amount"]) - int(rec["paid_amount_this_month"]))
    await cb.answer()
    await state.set_state(FinFSM.rec_pay_amt)
    await state.update_data(pay_rec_id=rid, panel_mid=cb.message.message_id)
    kb_rows = []
    if rem > 0:
        kb_rows.append([btn(f"✅ To'liq qoldiq ({fmt_sum(rem)})", f"fn:rpfull:{rid}")])
    kb_rows.append([btn("⬅️ Bekor", "fn:recs")])
    await _edit_or_send(
        cb.message,
        f"🏠 <b>{app.esc(rec['title'])}</b>\n"
        f"Shu oy qolgan: <b>{fmt_sum(rem)} so'm</b>\nQisman yoki to'liq to'langan summani yozing:",
        InlineKeyboardMarkup(inline_keyboard=kb_rows),
        state,
    )


@router.callback_query(FinFSM.rec_pay_amt, F.data.startswith("fn:rpfull:"))
async def cb_rec_pay_full(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[2])
    rec = db.q1("SELECT * FROM finance_recurring WHERE id = ?", (rid,))
    if not rec:
        await cb.answer("Topilmadi")
        return
    rem = max(0, int(rec["target_amount"]) - int(rec["paid_amount_this_month"]))
    pay_recurring(rid, rem)
    await state.clear()
    await cb.answer("Shu oy uchun to'liq yopildi!")
    text, kb = _recs_view()
    await _edit_or_send(cb.message, text, kb)


@router.message(FinFSM.rec_pay_amt)
async def msg_rec_pay_amt(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "rec_pay_amt")
    if amt is None or amt == -1:
        return
    await _finish_rec_pay(state, amt)


async def _finish_rec_pay(state: FSMContext, amt: int):
    data = await state.get_data()
    pay_recurring(data["pay_rec_id"], amt)
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _recs_view()
    await _edit_direct(p_mid, text, kb)


@router.callback_query(F.data == "fn:rnew")
async def cb_rec_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(FinFSM.rec_title)
    await _edit_or_send(
        cb.message,
        "🏠 <b>Yangi davriy to'lov</b>\n\nTo'lov nomini yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:recs")]]),
        state,
    )


@router.message(FinFSM.rec_title)
async def msg_rec_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not title:
        return
    await state.update_data(rec_title=title)
    await state.set_state(FinFSM.rec_target)
    await _edit_or_send(
        None,
        f"🏠 <b>{app.esc(title)}</b>\nOylik maqsad summani yozing:",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:recs")]]),
        state,
    )


@router.message(FinFSM.rec_target)
async def msg_rec_target(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    amt = await _handle_amount_step(state, raw, "rec_target")
    if amt is None or amt == -1:
        return
    await _after_rec_target(state, amt)


async def _after_rec_target(state: FSMContext, amt: int):
    await state.update_data(rec_target=amt)
    await state.set_state(FinFSM.rec_window)
    await _edit_or_send(
        None,
        "🗓 Oyning qaysi kunlari oralig'ida to'lanadi? (masalan: <code>8-15</code> yoki bitta kun <code>10</code>):",
        InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Bekor", "fn:recs")]]),
        state,
    )


@router.message(FinFSM.rec_window)
async def msg_rec_window(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace("–", "-")
    await messenger.delete_user_quiet(message.message_id)
    parts = [p.strip() for p in raw.split("-") if p.strip()]
    if not parts or not all(p.isdigit() for p in parts[:2]):
        return
    d1 = int(parts[0])
    d2 = int(parts[1]) if len(parts) > 1 else d1
    if not (1 <= d1 <= 31 and 1 <= d2 <= 31):
        return
    data = await state.get_data()
    create_recurring(data["rec_title"], int(data["rec_target"]), min(d1, d2), max(d1, d2))
    p_mid = data.get("panel_mid")
    await state.clear()
    text, kb = _recs_view()
    await _edit_direct(p_mid, text, kb)


# ------------------------------------------------------------------ Universal Minglik `500` tasdiq handler

@router.callback_query(FinFSM.confirm_amount, F.data.startswith("fn:cf:"))
async def cb_confirm_amount(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":")[2]
    data = await state.get_data()
    if choice == "cancel":
        await cb.answer("Bekor qilindi")
        await state.clear()
        text, kb = home_view()
        await _edit_or_send(cb.message, text, kb)
        return

    amt = int(choice)
    step = data.get("amb_step")
    await cb.answer(f"{fmt_sum(amt)} so'm")

    dispatch = {
        "acc_balance": _finish_acc_balance,
        "acc_limit": _finish_acc_limit,
        "acc_edit_bal": _finish_acc_edit_bal,
        "tx_amount": _after_tx_amount,
        "tx_sys_amount": _finish_sys_tx,
        "debt_amount": _after_debt_amount,
        "debt_pay_amt": _finish_debt_part,
        "loan_total": _after_loan_total,
        "loan_rem": _after_loan_rem,
        "loan_monthly": _after_loan_monthly,
        "loan_pay_amt": _finish_loan_pay,
        "rec_target": _after_rec_target,
        "rec_pay_amt": _finish_rec_pay,
    }
    fn = dispatch.get(step)
    if fn:
        await fn(state, amt)
