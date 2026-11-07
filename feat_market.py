"""
Smart Bozorlik, Dinamik Savat va Inflyatsiya Monitoringi (3.6 — feat_market.py):
- 0% Hardcoding: do'kon/bozor nomlari (`market_locations`) va mahsulotlar (`market_items`) faqat
  foydalanuvchi kiritishlari asosida dinamik yaratiladi va o'rganiladi.
- Ikki xil rejim (Dual Basket Flow):
  1) «📝 Rejalashtirilgan ro'yxat» (`mode='planned'`) — narxsiz taxminiy ro'yxat, balansga ta'sir qilmaydi,
     bozorda bitta tugma bilan «⚡️ Ayni damdagi xarid» rejimiga o'tkaziladi.
  2) «⚡️ Ayni damdagi xarid» (`mode='active'`) — narxlar, `#joy` teglari, `[✅]` belgilash,
     `[➕ Yangi narsa qo'shish]` va `[➖ O'chirish]` In-place boshqaruvi.
- Smart Parser (Minglik `< 1000 -> * 1000`, `unit_price = total_price / quantity`, `#teg` -> `market_locations`)
  hamda Anti-Cheat (`unit_price >= last_unit_price * 1.35` bo'lganda `[✅ Narx to'g'ri]` / `[✏️ Qayta kiritish]`).
- Moliya integratsiyasi (`feat_finance.py`): «🏁 Xaridni yakunlash» bosilganda faqat `is_bought = 1` mahsulotlar
  summasi tanlangan hisobdan yechilib, `finance_transactions` ga yagona yozuv bo'lib tushadi, `market_logs` da
  inflyatsiya foizi (`price_diff_percent`) qayd etiladi va `market_items` yangilanadi.
- `/narx <mahsulot>` — savdo joylari (`#teg`) bo'yicha narxlarni solishtirib, eng arzon joyni ajratib ko'rsatadi.
"""

import re
import uuid
from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import messenger

router = Router()

ANTI_CHEAT_RATIO = 1.35

_APOSTROPHES_RE = re.compile(r"[’‘`ʻʼ]")
_SPACE_THOUSANDS_RE = re.compile(
    r"(\b(?:kg|dona|litr|metr|\d+(?:[.,]\d+)?)\s+\d{1,3})\s+(000)(?=\s|$)",
    re.IGNORECASE,
)

# Asosiy Smart Parser regex (4.1-bo'lim talabi + kirill/lotin apostroflari):
SMART_ITEM_RE = re.compile(
    r"^(?P<item>[a-zA-Zа-яА-ЯёЁўқғҳЎҚҒҲo'g'shchO'G'SHCH\s'-]+?)\s+"
    r"(?P<qty>\d+(?:[\.,]\d+)?)\s*"
    r"(?P<unit>kg|dona|litr|metr)?"
    r"(?:\s+(?P<price>\d+(?:[\.,]\d+)?))?"
    r"(?:\s+#(?P<loc>[a-zA-Zа-яА-ЯёЁўқғҳЎҚҒҲ0-9_]+))?$",
    re.IGNORECASE,
)

# Rejalashtirilgan mahsulotga bozorda faqat narx (+ ixtiyoriy #joy) kiritish uchun:
PRICE_ONLY_RE = re.compile(
    r"^\s*(?P<price>\d+(?:[\.,]\d+)?)(?:\s+#(?P<loc>[a-zA-Zа-яА-ЯёЁўқғҳЎҚҒҲ0-9_]+))?\s*$"
)


class MarketFSM(StatesGroup):
    adding_items = State()
    anti_cheat = State()
    entering_price = State()
    setting_location = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_sum(n: int | float) -> str:
    val = int(round(float(n)))
    sign = "−" if val < 0 else ""
    return f"{sign}{abs(val):,}".replace(",", " ")


def fmt_qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{float(q):g}"


def _norm_apostrophes(s: str) -> str:
    return _APOSTROPHES_RE.sub("'", s or "")


def normalize_item_name(raw: str) -> str:
    cleaned = " ".join(_norm_apostrophes(raw).strip().split())
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:]


def normalize_location_name(raw: str) -> str:
    cleaned = _norm_apostrophes(raw).strip().lstrip("#").strip().lower()
    return re.sub(r"\s+", "_", cleaned)


# ------------------------------------------------------------------ Dinamik Joy va Mahsulot (0% Hardcode)

def get_or_create_location(raw_loc: str | None) -> int | None:
    if not raw_loc:
        return None
    loc_name = normalize_location_name(raw_loc)
    if not loc_name:
        return None
    row = db.q1("SELECT id FROM market_locations WHERE LOWER(name) = LOWER(?)", (loc_name,))
    if row:
        return int(row["id"])
    return db.ins(
        "INSERT INTO market_locations (name, created_at) VALUES (?, ?)",
        (loc_name, app.now().isoformat()),
    )


def get_or_create_item(raw_name: str, unit: str | None = None) -> dict:
    norm = normalize_item_name(raw_name)
    row = db.q1("SELECT * FROM market_items WHERE LOWER(name) = LOWER(?)", (norm,))
    if row:
        return row
    def_unit = (unit or "kg").lower()
    iid = db.ins(
        "INSERT INTO market_items (name, default_unit, last_unit_price, updated_at) VALUES (?, ?, 0, ?)",
        (norm, def_unit, app.now().isoformat()),
    )
    return db.q1("SELECT * FROM market_items WHERE id = ?", (iid,))


# ------------------------------------------------------------------ Smart Parser va Anti-Cheat

def normalize_market_price(raw_price: float) -> float:
    """Narx kiritilsa va `< 1000` bo'lsa `1000` ga ko'paytiriladi (`25 -> 25 000`)."""
    if raw_price <= 0:
        return 0.0
    if raw_price < 1000:
        return round(raw_price * 1000.0, 2)
    return round(raw_price, 2)


def parse_market_line(raw_line: str) -> dict | None:
    """
    Matn qatorini Smart Parser orqali tahlil qiladi.
    Misollar:
    - `Kartoshka 5 kg 25 #chorsu` -> qty=5, unit='kg', total_price=25000, unit_price=5000, location='chorsu'
    - `Non 4 dona` -> qty=4, unit='dona', total_price=0, unit_price=0, location=None
    """
    s = _norm_apostrophes(raw_line).strip()
    if not s:
        return None
    s = _SPACE_THOUSANDS_RE.sub(r"\1\2", s)
    m = SMART_ITEM_RE.match(s)
    if not m:
        return None
    item_name = normalize_item_name(m.group("item"))
    if not item_name:
        return None
    try:
        qty = float(m.group("qty").replace(",", "."))
    except ValueError:
        return None
    if qty <= 0:
        return None

    raw_unit = m.group("unit")
    unit = raw_unit.lower() if raw_unit else None

    raw_price_s = m.group("price")
    if raw_price_s is not None:
        try:
            raw_p = float(raw_price_s.replace(",", "."))
        except ValueError:
            return None
        if raw_p < 0:
            return None
        total_price = normalize_market_price(raw_p)
        unit_price = round(total_price / qty, 2) if total_price > 0 else 0.0
    else:
        total_price = 0.0
        unit_price = 0.0

    raw_loc = m.group("loc")
    loc = normalize_location_name(raw_loc) if raw_loc else None

    return {
        "item_name": item_name,
        "quantity": qty,
        "unit": unit,
        "unit_price": unit_price,
        "total_price": total_price,
        "location": loc,
    }


def check_anti_cheat(item_id: int, unit_price: float) -> dict:
    """
    Agar `last_unit_price > 0` va `unit_price >= last_unit_price * 1.35` (+35% va undan ko'p) bo'lsa,
    Anti-Cheat ogohlantirishini qaytaradi.
    """
    if unit_price <= 0:
        return {"triggered": False}
    item = db.q1("SELECT * FROM market_items WHERE id = ?", (item_id,))
    if not item:
        return {"triggered": False}
    prev = float(item.get("last_unit_price") or 0)
    if prev <= 0:
        return {"triggered": False}
    if unit_price + 1e-9 >= prev * ANTI_CHEAT_RATIO:
        diff_pct = round(((unit_price - prev) / prev) * 100.0, 1)
        return {
            "triggered": True,
            "item_name": item["name"],
            "last_unit_price": prev,
            "unit_price": unit_price,
            "diff_pct": diff_pct,
        }
    return {"triggered": False}


# ------------------------------------------------------------------ Savat CRUD va Checkout Biznes Mantiqi

def create_basket(mode: str, title: str | None = None, location_name: str | None = None) -> str:
    if mode not in ("planned", "active"):
        raise ValueError("Savat rejimi 'planned' yoki 'active' bo'lishi kerak")
    bid = str(uuid.uuid4())
    loc_id = get_or_create_location(location_name)
    default_title = (
        f"Reja ({app.today().strftime('%d.%m')})"
        if mode == "planned"
        else f"Bozorlik ({app.now().strftime('%d.%m %H:%M')})"
    )
    db.ex(
        "INSERT INTO market_baskets (id, title, mode, location_id, status, created_at) VALUES (?,?,?,?,?,?)",
        (bid, (title or default_title).strip(), mode, loc_id, "draft", app.now().isoformat()),
    )
    return bid


def switch_basket_to_active(basket_id: str):
    db.ex("UPDATE market_baskets SET mode = 'active' WHERE id = ? AND status = 'draft'", (basket_id,))


def add_basket_item(
    basket_id: str,
    item_name: str,
    quantity: float,
    unit: str | None = None,
    total_price: float = 0.0,
    location_name: str | None = None,
    force: bool = False,
) -> dict:
    """
    Savatga mahsulot qo'shadi.
    - Agar `force=False` bo'lsa va Anti-Cheat (+35%) ishga tushsa, bazaga yozmasdan
      `{'ok': False, 'anti_cheat': True, ...}` qaytaradi.
    """
    basket = db.q1("SELECT * FROM market_baskets WHERE id = ?", (basket_id,))
    if not basket or basket["status"] != "draft":
        return {"ok": False, "error": "Savat topilmadi yoki yopilgan."}

    item = get_or_create_item(item_name, unit)
    eff_unit = (unit or item.get("default_unit") or "kg").lower()
    tot = round(float(total_price or 0.0), 2)
    u_price = round(tot / float(quantity), 2) if (tot > 0 and quantity > 0) else 0.0

    if not force and u_price > 0:
        ac = check_anti_cheat(item["id"], u_price)
        if ac["triggered"]:
            return {
                "ok": False,
                "anti_cheat": True,
                "details": ac,
                "pending": {
                    "kind": "new_item",
                    "basket_id": basket_id,
                    "item_name": item["name"],
                    "quantity": float(quantity),
                    "unit": eff_unit,
                    "total_price": tot,
                    "unit_price": u_price,
                    "location": location_name,
                },
            }

    loc_id = get_or_create_location(location_name)
    if loc_id and not basket.get("location_id"):
        db.ex("UPDATE market_baskets SET location_id = ? WHERE id = ?", (loc_id, basket_id))

    #Planned rejimda balansga va xarid holatiga tegmaydi (is_bought=0); active rejimda narxi bo'lsa is_bought=1
    is_bought = 1 if (basket["mode"] == "active" and tot > 0) else 0

    bitem_id = db.ins(
        "INSERT INTO market_basket_items "
        "(basket_id, item_id, location_id, quantity, unit, unit_price, total_price, is_bought) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (basket_id, item["id"], loc_id, float(quantity), eff_unit, u_price, tot, is_bought),
    )
    return {"ok": True, "basket_item_id": bitem_id, "item_id": item["id"], "unit_price": u_price, "total_price": tot}


def set_basket_item_price(
    bitem_id: int,
    total_price: float,
    location_name: str | None = None,
    force: bool = False,
) -> dict:
    """Savatdagi mavjud (masalan rejalashtirilgan) mahsulotga narx yozadi va `is_bought = 1` qiladi."""
    bitem = db.q1(
        "SELECT bi.*, mi.name AS item_name FROM market_basket_items bi "
        "JOIN market_items mi ON mi.id = bi.item_id WHERE bi.id = ?",
        (bitem_id,),
    )
    if not bitem:
        return {"ok": False, "error": "Mahsulot topilmadi."}
    qty = float(bitem["quantity"])
    tot = normalize_market_price(float(total_price))
    u_price = round(tot / qty, 2) if (tot > 0 and qty > 0) else 0.0

    if not force and u_price > 0:
        ac = check_anti_cheat(int(bitem["item_id"]), u_price)
        if ac["triggered"]:
            return {
                "ok": False,
                "anti_cheat": True,
                "details": ac,
                "pending": {
                    "kind": "set_price",
                    "bitem_id": bitem_id,
                    "basket_id": bitem["basket_id"],
                    "item_name": bitem["item_name"],
                    "quantity": qty,
                    "unit": bitem["unit"],
                    "total_price": tot,
                    "unit_price": u_price,
                    "location": location_name,
                },
            }

    loc_id = get_or_create_location(location_name)
    if loc_id:
        db.ex("UPDATE market_basket_items SET location_id = ? WHERE id = ?", (loc_id, bitem_id))
        basket = db.q1("SELECT location_id FROM market_baskets WHERE id = ?", (bitem["basket_id"],))
        if basket and not basket.get("location_id"):
            db.ex("UPDATE market_baskets SET location_id = ? WHERE id = ?", (loc_id, bitem["basket_id"]))

    db.ex(
        "UPDATE market_basket_items SET unit_price = ?, total_price = ?, is_bought = 1 WHERE id = ?",
        (u_price, tot, bitem_id),
    )
    return {"ok": True, "basket_id": bitem["basket_id"], "unit_price": u_price, "total_price": tot}


def remove_basket_item(bitem_id: int) -> str | None:
    row = db.q1("SELECT basket_id FROM market_basket_items WHERE id = ?", (bitem_id,))
    if not row:
        return None
    db.ex("DELETE FROM market_basket_items WHERE id = ?", (bitem_id,))
    return row["basket_id"]


def checkout_basket(basket_id: str, account_id: int) -> dict:
    """
    «🏁 Xaridni yakunlash»:
    1) Faqat olingan (`is_bought = 1`) mahsulotlarning jami summasini hisoblaydi;
    2) `feat_finance.record_transaction` orqali tanlangan hisobdan summani yechadi va
       `finance_transactions` ga yagona yozuv (`Oziq-ovqat / Bozorlik`) qo'shadi;
    3) `market_logs` ga inflyatsiya dinamikasi (`price_diff_percent`) bilan yozadi;
    4) `market_items` dagi oxirgi narxlarni yangilaydi va `market_baskets.status = 'completed'` qiladi.
    """
    basket = db.q1(
        "SELECT b.*, l.name AS loc_name FROM market_baskets b "
        "LEFT JOIN market_locations l ON l.id = b.location_id WHERE b.id = ?",
        (basket_id,),
    )
    if not basket or basket["status"] != "draft":
        return {"ok": False, "error": "Savat topilmadi yoki allaqachon yakunlangan."}

    acc = db.q1("SELECT * FROM finance_accounts WHERE id = ? AND is_active = 1", (account_id,))
    if not acc:
        return {"ok": False, "error": "Tanlangan hisob topilmadi."}

    bought = db.q(
        "SELECT bi.*, mi.name AS item_name, mi.last_unit_price, mi.last_location_id, mi.last_bought_date, "
        "ml.name AS item_loc_name "
        "FROM market_basket_items bi "
        "JOIN market_items mi ON mi.id = bi.item_id "
        "LEFT JOIN market_locations ml ON ml.id = bi.location_id "
        "WHERE bi.basket_id = ? AND bi.is_bought = 1 ORDER BY bi.id",
        (basket_id,),
    )
    if not bought:
        return {"ok": False, "error": "Savatda olingan ([✅]) mahsulotlar yo'q."}

    missing_price = [r["item_name"] for r in bought if float(r["total_price"] or 0) <= 0]
    if missing_price:
        return {"ok": False, "error": f"Narxi kiritilmagan mahsulot bor: {', '.join(missing_price)}"}

    total_amount = int(round(sum(float(r["total_price"]) for r in bought)))
    if total_amount <= 0:
        return {"ok": False, "error": "Jami summa 0 so'm bo'lishi mumkin emas."}

    # Joy nomini aniqlash (savatdan yoki olingan mahsulotlardan)
    loc_name = basket.get("loc_name") or next((r["item_loc_name"] for r in bought if r.get("item_loc_name")), None)
    bought_names = ", ".join(f"{r['item_name']} ({fmt_qty(r['quantity'])} {r['unit']})" for r in bought)
    note = f"Bozorlik (#{loc_name}): {bought_names}" if loc_name else f"Bozorlik: {bought_names}"

    # Moliya kategoriyasini bog'lash
    cat_row = db.q1(
        "SELECT id, name, icon FROM finance_categories "
        "WHERE type = 'expense' AND is_active = 1 AND LOWER(name) LIKE '%bozorlik%' ORDER BY id LIMIT 1"
    )
    cat_id = int(cat_row["id"]) if cat_row else None
    cat_label = "Oziq-ovqat / Bozorlik"

    prev_items_snapshot = []
    seen_item_ids = set()
    for r in bought:
        iid = int(r["item_id"])
        if iid not in seen_item_ids:
            seen_item_ids.add(iid)
            prev_items_snapshot.append({
                "id": iid,
                "last_unit_price": r["last_unit_price"],
                "last_location_id": r["last_location_id"],
                "last_bought_date": r["last_bought_date"],
            })

    import feat_finance as fin

    tx_id = fin.record_transaction(
        total_amount,
        cat_label,
        note=note,
        from_account_id=account_id,
        category_id=cat_id,
        linked_kind="market",
        linked_id=1,
        undo_meta={"basket_id": basket_id, "prev_items": prev_items_snapshot},
    )

    now_iso = app.now().isoformat()
    today_iso = app.today().isoformat()
    inflation_rows = []

    for r in bought:
        iid = int(r["item_id"])
        cur_item = db.q1("SELECT last_unit_price FROM market_items WHERE id = ?", (iid,))
        prev_u = float(cur_item["last_unit_price"] or 0) if cur_item else 0.0
        cur_u = float(r["unit_price"])
        if prev_u > 0:
            diff_pct = round(((cur_u - prev_u) / prev_u) * 100.0, 2)
        else:
            diff_pct = 0.0

        eff_loc_id = r.get("location_id") or basket.get("location_id")
        db.ins(
            "INSERT INTO market_logs "
            "(basket_id, item_id, location_id, quantity, unit, unit_price, total_price, price_diff_percent, bought_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (basket_id, iid, eff_loc_id, float(r["quantity"]), r["unit"], cur_u, float(r["total_price"]), diff_pct, now_iso),
        )
        db.ex(
            "UPDATE market_items SET default_unit = ?, last_unit_price = ?, "
            "last_location_id = COALESCE(?, last_location_id), last_bought_date = ?, updated_at = ? WHERE id = ?",
            (r["unit"], cur_u, eff_loc_id, today_iso, now_iso, iid),
        )
        inflation_rows.append({
            "name": r["item_name"],
            "quantity": float(r["quantity"]),
            "unit": r["unit"],
            "unit_price": cur_u,
            "total_price": float(r["total_price"]),
            "prev_unit_price": prev_u,
            "price_diff_percent": diff_pct,
        })

    db.ex("UPDATE market_baskets SET status = 'completed' WHERE id = ?", (basket_id,))
    return {
        "ok": True,
        "tx_id": tx_id,
        "total_amount": total_amount,
        "account_name": acc["name"],
        "location": loc_name,
        "note": note,
        "items": inflation_rows,
    }


# ------------------------------------------------------------------ `/narx <mahsulot>` Dinamik Joy Tahlili

def analyze_item_prices(query_name: str) -> dict:
    """
    Mahsulotning bazadagi joylar (`#teg`) bo'yicha narxlarini taqqoslab,
    eng arzon savdo joyini va narx dinamikasini qaytaradi.
    """
    q_norm = _norm_apostrophes(query_name).strip()
    if not q_norm:
        return {"found": False, "query": ""}

    item = db.q1("SELECT * FROM market_items WHERE LOWER(name) = LOWER(?)", (q_norm,))
    if not item:
        item = db.q1(
            "SELECT * FROM market_items WHERE LOWER(name) LIKE LOWER(?) ORDER BY updated_at DESC, id DESC LIMIT 1",
            (f"%{q_norm}%",),
        )
    if not item:
        return {"found": False, "query": q_norm}

    logs = db.q(
        "SELECT ml.*, loc.name AS loc_name "
        "FROM market_logs ml "
        "LEFT JOIN market_locations loc ON loc.id = ml.location_id "
        "WHERE ml.item_id = ? ORDER BY ml.id DESC",
        (item["id"],),
    )

    by_loc: dict[str, dict] = {}
    for lg in logs:
        loc_key = lg["loc_name"] or ""
        if not loc_key:
            continue
        u_p = float(lg["unit_price"])
        if loc_key not in by_loc:
            by_loc[loc_key] = {
                "location": loc_key,
                "latest_unit_price": u_p,
                "min_unit_price": u_p,
                "sum_price": u_p,
                "count": 1,
                "unit": lg["unit"],
                "last_bought_at": lg["bought_at"],
            }
        else:
            rec = by_loc[loc_key]
            rec["min_unit_price"] = min(rec["min_unit_price"], u_p)
            rec["sum_price"] += u_p
            rec["count"] += 1

    ranked = sorted(by_loc.values(), key=lambda x: (x["latest_unit_price"], x["min_unit_price"], x["location"]))
    for r in ranked:
        r["avg_unit_price"] = round(r["sum_price"] / r["count"], 2)

    cheapest = ranked[0] if ranked else None
    return {
        "found": True,
        "item": item,
        "locations": ranked,
        "cheapest": cheapest,
        "logs_count": len(logs),
        "last_log": logs[0] if logs else None,
    }


def render_price_analysis(query_name: str) -> str:
    res = analyze_item_prices(query_name)
    if not res["found"]:
        return (
            f"🔎 <b>«{app.esc(query_name)}»</b> bo'yicha xarid tarixi topilmadi.\n"
            "Bozorlik paytida mahsulot yoniga <code>#joy_nomi</code> yozib kiriting "
            "(masalan: <code>Olma 2 kg 24 #bozor</code>)."
        )

    item = res["item"]
    unit = item.get("default_unit") or "kg"
    lines = [f"📊 <b>Narx va joylar tahlili: {app.esc(item['name'])}</b>"]
    if float(item.get("last_unit_price") or 0) > 0:
        d_s = f" ({item['last_bought_date']})" if item.get("last_bought_date") else ""
        lines.append(f"Oxirgi narx: <b>{fmt_sum(item['last_unit_price'])} so'm/{app.esc(unit)}</b>{d_s}")

    last_lg = res.get("last_log")
    if last_lg and float(last_lg.get("price_diff_percent") or 0) != 0:
        dp = float(last_lg["price_diff_percent"])
        sign = "+" if dp > 0 else ""
        ic = "📈" if dp > 0 else "📉"
        lines.append(f"Oxirgi inflyatsiya o'zgarishi: {ic} <b>{sign}{dp:g}%</b>")

    locs = res["locations"]
    if not locs:
        lines.append("\n📍 Joy teglari (<code>#teg</code>) bilan xaridlar hali kiritilmagan.")
        return "\n".join(lines)

    cheapest = res["cheapest"]
    lines.append(
        f"\n🏆 <b>Eng arzon joy: #{app.esc(cheapest['location'])}</b> — "
        f"<b>{fmt_sum(cheapest['latest_unit_price'])} so'm/{app.esc(cheapest['unit'])}</b>"
    )
    lines.append("\n📍 <b>Savdo joylari kesimida:</b>")
    base_p = cheapest["latest_unit_price"]
    for idx, r in enumerate(locs, start=1):
        medal = "🟢" if idx == 1 else "🔸"
        diff_note = ""
        if idx > 1 and base_p > 0:
            extra_pct = round(((r["latest_unit_price"] - base_p) / base_p) * 100.0, 1)
            diff_note = f" (+{extra_pct:g}% qimmat)"
        lines.append(
            f"{medal} <b>#{app.esc(r['location'])}</b>: {fmt_sum(r['latest_unit_price'])} so'm/{app.esc(r['unit'])}"
            f"{diff_note} <i>({r['count']} marta)</i>"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ In-Place UI Yordamchilari va Ekranlar

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


def home_view() -> tuple[str, InlineKeyboardMarkup]:
    drafts = db.q(
        "SELECT b.*, l.name AS loc_name, "
        "(SELECT COUNT(*) FROM market_basket_items bi WHERE bi.basket_id = b.id) AS item_cnt "
        "FROM market_baskets b "
        "LEFT JOIN market_locations l ON l.id = b.location_id "
        "WHERE b.status = 'draft' ORDER BY b.created_at DESC LIMIT 6"
    )
    lines = [
        "🛒 <b>Smart Bozorlik va Dinamik Savat</b>\n",
        "Rejimni tanlang:",
        "• <b>📝 Rejalashtirilgan ro'yxat</b> — uyda oldindan ro'yxat tuzish (narx shart emas, balansga daxl qilmaydi).",
        "• <b>⚡️ Ayni damdagi xarid</b> — hozir bozorda narxlar va <code>#joy</code> teglari bilan xarid qilish.",
    ]
    rows = [
        [btn("📝 Rejalashtirilgan ro'yxat", "mk:new:planned")],
        [btn("⚡️ Ayni damdagi xarid", "mk:new:active")],
    ]
    if drafts:
        lines.append("\n📂 <b>Ochiq savatlar:</b>")
        for d in drafts:
            m_icon = "📝" if d["mode"] == "planned" else "⚡️"
            loc_s = f" #{d['loc_name']}" if d.get("loc_name") else ""
            lines.append(f"• {m_icon} <b>{app.esc(d['title'])}{app.esc(loc_s)}</b> ({d['item_cnt']} ta)")
            rows.append([btn(f"{m_icon} {d['title'][:22]} ({d['item_cnt']})", f"mk:open:{d['id']}")])

    rows.append([btn("📊 Narx tahlili (/narx)", "mk:prices"), btn("💰 Moliya", "fn:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def basket_view(basket_id: str, delete_mode: bool = False, prompt_hint: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    basket = db.q1(
        "SELECT b.*, l.name AS loc_name FROM market_baskets b "
        "LEFT JOIN market_locations l ON l.id = b.location_id WHERE b.id = ?",
        (basket_id,),
    )
    if not basket:
        return home_view()

    items = db.q(
        "SELECT bi.*, mi.name AS item_name, mi.last_unit_price, ml.name AS item_loc "
        "FROM market_basket_items bi "
        "JOIN market_items mi ON mi.id = bi.item_id "
        "LEFT JOIN market_locations ml ON ml.id = bi.location_id "
        "WHERE bi.basket_id = ? ORDER BY bi.id",
        (basket_id,),
    )

    is_planned = basket["mode"] == "planned"
    mode_badge = "📝 Rejalashtirilgan ro'yxat" if is_planned else "⚡️ Ayni damdagi xarid"
    loc_badge = f" · 📍 <b>#{app.esc(basket['loc_name'])}</b>" if basket.get("loc_name") else ""
    lines = [f"🛒 <b>{app.esc(basket['title'])}</b>\n{mode_badge}{loc_badge}\n"]

    rows: list[list[InlineKeyboardButton]] = []

    if not items:
        if is_planned:
            lines.append(
                "<i>Ro'yxat bo'sh. Mahsulot va miqdorni yozib yuboring (narx shart emas):</i>\n"
                "<code>Kartoshka 3 kg\nOlma 2 kg\nNon 4 dona</code>"
            )
        else:
            lines.append(
                "<i>Savat bo'sh. Mahsulot, miqdor, narx va ixtiyoriy #joy yozib yuboring:</i>\n"
                "<code>Kartoshka 5 kg 25 #bozor\nSut 2 litr 22</code>"
            )
    else:
        bought_sum = 0.0
        est_sum = 0.0
        for idx, it in enumerate(items, start=1):
            q_s = f"{fmt_qty(it['quantity'])} {app.esc(it['unit'])}"
            iloc_s = f" <code>#{app.esc(it['item_loc'])}</code>" if it.get("item_loc") else ""
            tot = float(it["total_price"] or 0)
            bought = bool(it["is_bought"])

            if is_planned:
                if tot > 0:
                    price_s = f" — {fmt_sum(tot)} so'm"
                    est_sum += tot
                elif float(it["last_unit_price"] or 0) > 0:
                    approx = float(it["last_unit_price"]) * float(it["quantity"])
                    est_sum += approx
                    price_s = f" <i>(taxm. ~{fmt_sum(approx)})</i>"
                else:
                    price_s = ""
                lines.append(f"{idx}. 📝 <b>{app.esc(it['item_name'])}</b> — {q_s}{price_s}{iloc_s}")
            else:
                mark = "✅" if bought else "⬜"
                if tot > 0:
                    u_s = f"{fmt_sum(it['unit_price'])}/{app.esc(it['unit'])}"
                    price_s = f" — <b>{fmt_sum(tot)} so'm</b> ({u_s})"
                else:
                    price_s = " — <i>narx kiritilmagan</i>"
                if bought:
                    bought_sum += tot
                lines.append(f"{idx}. {mark} <b>{app.esc(it['item_name'])}</b> ({q_s}){price_s}{iloc_s}")

            if delete_mode:
                rows.append([btn(f"🗑 {it['item_name'][:22]} ({fmt_qty(it['quantity'])} {it['unit']})", f"mk:rm:{it['id']}")])
            elif not is_planned and basket["status"] == "draft":
                b_mark = "✅" if bought else "⬜"
                p_lbl = fmt_sum(tot) if tot > 0 else "narx?"
                rows.append([btn(f"{b_mark} {it['item_name'][:18]} ({fmt_qty(it['quantity'])} {it['unit']}) · {p_lbl}", f"mk:tg:{it['id']}")])

        if is_planned and est_sum > 0:
            lines.append(f"\n💡 Oldingi narxlar bo'yicha taxminiy: ~<b>{fmt_sum(est_sum)} so'm</b> (balansdan yechilmaydi)")
        elif not is_planned:
            bought_cnt = sum(1 for x in items if x["is_bought"])
            lines.append(f"\n💰 Olinganlar ({bought_cnt}/{len(items)}): <b>{fmt_sum(bought_sum)} so'm</b>")

    if prompt_hint:
        lines.append(f"\n✍️ <b>{prompt_hint}</b>")

    if delete_mode:
        rows.append([btn("⬅️ Savatga qaytish", f"mk:open:{basket_id}")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    if basket["status"] == "draft":
        rows.append([
            btn("➕ Yangi narsa qo'shish", f"mk:add:{basket_id}"),
            btn("➖ O'chirish", f"mk:delm:{basket_id}"),
        ])
        if is_planned:
            rows.append([btn("⚡️ Ayni damdagi xarid", f"mk:go:{basket_id}")])
        else:
            rows.append([
                btn("📍 Joy (#teg)", f"mk:loc:{basket_id}"),
                btn("🏁 Xaridni yakunlash", f"mk:chk:{basket_id}"),
            ])
        rows.append([
            btn("🗑 Bekor qilish", f"mk:can:{basket_id}"),
            btn("⬅️ Bozorlik", "mk:home"),
        ])
    else:
        rows.append([btn("⬅️ Bozorlik", "mk:home")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _anti_cheat_view(ac: dict, pending: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"🛡 <b>Anti-Cheat: Narx keskin sakrashi (+{ac['diff_pct']:g}%)!</b>\n",
        f"• Mahsulot: <b>{app.esc(pending['item_name'])}</b> ({fmt_qty(pending['quantity'])} {app.esc(pending['unit'])})",
        f"• Oldingi narx: <b>{fmt_sum(ac['last_unit_price'])} so'm/{app.esc(pending['unit'])}</b>",
        f"• Kiritilgan narx: <b>{fmt_sum(ac['unit_price'])} so'm/{app.esc(pending['unit'])}</b> "
        f"(jami: <b>{fmt_sum(pending['total_price'])} so'm</b>)",
        "\nNarx haqiqatan shundaymi?",
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Narx to'g'ri", "mk:ac:ok"), btn("✏️ Qayta kiritish", "mk:ac:re")],
    ])
    return "\n".join(lines), kb


# ------------------------------------------------------------------ Kirish va Buyruq Handlerlari

async def entry(message: Message):
    text, kb = home_view()
    await messenger.send(text, "info", kb)


@router.message(Command("bozorlik"))
async def cmd_market(message: Message, state: FSMContext):
    await state.clear()
    await entry(message)


@router.message(Command("narx"))
async def cmd_price_analysis(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        recent = db.q("SELECT id, name, default_unit, last_unit_price FROM market_items ORDER BY updated_at DESC, id DESC LIMIT 8")
        rows = [[btn(f"📊 {r['name']} ({fmt_sum(r['last_unit_price'])}/{r['default_unit']})", f"mk:pr:{r['id']}")] for r in recent]
        rows.append([btn("🛒 Smart Bozorlik", "mk:home")])
        await messenger.send(
            "📊 <b>Savdo joylari va narx tahlili</b>\n\n"
            "Buyruq: <code>/narx mahsulot_nomi</code>\n"
            + ("Yoki oxirgi mahsulotlardan tanlang:" if recent else "Hali xarid qilingan mahsulotlar yo'q."),
            "info",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    text = render_price_analysis(parts[1].strip())
    await messenger.send(text, "info")


@router.callback_query(F.data == "mk:prices")
async def cb_prices_list(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    recent = db.q("SELECT id, name, default_unit, last_unit_price FROM market_items ORDER BY updated_at DESC, id DESC LIMIT 8")
    rows = [[btn(f"📊 {r['name']} ({fmt_sum(r['last_unit_price'])}/{r['default_unit']})", f"mk:pr:{r['id']}")] for r in recent]
    rows.append([btn("⬅️ Bozorlik", "mk:home")])
    text = (
        "📊 <b>Savdo joylari tahlili</b>\n\n"
        "Istalgan payt <code>/narx mahsulot</code> deb yozishingiz mumkin."
        + ("\nQuyidagi mahsulotlardan birini tanlang:" if recent else "\nHali xaridlar tarixi yo'q.")
    )
    await _edit_or_send(cb.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("mk:pr:"))
async def cb_price_item(cb: CallbackQuery):
    iid = int(cb.data.split(":")[2])
    item = db.q1("SELECT name FROM market_items WHERE id = ?", (iid,))
    if not item:
        await cb.answer("Topilmadi")
        return
    await cb.answer()
    text = render_price_analysis(item["name"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Orqaga", "mk:prices"), btn("🛒 Bozorlik", "mk:home")]])
    await _edit_or_send(cb.message, text, kb)


# ------------------------------------------------------------------ Savat Callback va FSM Oqimlari

@router.callback_query(F.data == "mk:home")
async def cb_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = home_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("mk:new:"))
async def cb_new_basket(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]  # planned | active
    await cb.answer()
    bid = create_basket(mode)
    await state.clear()
    await state.set_state(MarketFSM.adding_items)
    hint = (
        "Mahsulotlarni yozib yuboring (masalan: Kartoshka 3 kg):"
        if mode == "planned"
        else "Mahsulot, miqdor va narxni yozing (masalan: Kartoshka 5 kg 25 #bozor):"
    )
    text, kb = basket_view(bid, prompt_hint=hint)
    mid = await _edit_or_send(cb.message, text, kb, state)
    await state.update_data(basket_id=bid, panel_mid=mid)


@router.callback_query(F.data.startswith("mk:open:"))
async def cb_open_basket(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    await cb.answer()
    await state.clear()
    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(bid)
    mid = await _edit_or_send(cb.message, text, kb, state)
    await state.update_data(basket_id=bid, panel_mid=mid)


@router.callback_query(F.data.startswith("mk:go:"))
async def cb_switch_to_active(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    switch_basket_to_active(bid)
    await cb.answer("⚡️ Ayni damdagi xarid rejimiga o'tkazildi!")
    await state.clear()
    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(
        bid,
        prompt_hint="Olingan mahsulotni bosing (narx kiritish uchun) yoki yangi mahsulot yozing:",
    )
    mid = await _edit_or_send(cb.message, text, kb, state)
    await state.update_data(basket_id=bid, panel_mid=mid)


@router.callback_query(F.data.startswith("mk:add:"))
async def cb_add_more(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    basket = db.q1("SELECT mode FROM market_baskets WHERE id = ?", (bid,))
    if not basket:
        await cb.answer("Savat topilmadi")
        return
    await cb.answer()
    await state.set_state(MarketFSM.adding_items)
    await state.update_data(basket_id=bid, panel_mid=cb.message.message_id)
    hint = (
        "Yangi mahsulot va miqdorni yozing (masalan: Guruch 2 kg):"
        if basket["mode"] == "planned"
        else "Yangi mahsulot yozing (masalan: Guruch 2 kg 36 #bozor):"
    )
    text, kb = basket_view(bid, prompt_hint=hint)
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("mk:delm:"))
async def cb_delete_mode(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    await cb.answer()
    text, kb = basket_view(bid, delete_mode=True, prompt_hint="O'chirmoqchi bo'lgan mahsulotingizni bosing:")
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("mk:rm:"))
async def cb_remove_item(cb: CallbackQuery, state: FSMContext):
    bitem_id = int(cb.data.split(":")[2])
    bid = remove_basket_item(bitem_id)
    if not bid:
        await cb.answer("Topilmadi")
        return
    await cb.answer("O'chirildi")
    rem_cnt = db.q1("SELECT COUNT(*) AS c FROM market_basket_items WHERE basket_id = ?", (bid,))["c"]
    text, kb = basket_view(bid, delete_mode=(rem_cnt > 0))
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("mk:can:"))
async def cb_cancel_basket(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    db.ex("UPDATE market_baskets SET status = 'cancelled' WHERE id = ?", (bid,))
    await cb.answer("Savat bekor qilindi")
    await state.clear()
    text, kb = home_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("mk:tg:"))
async def cb_toggle_bought(cb: CallbackQuery, state: FSMContext):
    bitem_id = int(cb.data.split(":")[2])
    bitem = db.q1(
        "SELECT bi.*, mi.name AS item_name FROM market_basket_items bi "
        "JOIN market_items mi ON mi.id = bi.item_id WHERE bi.id = ?",
        (bitem_id,),
    )
    if not bitem:
        await cb.answer("Topilmadi")
        return
    bid = bitem["basket_id"]
    # Agar hali narxi kiritilmagan bo'lsa, darhol shu xabarda narx so'raymiz!
    if not bitem["is_bought"] and float(bitem["total_price"] or 0) <= 0:
        await cb.answer()
        await state.set_state(MarketFSM.entering_price)
        await state.update_data(
            basket_id=bid,
            edit_bitem_id=bitem_id,
            panel_mid=cb.message.message_id,
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Savatga qaytish", f"mk:open:{bid}")]])
        await _edit_or_send(
            cb.message,
            f"💰 <b>{app.esc(bitem['item_name'])}</b> ({fmt_qty(bitem['quantity'])} {app.esc(bitem['unit'])})\n\n"
            "Jami narxini (va ixtiyoriy <code>#joy</code> tegini) yozing:\n"
            "Masalan: <code>30</code> (30 000 so'm) yoki <code>30 #bozor</code>",
            kb,
            state,
        )
        return

    new_val = 0 if bitem["is_bought"] else 1
    db.ex("UPDATE market_basket_items SET is_bought = ? WHERE id = ?", (new_val, bitem_id))
    await cb.answer("✅ Olindi" if new_val else "⬜ Olib tashlandi")
    await state.set_state(MarketFSM.adding_items)
    await state.update_data(basket_id=bid, panel_mid=cb.message.message_id)
    text, kb = basket_view(bid)
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(F.data.startswith("mk:loc:"))
async def cb_set_location_prompt(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    await cb.answer()
    await state.set_state(MarketFSM.setting_location)
    await state.update_data(basket_id=bid, panel_mid=cb.message.message_id)
    locs = db.q("SELECT name FROM market_locations ORDER BY id DESC LIMIT 6")
    rows = [[btn(f"📍 #{r['name']}", f"mk:sloc:{r['name']}")] for r in locs]
    rows.append([btn("⬅️ Savatga qaytish", f"mk:open:{bid}")])
    await _edit_or_send(
        cb.message,
        "📍 <b>Savdo joyi tegini kiriting</b> (masalan: <code>#bozor</code> yoki joy nomi):",
        InlineKeyboardMarkup(inline_keyboard=rows),
        state,
    )


@router.callback_query(MarketFSM.setting_location, F.data.startswith("mk:sloc:"))
async def cb_pick_saved_loc(cb: CallbackQuery, state: FSMContext):
    loc_name = cb.data.split(":", 2)[2]
    data = await state.get_data()
    bid = data["basket_id"]
    loc_id = get_or_create_location(loc_name)
    if loc_id:
        db.ex("UPDATE market_baskets SET location_id = ? WHERE id = ?", (loc_id, bid))
    await cb.answer(f"📍 #{loc_name}")
    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(bid)
    await _edit_or_send(cb.message, text, kb, state)


@router.message(MarketFSM.setting_location)
async def msg_set_location(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    bid = data["basket_id"]
    loc_id = get_or_create_location(raw)
    if loc_id:
        db.ex("UPDATE market_baskets SET location_id = ? WHERE id = ?", (loc_id, bid))
    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(bid)
    await _edit_or_send(None, text, kb, state)


# ------------------------------------------------------------------ Matn Kiritish va Anti-Cheat Tasdig'i

async def _process_parsed_queue(state: FSMContext, lines_queue: list[dict], bad_lines: list[str] | None = None):
    data = await state.get_data()
    bid = data["basket_id"]
    bad_lines = list(bad_lines or [])

    while lines_queue:
        cur = lines_queue.pop(0)
        res = add_basket_item(
            bid,
            cur["item_name"],
            cur["quantity"],
            unit=cur["unit"],
            total_price=cur["total_price"],
            location_name=cur["location"],
            force=False,
        )
        if not res["ok"] and res.get("anti_cheat"):
            await state.set_state(MarketFSM.anti_cheat)
            await state.update_data(
                ac_pending=res["pending"],
                ac_rest_queue=lines_queue,
                ac_bad_lines=bad_lines,
            )
            text, kb = _anti_cheat_view(res["details"], res["pending"])
            await _edit_or_send(None, text, kb, state)
            return

    await state.set_state(MarketFSM.adding_items)
    warn = f"⚠️ Tushunarsiz qator: {', '.join(bad_lines)}" if bad_lines else None
    text, kb = basket_view(bid, prompt_hint=warn)
    await _edit_or_send(None, text, kb, state)


@router.message(MarketFSM.adding_items)
async def msg_adding_items(message: Message, state: FSMContext):
    raw_text = (message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    if not raw_text:
        return
    data = await state.get_data()
    bid = data.get("basket_id")
    if not bid:
        await state.clear()
        return

    parsed_list = []
    bad = []
    for ln in raw_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        p = parse_market_line(ln)
        if p:
            parsed_list.append(p)
        else:
            bad.append(ln)

    if not parsed_list:
        text, kb = basket_view(
            bid,
            prompt_hint="Format: Mahsulot Miqdor [kg|dona|litr|metr] [Narx] [#joy] (masalan: Olma 2 kg 24 #bozor)",
        )
        await _edit_or_send(None, text, kb, state)
        return

    await _process_parsed_queue(state, parsed_list, bad)


@router.message(MarketFSM.entering_price)
async def msg_entering_item_price(message: Message, state: FSMContext):
    raw_text = _norm_apostrophes(message.text or "").strip()
    await messenger.delete_user_quiet(message.message_id)
    data = await state.get_data()
    bid = data.get("basket_id")
    bitem_id = data.get("edit_bitem_id")
    if not bid or not bitem_id:
        await state.clear()
        return

    m = PRICE_ONLY_RE.match(raw_text)
    if not m:
        return
    raw_p = float(m.group("price").replace(",", "."))
    loc = normalize_location_name(m.group("loc")) if m.group("loc") else None

    res = set_basket_item_price(bitem_id, raw_p, location_name=loc, force=False)
    if not res["ok"] and res.get("anti_cheat"):
        await state.set_state(MarketFSM.anti_cheat)
        await state.update_data(ac_pending=res["pending"], ac_rest_queue=[], ac_bad_lines=[])
        text, kb = _anti_cheat_view(res["details"], res["pending"])
        await _edit_or_send(None, text, kb, state)
        return

    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(bid)
    await _edit_or_send(None, text, kb, state)


@router.callback_query(MarketFSM.anti_cheat, F.data == "mk:ac:ok")
async def cb_anti_cheat_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Narx tasdiqlandi")
    data = await state.get_data()
    pending = data.get("ac_pending") or {}
    rest_q = list(data.get("ac_rest_queue") or [])
    bad_l = list(data.get("ac_bad_lines") or [])

    if pending.get("kind") == "new_item":
        add_basket_item(
            pending["basket_id"],
            pending["item_name"],
            pending["quantity"],
            unit=pending["unit"],
            total_price=pending["total_price"],
            location_name=pending.get("location"),
            force=True,
        )
    elif pending.get("kind") == "set_price":
        set_basket_item_price(
            pending["bitem_id"],
            pending["total_price"],
            location_name=pending.get("location"),
            force=True,
        )

    await state.update_data(ac_pending=None, ac_rest_queue=[], ac_bad_lines=[])
    if rest_q:
        await _process_parsed_queue(state, rest_q, bad_l)
        return

    bid = pending.get("basket_id") or data.get("basket_id")
    await state.set_state(MarketFSM.adding_items)
    text, kb = basket_view(bid)
    await _edit_or_send(cb.message, text, kb, state)


@router.callback_query(MarketFSM.anti_cheat, F.data == "mk:ac:re")
async def cb_anti_cheat_reenter(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Qayta kiriting")
    data = await state.get_data()
    pending = data.get("ac_pending") or {}
    bid = pending.get("basket_id") or data.get("basket_id")

    if pending.get("kind") == "set_price":
        await state.set_state(MarketFSM.entering_price)
        await state.update_data(edit_bitem_id=pending["bitem_id"], ac_pending=None)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Savatga qaytish", f"mk:open:{bid}")]])
        await _edit_or_send(
            cb.message,
            f"✏️ <b>{app.esc(pending['item_name'])}</b> ({fmt_qty(pending['quantity'])} {app.esc(pending['unit'])}) — "
            "to'g'ri jami narxni yozing:",
            kb,
            state,
        )
        return

    await state.set_state(MarketFSM.adding_items)
    await state.update_data(ac_pending=None)
    hint = f"«{pending.get('item_name', 'Mahsulot')}» qatorini to'g'ri narx bilan qaytadan yozing:"
    text, kb = basket_view(bid, prompt_hint=hint)
    await _edit_or_send(cb.message, text, kb, state)


# ------------------------------------------------------------------ Checkout («🏁 Xaridni yakunlash» -> feat_finance)

@router.callback_query(F.data.startswith("mk:chk:"))
async def cb_checkout_start(cb: CallbackQuery, state: FSMContext):
    bid = cb.data.split(":", 2)[2]
    bought = db.q(
        "SELECT bi.*, mi.name AS item_name FROM market_basket_items bi "
        "JOIN market_items mi ON mi.id = bi.item_id "
        "WHERE bi.basket_id = ? AND bi.is_bought = 1 ORDER BY bi.id",
        (bid,),
    )
    if not bought:
        await cb.answer("Avval olingan mahsulotlarni [✅] belgilang va narxini kiriting!", show_alert=True)
        return
    no_price = [r["item_name"] for r in bought if float(r["total_price"] or 0) <= 0]
    if no_price:
        await cb.answer(f"Narxi kiritilmagan: {', '.join(no_price)}", show_alert=True)
        return

    accs = db.q("SELECT * FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC, id ASC")
    total_sum = int(round(sum(float(r["total_price"]) for r in bought)))
    await cb.answer()
    await state.update_data(basket_id=bid, panel_mid=cb.message.message_id)

    if not accs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("➕ Hisob / karta qo'shish", "fn:anew")],
            [btn("⬅️ Savatga qaytish", f"mk:open:{bid}")],
        ])
        await _edit_or_send(
            cb.message,
            f"🏁 <b>Xaridni yakunlash</b>\nJami: <b>{fmt_sum(total_sum)} so'm</b>\n\n"
            "⚠️ To'lovni yechish uchun avval kamida bitta Moliya hisobini qo'shing:",
            kb,
            state,
        )
        return

    import feat_finance as fin

    rows = [
        [btn(f"{fin.ACC_TYPE_LABELS[a['type']]} {a['name'][:18]} ({fmt_sum(a['balance'])})", f"mk:pay:{a['id']}")]
        for a in accs
    ]
    rows.append([btn("⬅️ Savatga qaytish", f"mk:open:{bid}")])
    lines = [
        "🏁 <b>Xaridni yakunlash — hisobni tanlang</b>\n",
        f"• Olingan mahsulotlar: <b>{len(bought)} ta</b>",
        f"• Jami yechiladigan summa: <b>{fmt_sum(total_sum)} so'm</b>\n",
        "Qaysi hisobdan to'landingiz?",
    ]
    await _edit_or_send(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), state)


@router.callback_query(F.data.startswith("mk:pay:"))
async def cb_checkout_pay(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split(":")[2])
    data = await state.get_data()
    bid = data.get("basket_id")
    if not bid:
        draft = db.q1("SELECT id FROM market_baskets WHERE status = 'draft' AND mode = 'active' ORDER BY created_at DESC LIMIT 1")
        bid = draft["id"] if draft else None
    if not bid:
        await cb.answer("Faol savat topilmadi", show_alert=True)
        return

    res = checkout_basket(bid, acc_id)
    if not res["ok"]:
        await cb.answer(res["error"], show_alert=True)
        return

    await cb.answer("✅ Bozorlik yakunlandi!")
    await state.clear()

    loc_s = f" (📍 <b>#{app.esc(res['location'])}</b>)" if res.get("location") else ""
    lines = [
        f"✅ <b>Bozorlik yakunlandi!</b>{loc_s}\n",
        f"💳 Hisob: <b>{app.esc(res['account_name'])}</b> (−<b>{fmt_sum(res['total_amount'])} so'm</b>)",
        "🗂 Kategoriya: <b>Oziq-ovqat / Bozorlik</b>\n",
        "📈 <b>Inflyatsiya monitoringi:</b>",
    ]
    for it in res["items"]:
        dp = float(it["price_diff_percent"])
        if float(it["prev_unit_price"]) <= 0:
            dyn = "🆕 ilk narx"
        elif dp == 0:
            dyn = "➖ 0% (o'zgarmadi)"
        elif dp > 0:
            dyn = f"📈 +{dp:g}%"
        else:
            dyn = f"📉 {dp:g}%"
        lines.append(
            f"• <b>{app.esc(it['name'])}</b> ({fmt_qty(it['quantity'])} {app.esc(it['unit'])}): "
            f"{fmt_sum(it['total_price'])} so'm ({fmt_sum(it['unit_price'])}/{app.esc(it['unit'])}) — {dyn}"
        )

    tx_id = res.get("tx_id")
    kb_rows = []
    if tx_id is not None:
        kb_rows.append([btn("↩️ Bekor qilish (1 daq)", f"fn:undo:{tx_id}")])
    kb_rows.append([btn("🛒 Bozorlik", "mk:home"), btn("💰 Moliya paneli", "fn:home")])
    mid = await _edit_or_send(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))

    if mid is not None and tx_id is not None and "finance" in app.OPTIONAL:
        fin = app.OPTIONAL["finance"]
        app.schedule_job(
            app.now() + timedelta(seconds=fin.UNDO_WINDOW_SEC),
            fin.expire_undo_markup,
            [mid, tx_id],
            f"undo_exp_{tx_id}",
        )
