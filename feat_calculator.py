"""
Xarajatlar kalkulyatori (3.1 / 3.2):
- 🛒 Bozorlik: mahsulot, miqdor/birlik va narx (`Kartoshka 5kg 25 Men`), kg/litr/dona narx dinamikasi.
- 🍽 Restoran / Choyxona: umumiy + shaxsiy taomlar + % xizmat haqi (har kimning hisobiga mutanosib).
- 🚗 Safar / Umumiy fond: turli ishtirokchilar to'lagan xarajatlarni umumlashtirish.
- ⚡️ Tezkor bo'lish: `450000 / 4` — darhol teng bo'lib ko'rsatish.
- "Minglik" qoidasi (<1000 -> *1000, 400..999 chegara sonlarda tasdiq so'rash).
- 100 so'mgacha yaxlitlash (qoldiq eng ko'p to'laganga) va minimal tranzaksiyali qarz yopish.
"""

import json
import math
import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import feat_common as common
import messenger

router = Router()

TYPE_META = {
    "market": ("🛒", "Bozorlik"),
    "restaurant": ("🍽", "Restoran"),
    "trip": ("🚗", "Safar"),
}
SERVICE_OPTIONS = (0, 10, 12, 15, 20)
AMBIGUOUS_MIN = 400
AMBIGUOUS_MAX = 999

_QUICK_SPLIT_RE = re.compile(r"^\s*(\d[\d\s.,]*[kK]?)\s*/\s*(\d+)\s*$")
_GROUPED_THOUSANDS_RE = re.compile(r"(?<!\S)(\d{1,3})\s+(000)(?!\S)")
_AT_CONSUMERS_RE = re.compile(r"\s+@([^\s]+(?:\s*,\s*[^\s]+)*)\s*$")
_QTY_UNIT_TRAIL_RE = re.compile(
    r"^(.*?)\s+(\d+(?:[.,]\d+)?)\s*(kg|gr|g|litr|ml|l|dona|ta|sht|bog['’`]?|bog)$",
    re.IGNORECASE,
)
_AMOUNT_TOKEN_RE = re.compile(r"^(\d+(?:[.,]\d+)?)([kK]|ming)?$", re.IGNORECASE)


class CalcState(StatesGroup):
    picking_people = State()
    in_session = State()
    picking_consumers = State()
    confirm_amount = State()
    quick_wait = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_sum(n: int | float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def round_100(val: float) -> int:
    return int(math.floor(val / 100.0 + 0.5)) * 100


# ------------------------------------------------------------------ ishtirokchilar va birliklar

def _load_participants(session: dict) -> list[str]:
    raw = (session.get("participants") or "").strip()
    if not raw:
        return ["Men"]
    if raw.startswith("["):
        try:
            arr = [str(x).strip() for x in json.loads(raw) if str(x).strip()]
            return arr or ["Men"]
        except Exception:
            pass
    arr = [x.strip() for x in raw.split(",") if x.strip()]
    return arr or ["Men"]


def _save_participants(session_id: int, people: list[str]):
    clean = []
    seen = set()
    for p in people:
        name = p.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            clean.append(name)
    if not clean:
        clean = ["Men"]
    db.ex("UPDATE sessions SET participants = ? WHERE id = ?", (json.dumps(clean, ensure_ascii=False), session_id))


def known_people(limit: int = 12) -> list[str]:
    names = ["Men"]
    seen = {"men"}
    for row in db.q("SELECT participants FROM sessions ORDER BY id DESC LIMIT 30"):
        for n in _load_participants(row):
            if n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)
                if len(names) >= limit:
                    return names
    for row in db.q("SELECT DISTINCT payer AS n FROM expenses ORDER BY id DESC LIMIT 30"):
        n = (row["n"] or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
            if len(names) >= limit:
                return names
    return names


def _canonical_name(raw_name: str, existing: list[str]) -> str:
    clean = raw_name.strip()
    for e in existing:
        if e.lower() == clean.lower():
            return e
    if clean.lower() == "men":
        return "Men"
    return clean[0].upper() + clean[1:] if clean else "Men"


def normalize_qty_unit(qty: float, unit_raw: str) -> tuple[float, str]:
    u = unit_raw.lower().rstrip("'’`")
    if u in ("g", "gr"):
        return round(qty / 1000.0, 4), "kg"
    if u == "kg":
        return qty, "kg"
    if u == "ml":
        return round(qty / 1000.0, 4), "l"
    if u in ("l", "litr"):
        return qty, "l"
    if u in ("ta", "dona", "sht"):
        return qty, "dona"
    if u == "bog":
        return qty, "bog'"
    return qty, unit_raw.lower()


def fmt_qty(qty: float | None, unit: str | None) -> str:
    if qty is None or not unit:
        return ""
    q_s = str(int(qty)) if float(qty).is_integer() else f"{qty:g}"
    return f"{q_s} {unit}"


# ------------------------------------------------------------------ Minglik qoidasi va qator tahlili

def parse_amount_token(tok: str) -> dict | None:
    """
    Sonni so'mga aylantiradi ('Minglik' qoidasi bilan).
    Qaytaradi: {'amount': int, 'ambiguous': bool, 'raw_num': float} yoki None.
    """
    clean = tok.strip().replace("_", "").replace(" ", "")
    m = _AMOUNT_TOKEN_RE.match(clean)
    if not m:
        return None
    num_s = m.group(1).replace(",", ".")
    suffix = (m.group(2) or "").lower()
    try:
        val = float(num_s)
    except ValueError:
        return None
    if val <= 0:
        return None

    if suffix in ("k", "ming"):
        return {"amount": int(round(val * 1000)), "ambiguous": False, "raw_num": val}

    if val < 1000:
        ambiguous = val.is_integer() and (AMBIGUOUS_MIN <= int(val) <= AMBIGUOUS_MAX)
        return {"amount": int(round(val * 1000)), "ambiguous": ambiguous, "raw_num": val}

    return {"amount": int(round(val)), "ambiguous": False, "raw_num": val}


def parse_expense_line(line: str, participants: list[str], default_payer: str = "Men") -> dict:
    """
    Xarajat qatorini tahlil qiladi:
      - `Kartoshka 5kg 25 Men`
      - `Osh 180 Men`
      - `Kabob 45 Men @Ali` yoki `Salat 30 @Ali,Vali`
      - `Benzin 150 Ali`
    """
    s = (line or "").strip()
    if not s:
        return {"error": "Bo'sh qator."}

    consumers = None
    m_at = _AT_CONSUMERS_RE.search(s)
    if m_at:
        raw_cons = [x.strip() for x in m_at.group(1).split(",") if x.strip()]
        if raw_cons:
            consumers = [_canonical_name(c, participants) for c in raw_cons]
        s = s[:m_at.start()].strip()

    s = _GROUPED_THOUSANDS_RE.sub(r"\1\2", s)
    tokens = s.split()
    if len(tokens) < 2:
        return {
            "error": (
                "Format tushunarsiz. Misollar:\n"
                "• <code>Kartoshka 5kg 25 Men</code>\n"
                "• <code>Osh 180 Men</code>\n"
                "• <code>Kabob 45 Men @Ali</code>"
            )
        }

    # Oxirgi token summa yoki to'lovchi ekanini aniqlash
    amt_info = parse_amount_token(tokens[-1])
    if amt_info is not None:
        payer = _canonical_name(default_payer, participants)
        prefix_tokens = tokens[:-1]
    else:
        if len(tokens) < 3:
            return {"error": "Summani ham yozing, masalan: <code>Osh 180 Men</code>"}
        amt_info = parse_amount_token(tokens[-2])
        if amt_info is None:
            return {"error": "Summa topilmadi. Misol: <code>Kartoshka 5kg 25 Men</code>"}
        payer = _canonical_name(tokens[-1], participants)
        prefix_tokens = tokens[:-2]

    prefix = " ".join(prefix_tokens).strip()
    if not prefix:
        return {"error": "Mahsulot yoki xarajat nomini yozing."}

    qty = None
    unit = None
    title = prefix
    m_qu = _QTY_UNIT_TRAIL_RE.match(prefix)
    if m_qu and m_qu.group(1).strip():
        title = m_qu.group(1).strip()
        qty_raw = float(m_qu.group(2).replace(",", "."))
        if qty_raw > 0:
            qty, unit = normalize_qty_unit(qty_raw, m_qu.group(3))

    return {
        "title": title,
        "amount": amt_info["amount"],
        "ambiguous": amt_info["ambiguous"],
        "raw_num": amt_info["raw_num"],
        "payer": payer,
        "qty": qty,
        "unit": unit,
        "consumers": consumers,
    }


# ------------------------------------------------------------------ saqlash, narx dinamikasi, balans va qarz yopish

def _prev_unit_price(title: str, unit: str, before_id: int) -> int | None:
    row = db.q1(
        "SELECT unit_price FROM expenses WHERE LOWER(title) = LOWER(?) AND unit = ? "
        "AND unit_price IS NOT NULL AND id < ? ORDER BY id DESC LIMIT 1",
        (title.strip(), unit, before_id),
    )
    return int(row["unit_price"]) if row else None


def price_trend_badge(title: str, unit: str | None, unit_price: int | None, expense_id: int) -> str:
    if not unit or unit_price is None:
        return ""
    prev = _prev_unit_price(title, unit, expense_id)
    base = f"{fmt_sum(unit_price)}/{unit}"
    if prev is None or prev <= 0:
        return f"({base})"
    diff = unit_price - prev
    if diff == 0:
        return f"({base}, o'zgarmagan)"
    pct = round(diff / prev * 100)
    if diff > 0:
        return f"({base} 📈 +{pct}%)"
    return f"({base} 📉 {pct}%)"


def recompute_session_splits(session_id: int):
    """Sessiyadagi barcha xarajatlar bo'yicha `expense_splits` jadvalini qayta hisoblaydi."""
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not sess:
        return
    people = _load_participants(sess)
    mult = 1.0 + float(sess["service_pct"] or 0.0) / 100.0
    exps = db.q("SELECT * FROM expenses WHERE session_id = ? ORDER BY id", (session_id,))
    for e in exps:
        if e["is_shared"]:
            consumers = list(people)
        else:
            rows = db.q("SELECT user_name FROM expense_splits WHERE expense_id = ? ORDER BY user_name", (e["id"],))
            consumers = [r["user_name"] for r in rows] or list(people)
        eff = round_100(e["amount"] * mult) if sess["service_pct"] else int(e["amount"])
        n = max(1, len(consumers))
        each = round_100(eff / n) if eff >= 100 else eff // n
        shares = {u: each for u in consumers}
        rem = eff - sum(shares.values())
        anchor = e["payer"] if e["payer"] in shares else consumers[0]
        shares[anchor] += rem
        db.ex("DELETE FROM expense_splits WHERE expense_id = ?", (e["id"],))
        for u, sh in shares.items():
            db.ins(
                "INSERT INTO expense_splits (expense_id, user_name, share_amount) VALUES (?,?,?)",
                (e["id"], u, int(sh)),
            )


def add_expense(
    session_id: int,
    title: str,
    amount: int,
    payer: str,
    qty: float | None = None,
    unit: str | None = None,
    consumers: list[str] | None = None,
) -> int:
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (session_id,))
    people = _load_participants(sess)
    payer = _canonical_name(payer, people)
    changed_people = False
    if payer not in people:
        people.append(payer)
        changed_people = True

    is_shared = 1
    actual_consumers = list(people)
    if consumers:
        norm_c = []
        for c in consumers:
            cn = _canonical_name(c, people)
            if cn not in people:
                people.append(cn)
                changed_people = True
            if cn not in norm_c:
                norm_c.append(cn)
        if norm_c and set(norm_c) != set(people):
            is_shared = 0
            actual_consumers = norm_c
        else:
            actual_consumers = list(people)

    if changed_people:
        _save_participants(session_id, people)
        if is_shared:
            actual_consumers = list(people)

    unit_price = int(round(amount / qty)) if (qty and qty > 0 and unit) else None
    eid = db.ins(
        "INSERT INTO expenses (session_id, title, amount, payer, qty, unit, unit_price, is_shared, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, title.strip(), int(amount), payer, qty, unit, unit_price, is_shared, app.now().isoformat()),
    )
    for u in actual_consumers:
        db.ins("INSERT INTO expense_splits (expense_id, user_name, share_amount) VALUES (?,?,0)", (eid, u))
    recompute_session_splits(session_id)
    return eid


def minimize_transactions(balances: dict[str, int]) -> list[tuple[str, str, int]]:
    """
    `balances` = {ism: paid - share}, yig'indisi 0.
    Qarzlarni minimal tranzaksiyalar soni bilan yopish ro'yxati: [(qarzdor, haqdor, summa), ...].
    """
    debtors = [[u, -bal] for u, bal in balances.items() if bal < 0]
    creditors = [[u, bal] for u, bal in balances.items() if bal > 0]
    transfers: list[tuple[str, str, int]] = []

    while debtors and creditors:
        # 1) Teng summali juftlik bo'lsa bitta o'tkazma bilan ikkala tomon yopiladi
        matched = False
        for di, (du, da) in enumerate(debtors):
            for ci, (cu, ca) in enumerate(creditors):
                if da == ca:
                    transfers.append((du, cu, da))
                    debtors.pop(di)
                    creditors.pop(ci)
                    matched = True
                    break
            if matched:
                break
        if matched:
            continue

        # 2) Eng katta qarzdordan eng katta haqdorga
        debtors.sort(key=lambda x: (-x[1], x[0]))
        creditors.sort(key=lambda x: (-x[1], x[0]))
        du, da = debtors[0]
        cu, ca = creditors[0]
        amt = min(da, ca)
        transfers.append((du, cu, amt))
        if da == amt:
            debtors.pop(0)
        else:
            debtors[0][1] -= amt
        if ca == amt:
            creditors.pop(0)
        else:
            creditors[0][1] -= amt

    return transfers


def compute_settlement(session_id: int) -> dict:
    """
    Sessiya hisob-kitobi:
    - Har bir kishining to'lagani va ulushi (% servis mutanosib qo'shilgan holda)
    - 100 so'mgacha yaxlitlash, yaxlitlash qoldig'i eng ko'p to'lagan ishtirokchiga beriladi
    - Balans = to'lagani - ulushi
    - Minimal tranzaksiyali qarz yopish jadvali
    """
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not sess:
        return {}
    people = _load_participants(sess)
    exps = db.q("SELECT * FROM expenses WHERE session_id = ? ORDER BY id", (session_id,))
    pct = float(sess["service_pct"] or 0.0)
    mult = 1.0 + pct / 100.0

    subtotal = sum(int(e["amount"]) for e in exps)
    exact_paid = {u: 0.0 for u in people}
    exact_share = {u: 0.0 for u in people}

    for e in exps:
        p = e["payer"]
        if p not in exact_paid:
            people.append(p)
            exact_paid[p] = 0.0
            exact_share[p] = 0.0
        eff = float(e["amount"]) * mult
        exact_paid[p] += eff

        if e["is_shared"]:
            cons = list(people)
        else:
            rows = db.q("SELECT user_name FROM expense_splits WHERE expense_id = ?", (e["id"],))
            cons = [r["user_name"] for r in rows] or list(people)
            for c in cons:
                if c not in exact_share:
                    people.append(c)
                    exact_paid[c] = 0.0
                    exact_share[c] = 0.0
        part = eff / max(1, len(cons))
        for c in cons:
            exact_share[c] += part

    if not exps:
        return {
            "people": people,
            "expenses": [],
            "subtotal": 0,
            "service_pct": pct,
            "service_fee": 0,
            "total": 0,
            "paid": {u: 0 for u in people},
            "share": {u: 0 for u in people},
            "balance": {u: 0 for u in people},
            "transfers": [],
        }

    # Agar barcha to'lovlar 100 ga karrali va servis 0% bo'lsa — asl to'lov saqlanadi; aks holda 100 so'mga yaxlitlanadi
    if pct == 0.0 and all(int(round(v)) % 100 == 0 for v in exact_paid.values()):
        paid = {u: int(round(v)) for u, v in exact_paid.items()}
    elif pct == 0.0 and subtotal < 100:
        paid = {u: int(round(v)) for u, v in exact_paid.items()}
    else:
        paid = {u: round_100(v) for u, v in exact_paid.items()}

    total = sum(paid.values())
    service_fee = max(0, total - subtotal)

    # Eng ko'p to'lagan ishtirokchi (teng bo'lsa 'Men' ustuvor)
    top_payer = max(people, key=lambda u: (paid[u], 1 if u == "Men" else 0))

    if total >= 100:
        share = {u: round_100(exact_share[u]) for u in people}
    else:
        share = {u: int(round(exact_share[u])) for u in people}

    delta = total - sum(share.values())
    share[top_payer] += delta

    balance = {u: paid[u] - share[u] for u in people}
    transfers = minimize_transactions(balance)

    return {
        "people": people,
        "expenses": exps,
        "subtotal": subtotal,
        "service_pct": pct,
        "service_fee": service_fee,
        "total": total,
        "paid": paid,
        "share": share,
        "balance": balance,
        "transfers": transfers,
        "top_payer": top_payer,
    }


# ------------------------------------------------------------------ tezkor bo'lish

def parse_quick_split(text: str) -> dict | None:
    m = _QUICK_SPLIT_RE.match(text or "")
    if not m:
        return None
    amt_info = parse_amount_token(m.group(1))
    n = int(m.group(2))
    if amt_info is None or n <= 0 or n > 500:
        return None
    return {
        "amount": amt_info["amount"],
        "ambiguous": amt_info["ambiguous"],
        "raw_num": amt_info["raw_num"],
        "count": n,
    }


def render_quick_split(total: int, count: int) -> str:
    lines = [
        "⚡️ <b>Tezkor bo'lish</b>\n",
        f"💰 Jami: <b>{fmt_sum(total)} so'm</b>",
        f"👥 Odam soni: <b>{count} kishi</b>",
    ]
    if count == 1:
        lines.append(f"👉 <b>{fmt_sum(total)} so'm</b>")
        return "\n".join(lines)

    exact = total / count
    if total % count == 0 and (total // count) % 100 == 0:
        lines.append(f"👉 Har bir kishiga: <b>{fmt_sum(total // count)} so'm</b>")
        return "\n".join(lines)

    if total >= 100 * count:
        r_each = round_100(exact)
        rem = total - r_each * count
        lines.append(f"👉 Har bir kishiga (100 so'mgacha): <b>{fmt_sum(r_each)} so'm</b>")
        if rem != 0:
            adj = r_each + rem
            lines.append(
                f"<i>(To'liq yopilishi uchun: {count - 1} kishi {fmt_sum(r_each)} so'mdan, "
                f"eng ko'p to'lagan 1 kishi ulushi {fmt_sum(adj)} so'm)</i>"
            )
    else:
        lines.append(f"👉 O'rtacha: <b>{exact:.0f} so'm</b>")
    return "\n".join(lines)


# ------------------------------------------------------------------ ekranlar (UI)

def home_text_and_kb() -> tuple[str, InlineKeyboardMarkup]:
    opens = db.q("SELECT * FROM sessions WHERE status = 'open' ORDER BY id DESC LIMIT 5")
    lines = ["🧮 <b>Xarajatlar kalkulyatori</b>\n"]
    rows = []
    if opens:
        lines.append("🟢 <b>Ochiq sessiyalar:</b>")
        for s in opens:
            icon, _ = TYPE_META.get(s["type"], ("📋", s["type"]))
            tot = db.q1("SELECT COALESCE(SUM(amount), 0) AS t FROM expenses WHERE session_id = ?", (s["id"],))["t"]
            mult = 1.0 + float(s["service_pct"] or 0.0) / 100.0
            disp_tot = round_100(tot * mult) if s["service_pct"] else tot
            lines.append(f"• {icon} {app.esc(s['name'])} — {fmt_sum(disp_tot)} so'm")
            rows.append([btn(f"▶️ {icon} {s['name'][:22]} ({fmt_sum(disp_tot)})", f"ca:open:{s['id']}")])
        lines.append("")
    lines.append("Rejimni tanlang yoki to'g'ridan-to'g'ri <code>450000 / 4</code> deb yozing:")
    rows.extend([
        [btn("🛒 Bozorlik", "ca:new:market"), btn("🍽 Restoran / Choyxona", "ca:new:restaurant")],
        [btn("🚗 Safar / Umumiy fond", "ca:new:trip"), btn("⚡️ Tezkor bo'lish", "ca:quick")],
        [btn("📂 Tarix", "ca:hist")],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def render_session_view(session_id: int) -> tuple[str, InlineKeyboardMarkup]:
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not sess:
        return home_text_and_kb()
    icon, type_label = TYPE_META.get(sess["type"], ("📋", sess["type"]))
    st = compute_settlement(session_id)
    people = st["people"]
    exps = st["expenses"]
    closed = sess["status"] == "closed"

    status_badge = " (yakunlangan)" if closed else ""
    lines = [
        f"{icon} <b>{app.esc(sess['name'])}</b>{status_badge}",
        f"👥 Ishtirokchilar ({len(people)}): <b>{', '.join(app.esc(p) for p in people)}</b>",
    ]

    if exps:
        lines.append("")
        for i, e in enumerate(exps, 1):
            q_s = fmt_qty(e["qty"], e["unit"])
            q_part = f" ({app.esc(q_s)})" if q_s else ""
            if sess["type"] == "market":
                trend = price_trend_badge(e["title"], e["unit"], e["unit_price"], e["id"])
                trend_s = f" · {trend}" if trend else ""
                who_s = f" [{app.esc(e['payer'])}]" if len(people) > 1 or e["payer"] != "Men" else ""
                lines.append(f"{i}. {app.esc(e['title'])}{q_part} — <b>{fmt_sum(e['amount'])}</b>{who_s}{trend_s}")
            else:
                scope = ""
                if not e["is_shared"]:
                    c_rows = db.q("SELECT user_name FROM expense_splits WHERE expense_id = ?", (e["id"],))
                    c_names = ", ".join(app.esc(r["user_name"]) for r in c_rows)
                    scope = f" → <i>faqat {c_names}</i>"
                lines.append(
                    f"{i}. {app.esc(e['title'])}{q_part} — <b>{fmt_sum(e['amount'])}</b> ({app.esc(e['payer'])}{scope})"
                )

        lines.append("")
        if sess["type"] == "restaurant" and st["service_pct"] > 0:
            lines.append(
                f"🍽 Taomlar: {fmt_sum(st['subtotal'])} + Xizmat ({st['service_pct']:g}%): {fmt_sum(st['service_fee'])}\n"
                f"💰 <b>Jami hisob: {fmt_sum(st['total'])} so'm</b>"
            )
        else:
            lines.append(f"💰 <b>Jami: {fmt_sum(st['total'])} so'm</b>")

        if len(people) > 1:
            lines.append("\n📊 <b>Balans (to'ladi − ulushi):</b>")
            for u in people:
                b_val = st["balance"][u]
                sign = "+" if b_val > 0 else ("−" if b_val < 0 else "")
                b_str = f"{sign}{fmt_sum(abs(b_val))}" if b_val != 0 else "0"
                lines.append(
                    f"• {app.esc(u)}: {fmt_sum(st['paid'][u])} − {fmt_sum(st['share'][u])} = <b>{b_str}</b>"
                )
            if st["transfers"]:
                lines.append("\n💸 <b>Qarzlarni yopish:</b>")
                for deb, cred, amt in st["transfers"]:
                    lines.append(f"• <b>{app.esc(deb)} → {app.esc(cred)}:</b> {fmt_sum(amt)} so'm")
            else:
                lines.append("\n✅ Hisob-kitob teng, hech kim qarzdor emas.")
    else:
        lines.append("\n<i>Hali xarajat kiritilmagan.</i>")

    if not closed:
        lines.append("")
        if sess["type"] == "market":
            lines.append("✍️ Yozing: <code>Kartoshka 5kg 25 Men</code>")
        elif sess["type"] == "restaurant":
            lines.append("✍️ Umumiy: <code>Osh 180 Men</code>\n✍️ Shaxsiy: <code>Kabob 45 Men @Ali</code>")
        else:
            lines.append("✍️ Yozing: <code>Benzin 150 Men</code>")

    kb_rows = []
    if not closed:
        if sess["type"] == "restaurant":
            sv_row = []
            cur_pct = int(round(st["service_pct"]))
            for p_opt in SERVICE_OPTIONS:
                mark = "• " if cur_pct == p_opt else ""
                lbl = f"{mark}0%" if p_opt == 0 else f"{mark}+{p_opt}%"
                sv_row.append(btn(lbl, f"ca:sv:{session_id}:{p_opt}"))
            kb_rows.append(sv_row)
            kb_rows.append([
                btn("👤 Shaxsiy taom", f"ca:pers:{session_id}"),
                btn("👥 Ishtirokchilar", f"ca:ppl:{session_id}"),
            ])
        else:
            kb_rows.append([btn("👥 Ishtirokchilar", f"ca:ppl:{session_id}")])

        if exps:
            kb_rows.append([
                btn("↩️ Oxirgisini o'chirish", f"ca:undo:{session_id}"),
                btn("✅ Yakunlash", f"ca:close:{session_id}"),
            ])
        else:
            kb_rows.append([btn("🗑 Sessiyani o'chirish", f"ca:del_s:{session_id}")])
    else:
        kb_rows.append([
            btn("🔓 Qayta ochish", f"ca:reopen:{session_id}"),
            btn("🗑 O'chirish", f"ca:del_s:{session_id}"),
        ])

    kb_rows.append([btn("⬅️ Hisob menyu", "ca:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _show_or_update_card(state: FSMContext, session_id: int, fallback_msg=None):
    text, kb = render_session_view(session_id)
    data = await state.get_data()
    card_mid = data.get("card_mid") or (fallback_msg.message_id if fallback_msg else None)
    await state.set_state(CalcState.in_session)
    if card_mid:
        try:
            await app.bot.edit_message_text(
                text, chat_id=app.CHAT_ID, message_id=card_mid, reply_markup=messenger.with_delete(kb)
            )
            db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, card_mid))
            await state.update_data(session_id=session_id, card_mid=card_mid)
            return
        except Exception:
            pass
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    if sent:
        await state.update_data(session_id=session_id, card_mid=sent.message_id)


# ------------------------------------------------------------------ kirish nuqtalari

async def entry(message: Message):
    """Doimiy menyudagi '🧮 Hisob' tugmasi kirish nuqtasi."""
    text, kb = home_text_and_kb()
    await messenger.send(text, "info", kb)


@router.message(Command("hisob"))
async def cmd_calc(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        qs = parse_quick_split(parts[1])
        if qs and not qs["ambiguous"]:
            await messenger.send(render_quick_split(qs["amount"], qs["count"]), "info")
            return
    await state.clear()
    await entry(message)


@router.callback_query(F.data == "ca:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await common.clear_prompts(state)
    await state.clear()
    text, kb = home_text_and_kb()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


# ------------------------------------------------------------------ yangi sessiya va ishtirokchilarni tanlash

def _people_picker_view(mode_type: str, candidates: list[str], selected: list[str], editing_sid: int | None = None):
    icon, label = TYPE_META[mode_type]
    rows, row = [], []
    for idx, name in enumerate(candidates):
        mark = "☑️" if name in selected else "⬜"
        row.append(btn(f"{mark} {name[:18]}", f"ca:pt:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    go_lbl = f"💾 Saqlash ({len(selected)} kishi)" if editing_sid else f"▶️ Boshlash ({len(selected)} kishi)"
    back_cb = f"ca:open:{editing_sid}" if editing_sid else "ca:home"
    rows.append([btn(go_lbl, "ca:go")])
    rows.append([btn("⬅️ Orqaga", back_cb)])
    sel_txt = ", ".join(app.esc(x) for x in selected) if selected else "tanlanmagan"
    text = (
        f"{icon} <b>{label}: ishtirokchilar</b>\n\n"
        f"Tanlangan: <b>{sel_txt}</b>\n\n"
        "Tugmalardan belgilang yoki ismlarni vergul bilan yozib yuboring "
        "(masalan: <code>Men, Ali, Vali</code>):"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ca:new:"))
async def cb_new_session(cb: CallbackQuery, state: FSMContext):
    mode_type = cb.data.split(":")[2]
    await cb.answer()
    await common.clear_prompts(state)
    await state.clear()

    if mode_type == "market":
        # Bozorlik darhol ochiladi (standart: Men; kerak bo'lsa 👥 Ishtirokchilar orqali qo'shiladi)
        name = f"Bozorlik {app.today().strftime('%d.%m')}"
        sid = db.ins(
            "INSERT INTO sessions (name, type, status, service_pct, participants, created_at) VALUES (?,?,?,?,?,?)",
            (name, "market", "open", 0, json.dumps(["Men"]), app.now().isoformat()),
        )
        await state.set_state(CalcState.in_session)
        await state.update_data(session_id=sid, card_mid=cb.message.message_id)
        text, kb = render_session_view(sid)
        await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))
        return

    cands = known_people()
    selected = ["Men"]
    await state.set_state(CalcState.picking_people)
    await state.update_data(
        new_type=mode_type,
        candidates=cands,
        selected=selected,
        editing_sid=None,
        card_mid=cb.message.message_id,
    )
    text, kb = _people_picker_view(mode_type, cands, selected)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:ppl:"))
async def cb_edit_people(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (sid,))
    if not sess:
        await cb.answer("Sessiya topilmadi")
        return
    await cb.answer()
    cur = _load_participants(sess)
    cands = list(cur)
    for kp in known_people():
        if kp.lower() not in {x.lower() for x in cands}:
            cands.append(kp)
    await state.set_state(CalcState.picking_people)
    await state.update_data(
        new_type=sess["type"],
        candidates=cands[:14],
        selected=cur,
        editing_sid=sid,
        card_mid=cb.message.message_id,
    )
    text, kb = _people_picker_view(sess["type"], cands[:14], cur, editing_sid=sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(CalcState.picking_people, F.data.startswith("ca:pt:"))
async def cb_toggle_person(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[2])
    data = await state.get_data()
    cands = data.get("candidates", ["Men"])
    selected = list(data.get("selected", ["Men"]))
    if 0 <= idx < len(cands):
        person = cands[idx]
        if person in selected:
            selected.remove(person)
        else:
            selected.append(person)
    await cb.answer()
    await state.update_data(selected=selected)
    text, kb = _people_picker_view(data["new_type"], cands, selected, data.get("editing_sid"))
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.message(CalcState.picking_people)
async def msg_people_names(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not raw:
        return
    data = await state.get_data()
    custom_title = None
    if ":" in raw:
        left, right = raw.split(":", 1)
        if left.strip() and right.strip():
            custom_title = left.strip()
            raw = right.strip()

    parts = [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]
    if len(parts) == 1 and " " in parts[0]:
        parts = [p.strip() for p in parts[0].split() if p.strip()]

    cands = list(data.get("candidates", ["Men"]))
    selected = list(data.get("selected", ["Men"]))
    for p in parts:
        cn = _canonical_name(p, cands)
        if cn not in cands:
            cands.append(cn)
        if cn not in selected:
            selected.append(cn)
    await state.update_data(candidates=cands, selected=selected, custom_title=custom_title)
    await _finalize_people_pick(state)


@router.callback_query(CalcState.picking_people, F.data == "ca:go")
async def cb_people_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected"):
        await cb.answer("Kamida 1 ishtirokchini tanlang", show_alert=True)
        return
    await cb.answer()
    await _finalize_people_pick(state, fallback_msg=cb.message)


async def _finalize_people_pick(state: FSMContext, fallback_msg=None):
    data = await state.get_data()
    selected = data.get("selected") or ["Men"]
    editing_sid = data.get("editing_sid")
    if editing_sid:
        _save_participants(editing_sid, selected)
        recompute_session_splits(editing_sid)
        await _show_or_update_card(state, editing_sid, fallback_msg=fallback_msg)
        return
    mode_type = data["new_type"]
    _, label = TYPE_META[mode_type]
    name = data.get("custom_title") or f"{label} {app.today().strftime('%d.%m')}"
    sid = db.ins(
        "INSERT INTO sessions (name, type, status, service_pct, participants, created_at) VALUES (?,?,?,?,?,?)",
        (name, mode_type, "open", 0, json.dumps(selected, ensure_ascii=False), app.now().isoformat()),
    )
    await _show_or_update_card(state, sid, fallback_msg=fallback_msg)


# ------------------------------------------------------------------ ochiq sessiya amallari

@router.callback_query(F.data.startswith("ca:open:"))
async def cb_open_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await cb.answer()
    await common.clear_prompts(state)
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (sid,))
    if not sess:
        text, kb = home_text_and_kb()
        await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))
        return
    if sess["status"] == "open":
        await state.set_state(CalcState.in_session)
        await state.update_data(session_id=sid, card_mid=cb.message.message_id, pending_consumers=None)
    else:
        await state.clear()
    text, kb = render_session_view(sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:sv:"))
async def cb_service_pct(cb: CallbackQuery, state: FSMContext):
    _, _, sid_s, pct_s = cb.data.split(":")
    sid, pct = int(sid_s), float(pct_s)
    db.ex("UPDATE sessions SET service_pct = ? WHERE id = ?", (pct, sid))
    recompute_session_splits(sid)
    await cb.answer(f"Xizmat haqi: {pct:g}%")
    await state.set_state(CalcState.in_session)
    await state.update_data(session_id=sid, card_mid=cb.message.message_id)
    text, kb = render_session_view(sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:undo:"))
async def cb_undo_last(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    last = db.q1("SELECT id, title FROM expenses WHERE session_id = ? ORDER BY id DESC LIMIT 1", (sid,))
    if not last:
        await cb.answer("Xarajat yo'q")
        return
    db.ex("DELETE FROM expense_splits WHERE expense_id = ?", (last["id"],))
    db.ex("DELETE FROM expenses WHERE id = ?", (last["id"],))
    await cb.answer(f"O'chirildi: {last['title'][:20]}")
    text, kb = render_session_view(sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:close:"))
async def cb_close_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    db.ex("UPDATE sessions SET status = 'closed' WHERE id = ?", (sid,))
    await cb.answer("Sessiya yakunlandi!")
    await state.clear()
    text, kb = render_session_view(sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:reopen:"))
async def cb_reopen_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    db.ex("UPDATE sessions SET status = 'open' WHERE id = ?", (sid,))
    await cb.answer("Qayta ochildi")
    await state.set_state(CalcState.in_session)
    await state.update_data(session_id=sid, card_mid=cb.message.message_id)
    text, kb = render_session_view(sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data.startswith("ca:del_s:"))
async def cb_delete_session(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    for r in db.q("SELECT id FROM expenses WHERE session_id = ?", (sid,)):
        db.ex("DELETE FROM expense_splits WHERE expense_id = ?", (r["id"],))
    db.ex("DELETE FROM expenses WHERE session_id = ?", (sid,))
    db.ex("DELETE FROM sessions WHERE id = ?", (sid,))
    await cb.answer("Sessiya o'chirildi")
    await state.clear()
    text, kb = home_text_and_kb()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "ca:hist")
async def cb_history(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    rows = db.q("SELECT * FROM sessions ORDER BY id DESC LIMIT 12")
    if not rows:
        await cb.message.edit_text(
            "📂 <b>Sessiyalar tarixi</b>\n\nHali sessiyalar yo'q.",
            reply_markup=messenger.with_delete(InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "ca:home")]])),
        )
        return
    lines = ["📂 <b>Oxirgi sessiyalar:</b>\n"]
    kb_rows = []
    for s in rows:
        icon, _ = TYPE_META.get(s["type"], ("📋", s["type"]))
        tot = db.q1("SELECT COALESCE(SUM(amount), 0) AS t FROM expenses WHERE session_id = ?", (s["id"],))["t"]
        mult = 1.0 + float(s["service_pct"] or 0.0) / 100.0
        disp_tot = round_100(tot * mult) if s["service_pct"] else tot
        st_mark = "🟢" if s["status"] == "open" else "✅"
        lines.append(f"{st_mark} {icon} {app.esc(s['name'])} — <b>{fmt_sum(disp_tot)} so'm</b>")
        kb_rows.append([btn(f"{st_mark} {icon} {s['name'][:22]} ({fmt_sum(disp_tot)})", f"ca:open:{s['id']}")])
    kb_rows.append([btn("⬅️ Orqaga", "ca:home")])
    await cb.message.edit_text("\n".join(lines), reply_markup=messenger.with_delete(InlineKeyboardMarkup(inline_keyboard=kb_rows)))


# ------------------------------------------------------------------ shaxsiy taom (iste'molchi tanlash tugmasi)

def _consumer_picker_view(people: list[str], chosen: list[str], sid: int):
    rows, row = [], []
    for idx, p in enumerate(people):
        mark = "☑️" if p in chosen else "⬜"
        row.append(btn(f"{mark} {p[:18]}", f"ca:pc:{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("✅ Tanlandi", "ca:pok"), btn("⬅️ Orqaga", f"ca:open:{sid}")])
    who = ", ".join(app.esc(x) for x in chosen) if chosen else "tanlanmagan"
    text = (
        f"👤 <b>Shaxsiy taom kimga tegishli?</b>\n\n"
        f"Tanlangan: <b>{who}</b>\n"
        "Belgilab <b>«✅ Tanlandi»</b> bosing, keyin taom va summani yozing (masalan: <code>Kabob 45 Men</code>)."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ca:pers:"))
async def cb_personal_item(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    sess = db.q1("SELECT * FROM sessions WHERE id = ?", (sid,))
    if not sess:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    people = _load_participants(sess)
    chosen = [people[0]]
    await state.set_state(CalcState.picking_consumers)
    await state.update_data(session_id=sid, cons_people=people, cons_chosen=chosen, card_mid=cb.message.message_id)
    text, kb = _consumer_picker_view(people, chosen, sid)
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(CalcState.picking_consumers, F.data.startswith("ca:pc:"))
async def cb_toggle_consumer(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[2])
    data = await state.get_data()
    people = data.get("cons_people", ["Men"])
    chosen = list(data.get("cons_chosen", []))
    if 0 <= idx < len(people):
        p = people[idx]
        if p in chosen:
            chosen.remove(p)
        else:
            chosen.append(p)
    await cb.answer()
    await state.update_data(cons_chosen=chosen)
    text, kb = _consumer_picker_view(people, chosen, data["session_id"])
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(CalcState.picking_consumers, F.data == "ca:pok")
async def cb_confirm_consumers(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    chosen = data.get("cons_chosen") or []
    if not chosen:
        await cb.answer("Kamida 1 kishini tanlang", show_alert=True)
        return
    await cb.answer("Endi taom va narxni yozing")
    sid = data["session_id"]
    await state.set_state(CalcState.in_session)
    await state.update_data(pending_consumers=chosen)
    text, kb = render_session_view(sid)
    who = ", ".join(app.esc(x) for x in chosen)
    text += f"\n\n🎯 Keyingi kiritiladigan taom faqat: <b>{who}</b> uchun yoziladi."
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


# ------------------------------------------------------------------ xarajat yozish va chegara (500) tasdig'i

def _ambiguous_kb(raw_num: int) -> InlineKeyboardMarkup:
    rows = [[btn(f"✅ {fmt_sum(raw_num * 1000)} so'm", f"ca:cf:{raw_num * 1000}")]]
    if raw_num % 10 == 0:
        rows.append([btn(f"{fmt_sum(raw_num * 100)} so'm", f"ca:cf:{raw_num * 100}")])
    rows.append([
        btn(f"{fmt_sum(raw_num)} so'm", f"ca:cf:{raw_num}"),
        btn("❌ Bekor", "ca:cf:cancel"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CalcState.in_session)
async def msg_in_session(message: Message, state: FSMContext):
    raw_text = (message.text or "").strip()
    if not raw_text:
        return

    # Sessiya ichida ham `450000 / 4` yozilsa tezkor bo'lib ko'rsatadi
    qs = parse_quick_split(raw_text)
    if qs and not qs["ambiguous"]:
        await messenger.send(render_quick_split(qs["amount"], qs["count"]), "info")
        return

    data = await state.get_data()
    sid = data.get("session_id")
    sess = db.q1("SELECT * FROM sessions WHERE id = ? AND status = 'open'", (sid,)) if sid else None
    if not sess:
        await state.clear()
        await entry(message)
        return

    people = _load_participants(sess)
    preset_consumers = data.get("pending_consumers")
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]

    parsed_items = []
    for ln in lines:
        p = parse_expense_line(ln, people)
        if "error" in p:
            await messenger.send("⚠️ " + p["error"], "confirm")
            return
        if p["consumers"] is None and preset_consumers:
            p["consumers"] = preset_consumers
        parsed_items.append(p)

    await messenger.delete_user_quiet(message.message_id)
    await state.update_data(pending_consumers=None)

    # Agar 500 kabi chegara son bo'lsa — saqlashdan oldin tasdiq so'raladi
    amb_idx = next((i for i, it in enumerate(parsed_items) if it["ambiguous"]), None)
    if amb_idx is not None:
        # Undan oldingi aniq qatorlarni saqlab qo'yamiz
        for it in parsed_items[:amb_idx]:
            add_expense(sid, it["title"], it["amount"], it["payer"], it["qty"], it["unit"], it["consumers"])
        target = parsed_items[amb_idx]
        rest = parsed_items[amb_idx + 1:]
        raw_int = int(round(target["raw_num"]))
        prompt = await messenger.send(
            f"🤔 <b>{app.esc(target['title'])}</b> uchun <code>{raw_int}</code> kiritildi.\n"
            "Qaysi summani nazarda tutdingiz?",
            "flow",
            _ambiguous_kb(raw_int),
            actionable=True,
            bind_trigger=False,
        )
        await state.set_state(CalcState.confirm_amount)
        await state.update_data(
            amb_mode="expense",
            amb_item=target,
            amb_rest=rest,
            amb_prompt_mid=prompt.message_id if prompt else None,
        )
        return

    for it in parsed_items:
        add_expense(sid, it["title"], it["amount"], it["payer"], it["qty"], it["unit"], it["consumers"])
    await _show_or_update_card(state, sid)


@router.callback_query(CalcState.confirm_amount, F.data.startswith("ca:cf:"))
async def cb_confirm_amount(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":")[2]
    data = await state.get_data()
    prompt_mid = data.get("amb_prompt_mid") or cb.message.message_id

    if choice == "cancel":
        await cb.answer("Bekor qilindi")
        await messenger.delete_quiet(prompt_mid)
        if data.get("amb_mode") == "expense" and data.get("session_id"):
            await _show_or_update_card(state, data["session_id"])
        else:
            await state.clear()
        return

    chosen_amount = int(choice)
    await cb.answer(f"{fmt_sum(chosen_amount)} so'm")
    await messenger.delete_quiet(prompt_mid)

    if data.get("amb_mode") == "quick":
        cnt = int(data["amb_count"])
        await state.clear()
        await messenger.send(render_quick_split(chosen_amount, cnt), "info", bind_trigger=False)
        return

    sid = data["session_id"]
    it = data["amb_item"]
    add_expense(sid, it["title"], chosen_amount, it["payer"], it["qty"], it["unit"], it["consumers"])
    for r_it in data.get("amb_rest", []):
        add_expense(sid, r_it["title"], r_it["amount"], r_it["payer"], r_it["qty"], r_it["unit"], r_it["consumers"])
    await _show_or_update_card(state, sid)


# ------------------------------------------------------------------ ⚡️ Tezkor bo'lish (tugma orqali va har joydan)

@router.callback_query(F.data == "ca:quick")
async def cb_quick_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(CalcState.quick_wait)
    await common.ask(
        state,
        "⚡️ <b>Tezkor bo'lish</b>\n\nSumma va odam sonini yozing:\n"
        "• <code>450000 / 4</code>\n• <code>180 / 3</code> (180 000 / 3)",
    )


@router.message(CalcState.quick_wait)
async def msg_quick_wait(message: Message, state: FSMContext):
    await common.track_user(state, message)
    qs = parse_quick_split(message.text or "")
    if not qs:
        await messenger.send("Format: <code>450000 / 4</code>", "confirm", bind_trigger=False)
        return
    await common.clear_prompts(state)
    if qs["ambiguous"]:
        raw_int = int(round(qs["raw_num"]))
        prompt = await messenger.send(
            f"🤔 <code>{raw_int} / {qs['count']}</code> kiritildi. Qaysi summani bo'lamiz?",
            "flow",
            _ambiguous_kb(raw_int),
            actionable=True,
            bind_trigger=False,
        )
        await state.set_state(CalcState.confirm_amount)
        await state.update_data(
            amb_mode="quick",
            amb_count=qs["count"],
            amb_prompt_mid=prompt.message_id if prompt else None,
        )
        return
    await state.clear()
    await messenger.send(render_quick_split(qs["amount"], qs["count"]), "info", bind_trigger=False)


@router.message(StateFilter(default_state), F.text.regexp(r"^\s*\d[\d\s.,]*[kK]?\s*/\s*\d+\s*$"))
async def msg_quick_anywhere(message: Message, state: FSMContext):
    qs = parse_quick_split(message.text or "")
    if not qs:
        return
    if qs["ambiguous"]:
        raw_int = int(round(qs["raw_num"]))
        prompt = await messenger.send(
            f"🤔 <code>{raw_int} / {qs['count']}</code> kiritildi. Qaysi summani bo'lamiz?",
            "flow",
            _ambiguous_kb(raw_int),
            actionable=True,
        )
        await state.set_state(CalcState.confirm_amount)
        await state.update_data(
            amb_mode="quick",
            amb_count=qs["count"],
            amb_prompt_mid=prompt.message_id if prompt else None,
        )
        return
    await messenger.send(render_quick_split(qs["amount"], qs["count"]), "info")
