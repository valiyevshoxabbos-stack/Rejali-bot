"""
Salomatlik, Oddiy Dorixona, Kurslar, Triage va Skin Care moduli (3.7 — feat_health.py):
- Qat'iy tibbiy xavfsizlik: har bir xabar oxirida majburiy 103 ogohlantirish footeri.
- 1. Oddiy aptechka: Uyda va sumkada filtrlari, qoldiq hisobi, [💊 Ichdim (-1)],
     takroriy doza (<1 soat) tekshiruvi, kam qolgan (<=2) dorilarni bozorlikka qo'shish (#dorixona),
     ketma-ket 3 kunlik suiiste'mol (overuse) ogohlantirishi.
     Yangi sotib olish Moliya (Sport / Sog'liq) bilan integratsiyalashgan.
- 2. Davolanish kursi: shifokor tavsiyalari, dori vaqtlari eslatmasi, [✅ Ichdim], [⏰ 15 daq keyin].
- 3. Virtual Dorixonachi (Triage): multi-select alomatlar, Qizil bayroq (Red Flag) filtri (dori berilmaydi,
     darhol 103), yengil alomatlarda 3 bosqichli tavsiya (tabiiy, uyda bor, OTC).
- 4. Skin Care & Tonggi gigiyena: parvarish vositalari, ob-havoga mos tavsiyalar, 06:45 tonggi gigiyena eslatmasi.
- 5. "Sog'lom Hafta" reytingi va kasallik kunlarida sport intizomi jarimasini bekor qilish.
"""

import asyncio
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.triggers.cron import CronTrigger

import app
import db
import messenger
import services as sv

router = Router()

HEALTH_FOOTER = "\n\n🚑 Unutmang: agar o'zingizni yomon his etsangiz, darhol 103 tez yordamga qo'ng'iroq qiling!"
PAGE_SIZE = 6


class HealthFSM(StatesGroup):
    # Add medicine / item
    item_name = State()
    item_cat = State()
    item_loc = State()
    item_form = State()
    item_dosage = State()
    item_qty = State()
    item_unit = State()
    item_source = State()
    item_exp = State()
    item_notes = State()
    item_price = State()
    item_account = State()

    # Add course
    course_title = State()
    course_notes = State()
    course_days = State()
    course_med_name = State()
    course_med_times = State()
    course_med_dose = State()
    course_med_food = State()

    # Triage selection
    triage_select = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def fmt_sum(n: int | float) -> str:
    val = int(round(float(n)))
    sign = "−" if val < 0 else ""
    return f"{sign}{abs(val):,}".replace(",", " ")


def fmt_qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{float(q):g}"


def with_footer(text: str) -> str:
    if HEALTH_FOOTER.strip() in text:
        return text
    return text.rstrip() + HEALTH_FOOTER


async def _edit_or_send(target_msg, text: str, kb: InlineKeyboardMarkup | None):
    markup = messenger.with_delete(kb)
    final_text = with_footer(text)
    mid = target_msg.message_id if target_msg is not None else None
    if mid is not None:
        try:
            await app.bot.edit_message_text(
                final_text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=markup
            )
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return mid
        except Exception:
            pass
    sent = await messenger.send(final_text, "info", kb, bind_trigger=False)
    return sent.message_id if sent else None


# ------------------------------------------------------------------ 1. Asosiy Menyu va Ekranlar

def main_health_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("💊 Oddiy aptechka", "hl:ap:home:0"), btn("🩺 Davolanish kursi", "hl:cr:home")],
        [btn("👨‍⚕️ Dorixonachi (Simptomlar)", "hl:tr:home"), btn("🧴 Skin Care & Gigiyena", "hl:sk:home")],
    ])


def main_health_view() -> tuple[str, InlineKeyboardMarkup]:
    total_meds = db.q1("SELECT COUNT(*) AS c FROM health_items WHERE kind = 'medicine' AND is_active = 1 AND remaining_quantity > 0")["c"]
    total_skin = db.q1("SELECT COUNT(*) AS c FROM health_items WHERE kind = 'skincare' AND is_active = 1 AND remaining_quantity > 0")["c"]
    active_courses = db.q1("SELECT COUNT(*) AS c FROM health_courses WHERE is_active = 1")["c"]
    low_stock = db.q1(
        "SELECT COUNT(*) AS c FROM health_items WHERE is_active = 1 AND remaining_quantity > 0 AND remaining_quantity <= low_stock_threshold"
    )["c"]

    lines = [
        "🏥 <b>Salomatlik, Dorixona & Parvarish markazi</b>\n",
        f"💊 <b>Aptechkadagi dorilar:</b> {total_meds} ta",
        f"🧴 <b>Parvarish vositalari:</b> {total_skin} ta",
        f"🩺 <b>Faol davolanish kurslari:</b> {active_courses} ta",
    ]
    if low_stock > 0:
        lines.append(f"⚠️ <b>Tugab borayotgan vositalar:</b> {low_stock} ta")
    lines.append("\nQuyidagi bo'limlardan birini tanlang:")
    return "\n".join(lines), main_health_kb()


async def entry(message: Message):
    text, kb = main_health_view()
    await messenger.send(with_footer(text), "info", kb)


@router.message(Command("salomatlik"))
@router.message(Command("dorixona"))
async def cmd_health_home(message: Message, state: FSMContext):
    await state.clear()
    await entry(message)


@router.callback_query(F.data == "hl:home")
async def cb_health_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = main_health_view()
    await _edit_or_send(cb.message, text, kb)


# ------------------------------------------------------------------ 2. Oddiy Aptechka (Pharmacy & Stock)

def pharmacy_view(loc: str = "home", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    db.seed_health_data()
    where = "WHERE is_active = 1 AND remaining_quantity > 0"
    args = []
    if loc in ("home", "bag"):
        where += " AND location = ?"
        args.append(loc)

    total = db.q1(f"SELECT COUNT(*) AS c FROM health_items {where}", tuple(args))["c"]
    max_page = max(0, (total - 1) // PAGE_SIZE)
    page = min(max(0, page), max_page)
    offset = page * PAGE_SIZE

    rows = db.q(
        f"SELECT i.*, c.name AS cat_name, c.icon AS cat_icon "
        f"FROM health_items i LEFT JOIN health_categories c ON c.id = i.category_id "
        f"{where} ORDER BY i.remaining_quantity ASC, i.name ASC LIMIT ? OFFSET ?",
        tuple(args + [PAGE_SIZE, offset]),
    )

    loc_titles = {"home": "🏠 Uyda bor", "bag": "🎒 Sumkada", "all": "📦 Barcha vositalar"}
    lines = [f"💊 <b>Oddiy Aptechka</b> — <i>{loc_titles.get(loc, 'Roʻyxat')}</i>\n"]
    if not rows:
        lines.append("Aptechkangizda hozircha dori yo'q.\n[➕ Yangi dori qo'shish] orqali shaxsiy zaxirangizni kiriting.")
    else:
        for r in rows:
            icon = r.get("cat_icon") or "💊"
            low_warn = " ⚠️ (kam qoldi!)" if float(r["remaining_quantity"]) <= float(r["low_stock_threshold"]) else ""
            lines.append(
                f"• {icon} <b>{app.esc(r['name'])}</b>: <b>{fmt_qty(r['remaining_quantity'])} {app.esc(r['unit'])}</b>{low_warn}"
            )

    kb_rows = [
        [
            btn("• 🏠 Uyda" if loc == "home" else "🏠 Uyda", "hl:ap:home:0"),
            btn("• 🎒 Sumkada" if loc == "bag" else "🎒 Sumkada", "hl:ap:bag:0"),
            btn("• 📦 Barchasi" if loc == "all" else "📦 Barchasi", "hl:ap:all:0"),
        ]
    ]

    # Item buttons for details
    item_btns = []
    for r in rows:
        item_btns.append(btn(f"{r['name'][:18]}", f"hl:it:{r['id']}"))
    for i in range(0, len(item_btns), 2):
        kb_rows.append(item_btns[i : i + 2])

    # Pagination
    nav_row = []
    if page > 0:
        nav_row.append(btn("⬅️ Oldingi", f"hl:ap:{loc}:{page - 1}"))
    if page < max_page:
        nav_row.append(btn("Keyingi ➡️", f"hl:ap:{loc}:{page + 1}"))
    if nav_row:
        kb_rows.append(nav_row)

    kb_rows.append([btn("➕ Yangi dori qo'shish", "hl:add_it"), btn("⬅️ Asosiy menyu", "hl:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.startswith("hl:ap:"))
async def cb_pharmacy_list(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    loc = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    await cb.answer()
    await state.clear()
    text, kb = pharmacy_view(loc, page)
    await _edit_or_send(cb.message, text, kb)


def item_detail_view(item_id: int) -> tuple[str, InlineKeyboardMarkup]:
    item = db.q1(
        "SELECT i.*, c.name AS cat_name, c.icon AS cat_icon "
        "FROM health_items i LEFT JOIN health_categories c ON c.id = i.category_id WHERE i.id = ?",
        (item_id,),
    )
    if not item:
        return "⚠️ Vosita topilmadi.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Aptechka", "hl:ap:home:0")]])

    loc_name = "🏠 Uyda" if item["location"] == "home" else ("🎒 Sumkada" if item["location"] == "bag" else "Boshqa")
    cat_rows = db.q(
        "SELECT c.name, c.icon FROM health_item_categories ic JOIN health_categories c ON c.id = ic.category_id WHERE ic.item_id = ? ORDER BY c.sort_order, c.id",
        (item_id,),
    )
    if cat_rows:
        cat_disp = ", ".join(f"{c.get('icon') or ''} {c['name']}".strip() for c in cat_rows)
    else:
        cat_disp = item.get("cat_name") or "Umumiy"

    lines = [
        f"{item.get('cat_icon') or '💊'} <b>{app.esc(item['name'])}</b>\n",
        f"📂 <b>Kategoriya:</b> {app.esc(cat_disp)}",
        f"📍 <b>Joylashuvi:</b> {loc_name}",
        f"💊 <b>Shakli:</b> {app.esc(item['form'])}",
        f"🔢 <b>Doza:</b> {app.esc(item['default_dosage']) or 'belgilanmagan'}",
        f"📦 <b>Qoldiq:</b> <b>{fmt_qty(item['remaining_quantity'])} {app.esc(item['unit'])}</b>",
    ]
    if item.get("expiry_date"):
        lines.append(f"⏳ <b>Yaroqlilik muddati:</b> {item['expiry_date']}")
    if item.get("notes"):
        lines.append(f"📝 <b>Izoh:</b> {app.esc(item['notes'])}")

    if float(item["remaining_quantity"]) <= float(item["low_stock_threshold"]):
        lines.append("\n⚠️ <b>Qoldiq kam qoldi!</b> Bozorlik rejasiga qo'shish tavsiya etiladi.")

    # Oxirgi ichilgan vaqti
    last_log = db.q1("SELECT taken_at FROM health_intake_logs WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
    if last_log:
        try:
            dt_s = datetime.fromisoformat(last_log["taken_at"]).strftime("%d.%m %H:%M")
        except Exception:
            dt_s = last_log["taken_at"][:16]
        lines.append(f"🕒 <b>Oxirgi qabul:</b> {dt_s}")

    kb_rows = [
        [btn("💊 Ichdim (-1)", f"hl:drk:{item_id}")],
    ]
    if float(item["remaining_quantity"]) <= float(item["low_stock_threshold"]):
        kb_rows.append([btn("🛒 Bozorlikka qo'shish", f"hl:low_mkt:{item_id}")])
    kb_rows.append([
        btn("🗑 O'chirish", f"hl:del_it:{item_id}"),
        btn("⬅️ Orqaga", f"hl:ap:{item['location']}:0"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data.startswith("hl:it:"))
async def cb_item_detail(cb: CallbackQuery, state: FSMContext):
    item_id = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    text, kb = item_detail_view(item_id)
    await _edit_or_send(cb.message, text, kb)


# --- Dori ichish, Double-Dose, Low-Stock va Overuse ogohlantirishlari ---

def check_double_dose(item_id: int) -> dict | None:
    """Oxirgi 60 daqiqa ichida shu dori ichilganmi tekshiradi."""
    last_log = db.q1("SELECT taken_at FROM health_intake_logs WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,))
    if not last_log:
        return None
    try:
        t = datetime.fromisoformat(last_log["taken_at"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=app.TZ)
        diff_sec = (app.now() - t).total_seconds()
        if 0 <= diff_sec < 3600:
            return {"recent": True, "taken_at": t, "minutes_ago": int(diff_sec // 60)}
    except Exception:
        pass
    return None


def check_overuse_alert(item_id: int) -> dict | None:
    """Ketma-ket 3 kun bitta kategoriyadagi dori ichilganmi tekshiradi."""
    item = db.q1("SELECT category_id FROM health_items WHERE id = ?", (item_id,))
    if not item or not item.get("category_id"):
        return None
    cat_id = item["category_id"]
    rows = db.q(
        "SELECT DISTINCT DATE(l.taken_at) AS log_d "
        "FROM health_intake_logs l JOIN health_items i ON i.id = l.item_id "
        "WHERE i.category_id = ? ORDER BY log_d DESC LIMIT 4",
        (cat_id,),
    )
    dates = {r["log_d"] for r in rows if r["log_d"]}
    today = app.today()
    d0 = today.isoformat()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    if d0 in dates and d1 in dates and d2 in dates:
        cat = db.q1("SELECT name FROM health_categories WHERE id = ?", (cat_id,))
        return {"overuse": True, "category_name": cat["name"] if cat else "bu kategoriya"}
    return None


def execute_intake(item_id: int, quantity: float = 1.0) -> dict:
    item = db.q1("SELECT * FROM health_items WHERE id = ?", (item_id,))
    if not item:
        return {"ok": False, "error": "Vosita topilmadi."}

    cur_q = float(item["remaining_quantity"])
    new_q = max(0.0, cur_q - quantity)
    db.ex("UPDATE health_items SET remaining_quantity = ? WHERE id = ?", (new_q, item_id))
    db.ins(
        "INSERT INTO health_intake_logs (item_id, quantity, status, taken_at) VALUES (?, ?, 'taken', ?)",
        (item_id, quantity, app.now().isoformat()),
    )

    low = new_q <= float(item["low_stock_threshold"])
    return {"ok": True, "new_quantity": new_q, "item": item, "low_stock": low}


@router.callback_query(F.data.startswith("hl:drk:"))
async def cb_drink_item(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    item = db.q1("SELECT * FROM health_items WHERE id = ?", (item_id,))
    if not item:
        await cb.answer("Vosita topilmadi", show_alert=True)
        return

    # Double dose check (< 1 soat)
    dd = check_double_dose(item_id)
    if dd:
        await cb.answer()
        text = (
            f"⚠️ <b>DIQQAT: TAKRORIY DOZA XAVFI!</b>\n\n"
            f"Siz <b>{app.esc(item['name'])}</b> vositasini oxirgi 1 soat ichida "
            f"({dd['minutes_ago']} daqiqa oldin, soat {dd['taken_at'].strftime('%H:%M')} da) ichgansiz.\n\n"
            f"Shifokor ko'rsatmasisiz qisqa vaqtda qayta ichish xavfli bo'lishi mumkin. "
            f"Baribir qabul qilinsinmi?"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Baribir ichdim", f"hl:drk_f:{item_id}"), btn("❌ Bekor qilish", f"hl:it:{item_id}")],
        ])
        await _edit_or_send(cb.message, text, kb)
        return

    res = execute_intake(item_id)
    await cb.answer("Qabul qayd etildi!")
    await _show_intake_result(cb.message, item_id, res)


@router.callback_query(F.data.startswith("hl:drk_f:"))
async def cb_drink_force(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    await cb.answer("Qayd etildi!")
    res = execute_intake(item_id)
    await _show_intake_result(cb.message, item_id, res)


async def _show_intake_result(target_msg, item_id: int, res: dict):
    item = res["item"]
    new_q = res["new_quantity"]
    lines = [
        f"✅ <b>{app.esc(item['name'])}</b> qabul qilindi.\n",
        f"📦 Qoldiq: <b>{fmt_qty(new_q)} {app.esc(item['unit'])}</b>",
    ]

    kb_rows = []
    if res["low_stock"]:
        lines.append(f"\n⚠️ <b>Diqqat:</b> Ushbu dori tugab bormoqda (≤ {fmt_qty(item['low_stock_threshold'])}).")
        kb_rows.append([btn("🛒 Bozorlikka qo'shish", f"hl:low_mkt:{item_id}")])

    # Ketma-ket 3 kunlik overuse tekshiruvi
    ov = check_overuse_alert(item_id)
    if ov:
        lines.append(
            f"\n⚠️ <b>OGOHLANTIRISH:</b> Siz ketma-ket 3 kundan buyon <b>{app.esc(ov['category_name'])}</b> "
            f"vositalarini qabul qilmoqdasiz. O'z-o'zini davolash xavfli bo'lishi mumkin, iltimos shifokorga murojaat qiling!"
        )

    kb_rows.append([btn("⬅️ Aptechka", f"hl:ap:{item['location']}:0"), btn("🏠 Asosiy menyu", "hl:home")])
    await _edit_or_send(target_msg, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("hl:low_mkt:"))
async def cb_add_low_to_market(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    item = db.q1("SELECT * FROM health_items WHERE id = ?", (item_id,))
    if not item:
        await cb.answer("Vosita topilmadi", show_alert=True)
        return

    mkt_title = f"{item['name']} #dorixona"
    added = False
    if "market" in app.OPTIONAL:
        mkt = app.OPTIONAL["market"]
        # Planned rejadagi savatni topish yoki yaratish
        b_row = db.q1("SELECT id FROM market_baskets WHERE mode = 'planned' AND status = 'draft' ORDER BY created_at DESC LIMIT 1")
        if b_row:
            bid = b_row["id"]
        else:
            bid = mkt.create_basket("planned", f"Reja ({app.today().strftime('%d.%m')})")
        mkt.add_basket_item(bid, item["name"], 1.0, unit=item["unit"], total_price=0.0, location_name="dorixona", force=True)
        added = True
    elif "market_baskets" in [r[0] for r in db.q("SELECT name FROM sqlite_master WHERE type='table'")]:
        # Fallback to direct DB insert
        import uuid
        bid = str(uuid.uuid4())
        db.ex("INSERT INTO market_baskets (id, title, mode, status) VALUES (?, ?, 'planned', 'draft')",
              (bid, f"Reja ({app.today().strftime('%d.%m')})"))
        item_row = db.q1("SELECT id FROM market_items WHERE LOWER(name) = LOWER(?)", (item["name"],))
        it_id = item_row["id"] if item_row else db.ins("INSERT INTO market_items (name, default_unit) VALUES (?, ?)", (item["name"], item["unit"]))
        loc_row = db.q1("SELECT id FROM market_locations WHERE LOWER(name) = 'dorixona'")
        loc_id = loc_row["id"] if loc_row else db.ins("INSERT INTO market_locations (name) VALUES ('dorixona')")
        db.ex("INSERT INTO market_basket_items (basket_id, item_id, location_id, quantity, unit) VALUES (?,?,?,?,?)",
              (bid, it_id, loc_id, 1.0, item["unit"]))
        added = True

    await cb.answer("🛒 Bozorlik rejasiga qo'shildi!", show_alert=True)
    text, kb = item_detail_view(item_id)
    await _edit_or_send(cb.message, f"✅ <b>{app.esc(item['name'])}</b> bozorlik rejasiga (#dorixona tegi bilan) qo'shildi!\n\n" + text, kb)


@router.callback_query(F.data.startswith("hl:del_it:"))
async def cb_delete_item(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    db.ex("UPDATE health_items SET is_active = 0 WHERE id = ?", (item_id,))
    await cb.answer("Vosita o'chirildi")
    text, kb = pharmacy_view("home", 0)
    await _edit_or_send(cb.message, text, kb)


# --- Dori qo'shish FSM (Uyda bor zaxira vs Yangi sotib oldim) ---

@router.callback_query(F.data == "hl:add_it")
async def cb_start_add_item(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(HealthFSM.item_name)
    await state.update_data(panel_mid=cb.message.message_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "hl:home")]])
    await _edit_or_send(cb.message, "➕ <b>Yangi dori/vosita qo'shish</b>\n\nVosita nomini kiriting (masalan: <code>Tsitramon</code>):", kb)


def category_multiselect_kb(selected_ids: set[int]) -> InlineKeyboardMarkup:
    cats = db.q("SELECT id, name, icon FROM health_categories ORDER BY sort_order, id")
    rows = []
    for c in cats:
        cid = c["id"]
        checked = "✅" if cid in selected_ids else "⬜"
        icon = c.get("icon") or "💊"
        title = f"{checked} {icon} {c['name']}"
        rows.append([btn(title, f"hl:togcat:{cid}")])
    cnt = len(selected_ids)
    rows.append([btn(f"➡️ Davom etish (Tanlandi: {cnt} ta)", "hl:cat_done")])
    rows.append([btn("❌ Bekor qilish", "hl:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(HealthFSM.item_name)
async def process_item_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await messenger.send("⚠️ Nom kamida 2 belgidan iborat bo'lsin.", "info", bind_trigger=False)
        return
    await state.update_data(item_name=name, selected_cats=[])
    await state.set_state(HealthFSM.item_cat)

    kb = category_multiselect_kb(set())
    data = await state.get_data()
    mid = data.get("panel_mid")
    prompt = f"<b>{app.esc(name)}</b> uchun toifalarni tanlang (bir nechta tanlash mumkin):"
    await _edit_or_send(message if mid is None else None, prompt, kb)


@router.callback_query(HealthFSM.item_cat, F.data.startswith("hl:togcat:"))
async def cb_toggle_cat(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    data = await state.get_data()
    selected = set(data.get("selected_cats") or [])
    if cid in selected:
        selected.remove(cid)
    else:
        selected.add(cid)
    await state.update_data(selected_cats=sorted(list(selected)))
    await cb.answer()
    name = data.get("item_name", "Dori")
    prompt = f"<b>{app.esc(name)}</b> uchun toifalarni tanlang (bir nechta tanlash mumkin):"
    kb = category_multiselect_kb(selected)
    await _edit_or_send(cb.message, prompt, kb)


@router.callback_query(HealthFSM.item_cat, F.data == "hl:cat_done")
async def cb_cat_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_cats") or [])
    if not selected:
        await cb.answer("Kamida 1 ta kategoriya tanlang!", show_alert=True)
        return
    await cb.answer()
    await state.set_state(HealthFSM.item_loc)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🏠 Uyda (Aptechka)", "hl:setloc:home"), btn("🎒 Sumkada", "hl:setloc:bag")],
        [btn("❌ Bekor qilish", "hl:home")],
    ])
    await _edit_or_send(cb.message, "Vosita qayerda saqlanadi?", kb)


@router.callback_query(HealthFSM.item_cat, F.data.startswith("hl:setcat:"))
async def cb_choose_cat(cb: CallbackQuery, state: FSMContext):
    cat_id = int(cb.data.split(":")[2])
    await cb.answer()
    await state.update_data(item_cat=cat_id, selected_cats=[cat_id])
    await state.set_state(HealthFSM.item_loc)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🏠 Uyda (Aptechka)", "hl:setloc:home"), btn("🎒 Sumkada", "hl:setloc:bag")],
        [btn("❌ Bekor qilish", "hl:home")],
    ])
    await _edit_or_send(cb.message, "Vosita qayerda saqlanadi?", kb)


@router.callback_query(HealthFSM.item_loc, F.data.startswith("hl:setloc:"))
async def cb_choose_loc(cb: CallbackQuery, state: FSMContext):
    loc = cb.data.split(":")[2]
    await cb.answer()
    await state.update_data(item_loc=loc)
    await state.set_state(HealthFSM.item_form)

    forms = [
        ("Tabletka", "tabletka"), ("Kapsula", "kapsula"),
        ("Sirop", "sirop"), ("Maz / Krem", "maz"),
        ("Sprey", "sprey"), ("Tomchi", "tomchi"),
        ("Boshqa", "boshqa"),
    ]
    rows = []
    for i in range(0, len(forms), 2):
        row = [btn(forms[i][0], f"hl:setform:{forms[i][1]}")]
        if i + 1 < len(forms):
            row.append(btn(forms[i + 1][0], f"hl:setform:{forms[i + 1][1]}"))
        rows.append(row)
    rows.append([btn("❌ Bekor qilish", "hl:home")])
    await _edit_or_send(cb.message, "Dori/vosita shaklini tanlang:", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(HealthFSM.item_form, F.data.startswith("hl:setform:"))
async def cb_choose_form(cb: CallbackQuery, state: FSMContext):
    form = cb.data.split(":")[2]
    await cb.answer()
    await state.update_data(item_form=form)
    await state.set_state(HealthFSM.item_dosage)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("O'tkazib yuborish (1 dona)", "hl:skip_dose")],
        [btn("❌ Bekor qilish", "hl:home")],
    ])
    await _edit_or_send(cb.message, "Standart doza qancha? (masalan: <code>1 tabletka</code> yoki <code>1 qoshiq</code>):", kb)


@router.callback_query(HealthFSM.item_dosage, F.data == "hl:skip_dose")
async def cb_skip_dosage(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(item_dosage="1 dona")
    await state.set_state(HealthFSM.item_qty)
    await _edit_or_send(cb.message, "Hozirda nechta bor? (raqam kiriting, masalan: <code>10</code>):", None)


@router.message(HealthFSM.item_dosage)
async def process_dosage_msg(message: Message, state: FSMContext):
    dosage = (message.text or "").strip()
    await state.update_data(item_dosage=dosage)
    await state.set_state(HealthFSM.item_qty)
    await messenger.send(with_footer("Hozirda nechta bor? (raqam kiriting, masalan: <code>10</code>):"), "info")


@router.message(HealthFSM.item_qty)
async def process_qty_msg(message: Message, state: FSMContext):
    try:
        qty = float((message.text or "").strip().replace(",", "."))
        if qty < 0:
            raise ValueError
    except ValueError:
        await messenger.send("⚠️ Iltimos, musbat son kiriting (masalan: 10):", "info")
        return
    await state.update_data(item_qty=qty)
    await state.set_state(HealthFSM.item_source)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🏠 Uyda bor zaxira (0 so'm)", "hl:src:home")],
        [btn("🛒 Yangi sotib oldim (Moliya)", "hl:src:buy")],
        [btn("❌ Bekor qilish", "hl:home")],
    ])
    await messenger.send(with_footer("Ushbu dori qanday kiritilmoqda?"), "info", kb)


@router.callback_query(HealthFSM.item_source, F.data == "hl:src:home")
async def cb_source_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await _save_health_item(data, price=0, account_id=None)
    await state.clear()
    await _edit_or_send(
        cb.message,
        f"✅ <b>{app.esc(data['item_name'])}</b> muvaffaqiyatli uy aptechkasiga qo'shildi! (Moliya balansiga ta'sir qilmadi).",
        InlineKeyboardMarkup(inline_keyboard=[[btn("💊 Aptechkaga o'tish", "hl:ap:home:0")]]),
    )


@router.callback_query(HealthFSM.item_source, F.data == "hl:src:buy")
async def cb_source_buy(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(HealthFSM.item_price)
    await _edit_or_send(cb.message, "Dorining narxini so'mda kiriting (masalan: <code>15000</code> yoki <code>15k</code>):", None)


@router.message(HealthFSM.item_price)
async def process_buy_price(message: Message, state: FSMContext):
    txt = (message.text or "").strip().lower().replace("k", "000").replace(" ", "")
    try:
        price = float(txt)
        if price <= 0:
            raise ValueError
    except ValueError:
        await messenger.send("⚠️ Narxni to'g'ri son shaklida kiriting:", "info")
        return
    await state.update_data(item_price=price)
    await state.set_state(HealthFSM.item_account)

    accs = db.q("SELECT id, name, type, balance FROM finance_accounts WHERE is_active = 1")
    if not accs:
        # Hisob yo'q bo'lsa to'g'ridan-to'g'ri saqlaymiz
        data = await state.get_data()
        await _save_health_item(data, price=price, account_id=None)
        await state.clear()
        await messenger.send(with_footer(f"✅ <b>{app.esc(data['item_name'])}</b> saqlandi!"), "info")
        return

    rows = []
    for a in accs:
        rows.append([btn(f"💳 {a['name']} ({fmt_sum(a['balance'])} so'm)", f"hl:payacc:{a['id']}")])
    rows.append([btn("❌ Bekor qilish", "hl:home")])
    await messenger.send(with_footer("Qaysi hisobdan to'landi?"), "info", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(HealthFSM.item_account, F.data.startswith("hl:payacc:"))
async def cb_buy_pay_acc(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split(":")[2])
    await cb.answer()
    data = await state.get_data()
    price = data.get("item_price", 0)
    await _save_health_item(data, price=price, account_id=acc_id)
    await state.clear()
    await _edit_or_send(
        cb.message,
        f"✅ <b>{app.esc(data['item_name'])}</b> qo'shildi va moliyadan <b>{fmt_sum(price)} so'm</b> (Sport/Sog'liq) yechildi!",
        InlineKeyboardMarkup(inline_keyboard=[[btn("💊 Aptechkaga o'tish", "hl:ap:home:0")]]),
    )


async def _save_health_item(data: dict, price: float = 0, account_id: int | None = None) -> int:
    name = data["item_name"]
    selected_cats = data.get("selected_cats") or []
    cat_id = selected_cats[0] if selected_cats else data.get("item_cat")
    loc = data.get("item_loc", "home")
    form = data.get("item_form", "tabletka")
    dosage = data.get("item_dosage", "1 dona")
    qty = float(data.get("item_qty", 10.0))
    unit = "dona"

    item_id = db.ins(
        "INSERT INTO health_items (name, kind, category_id, location, form, default_dosage, remaining_quantity, unit, low_stock_threshold, is_seed) "
        "VALUES (?, 'medicine', ?, ?, ?, ?, ?, ?, 2.0, 0)",
        (name, cat_id, loc, form, dosage, qty, unit),
    )
    if selected_cats:
        for cid in selected_cats:
            db.ex("INSERT INTO health_item_categories (item_id, category_id) VALUES (?, ?)", (item_id, cid))
    elif cat_id:
        db.ex("INSERT INTO health_item_categories (item_id, category_id) VALUES (?, ?)", (item_id, cat_id))

    if price > 0 and account_id:
        if "finance" in app.OPTIONAL:
            fin = app.OPTIONAL["finance"]
            cat_row = db.q1("SELECT id FROM finance_categories WHERE name = ? OR name = ?", ("Sport/Sog'liq", "Sport / Sog'liq"))
            fin_cat_id = cat_row["id"] if cat_row else None
            fin.record_transaction(
                price,
                "Sport / Sog'liq",
                f"Dorixona xaridi: {name}",
                from_account_id=account_id,
                category_id=fin_cat_id,
                linked_kind="health",
                linked_id=item_id,
            )
    return item_id


# ------------------------------------------------------------------ 3. Davolanish Kursi (Courses & Scheduler)

def courses_view() -> tuple[str, InlineKeyboardMarkup]:
    today_iso = app.today().isoformat()
    # Auto-complete expired courses
    db.ex("UPDATE health_courses SET is_active = 0 WHERE is_active = 1 AND end_date < ?", (today_iso,))

    courses = db.q("SELECT * FROM health_courses ORDER BY is_active DESC, end_date DESC")
    lines = ["🩺 <b>Davolanish Kurslari</b>\n"]
    if not courses:
        lines.append("Hozircha hech qanday davolanish kursi kiritilmagan.")
    else:
        for c in courses:
            status_s = "🟢 Faol" if c["is_active"] else "⚪️ Yakunlangan"
            items = db.q("SELECT * FROM health_course_items WHERE course_id = ? AND is_active = 1", (c["id"],))
            lines.append(f"• <b>{app.esc(c['title'])}</b> ({status_s})")
            lines.append(f"  Muddat: {c['start_date']} – {c['end_date']}")
            if c.get("doctor_notes"):
                lines.append(f"  👨‍⚕️ <i>{app.esc(c['doctor_notes'])}</i>")
            for it in items:
                lines.append(f"  💊 {app.esc(it['item_name'])}: {it['schedule_times']} ({it['relation_to_food']})")
            lines.append("")

    kb_rows = [
        [btn("➕ Yangi kurs qo'shish", "hl:add_cr")],
        [btn("⬅️ Salomatlik markazi", "hl:home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "hl:cr:home")
async def cb_courses_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = courses_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "hl:add_cr")
async def cb_start_course(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(HealthFSM.course_title)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "hl:cr:home")]])
    await _edit_or_send(cb.message, "🩺 <b>Yangi davolanish kursi</b>\n\nKurs nomini kiriting (masalan: <i>Kardiolog tavsiyasi</i>):", kb)


@router.message(HealthFSM.course_title)
async def process_cr_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    await state.update_data(course_title=title)
    await state.set_state(HealthFSM.course_days)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("5 kun", "hl:crdays:5"), btn("7 kun", "hl:crdays:7"), btn("10 kun", "hl:crdays:10")],
        [btn("14 kun", "hl:crdays:14"), btn("30 kun", "hl:crdays:30")],
        [btn("❌ Bekor qilish", "hl:cr:home")],
    ])
    await messenger.send(with_footer("Kurs davomiyligini tanlang:"), "info", kb)


@router.callback_query(HealthFSM.course_days, F.data.startswith("hl:crdays:"))
async def cb_cr_days(cb: CallbackQuery, state: FSMContext):
    days = int(cb.data.split(":")[2])
    await cb.answer()
    await state.update_data(course_days=days)
    await state.set_state(HealthFSM.course_med_name)
    await _edit_or_send(cb.message, "Dori nomini kiriting (masalan: <code>Amoksitsillin</code>):", None)


@router.message(HealthFSM.course_med_name)
async def process_cr_med_name(message: Message, state: FSMContext):
    med = (message.text or "").strip()
    await state.update_data(course_med_name=med)
    await state.set_state(HealthFSM.course_med_times)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("Kuniga 1 marta (09:00)", "hl:crtimes:09:00")],
        [btn("Kuniga 2 marta (09:00, 21:00)", "hl:crtimes:09:00, 21:00")],
        [btn("Kuniga 3 marta (08:00, 14:00, 20:00)", "hl:crtimes:08:00, 14:00, 20:00")],
    ])
    await messenger.send(with_footer("Dori ichish vaqtlarini tanlang yoki qo'lda kiriting (masalan: <code>08:00, 20:00</code>):"), "info", kb)


@router.callback_query(HealthFSM.course_med_times, F.data.startswith("hl:crtimes:"))
async def cb_cr_times_select(cb: CallbackQuery, state: FSMContext):
    times_s = cb.data[len("hl:crtimes:"):]
    await cb.answer()
    await state.update_data(course_med_times=times_s)
    await state.set_state(HealthFSM.course_med_food)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("Ovqatdan oldin", "hl:crfood:ovqatdan oldin"), btn("Ovqat bilan", "hl:crfood:ovqat bilan")],
        [btn("Ovqatdan keyin", "hl:crfood:ovqatdan keyin")],
    ])
    await _edit_or_send(cb.message, "Ovqatlanishga nisbatan qachon ichiladi?", kb)


@router.message(HealthFSM.course_med_times)
async def process_cr_times_msg(message: Message, state: FSMContext):
    times_s = (message.text or "").strip()
    await state.update_data(course_med_times=times_s)
    await state.set_state(HealthFSM.course_med_food)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("Ovqatdan oldin", "hl:crfood:ovqatdan oldin"), btn("Ovqat bilan", "hl:crfood:ovqat bilan")],
        [btn("Ovqatdan keyin", "hl:crfood:ovqatdan keyin")],
    ])
    await messenger.send(with_footer("Ovqatlanishga nisbatan qachon ichiladi?"), "info", kb)


@router.callback_query(HealthFSM.course_med_food, F.data.startswith("hl:crfood:"))
async def cb_cr_food(cb: CallbackQuery, state: FSMContext):
    food_rel = cb.data[len("hl:crfood:"):]
    await cb.answer()
    data = await state.get_data()
    title = data["course_title"]
    days = data.get("course_days", 7)
    med = data["course_med_name"]
    times = data["course_med_times"]

    today = app.today()
    end_date = today + timedelta(days=days)

    cid = db.ins(
        "INSERT INTO health_courses (title, doctor_notes, start_date, end_date, is_active) VALUES (?, ?, ?, ?, 1)",
        (title, "Shifokor ko'rsatmasi bo'yicha", today.isoformat(), end_date.isoformat()),
    )
    ci_id = db.ins(
        "INSERT INTO health_course_items (course_id, item_name, schedule_times, dose_quantity, dose_unit, relation_to_food) "
        "VALUES (?, ?, ?, 1, 'tabletka', ?)",
        (cid, med, times, food_rel),
    )
    await state.clear()
    schedule_course_reminders()

    await _edit_or_send(
        cb.message,
        f"🩺 <b>Davolanish kursi yaratildi!</b>\n\n"
        f"• Kurs: <b>{app.esc(title)}</b> ({days} kun)\n"
        f"• Dori: <b>{app.esc(med)}</b>\n"
        f"• Vaqtlari: <code>{times}</code> ({food_rel})\n"
        f"Eslatmalar avtomatik o'rnatildi.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("🩺 Kurslar ro'yxati", "hl:cr:home")]]),
    )


# Course dose reminder job execution
async def send_course_dose_reminder(item_id: int):
    item = db.q1(
        "SELECT ci.*, c.title AS course_title "
        "FROM health_course_items ci JOIN health_courses c ON c.id = ci.course_id "
        "WHERE ci.id = ? AND ci.is_active = 1 AND c.is_active = 1",
        (item_id,),
    )
    if not item:
        return

    text = (
        f"🩺 <b>DORI VAQTI!</b>\n\n"
        f"Kurs: <b>{app.esc(item['course_title'])}</b>\n"
        f"💊 Dori: <b>{app.esc(item['item_name'])}</b> ({item['dose_quantity']} {item['dose_unit']})\n"
        f"🍽 <b>{app.esc(item['relation_to_food'])}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ Ichdim", f"hl:cr_d:{item_id}"), btn("⏰ 15 daqiqa keyin", f"hl:cr_post:{item_id}")],
    ])
    await messenger.send(with_footer(text), "reminder", kb, actionable=True, bind_trigger=False)


@router.callback_query(F.data.startswith("hl:cr_d:"))
async def cb_course_drank(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    item = db.q1("SELECT * FROM health_course_items WHERE id = ?", (item_id,))
    if item:
        db.ins(
            "INSERT INTO health_intake_logs (item_id, course_id, quantity, status, taken_at) VALUES (NULL, ?, ?, 'taken', ?)",
            (item["course_id"], item["dose_quantity"], app.now().isoformat()),
        )
    await cb.answer("Qabul qayd etildi!")
    await cb.message.edit_text(with_footer("✅ Dori qabul qilindi. Shifo bo'lsin!"))


@router.callback_query(F.data.startswith("hl:cr_post:"))
async def cb_course_postpone(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[2])
    target_dt = app.now() + timedelta(minutes=15)
    app.schedule_job(target_dt, send_course_dose_reminder, [item_id], f"c_dose_post_{item_id}_{int(app.now().timestamp())}")
    await cb.answer("15 daqiqaga kechiktirildi")
    await cb.message.edit_text(with_footer(f"⏰ Eslatma 15 daqiqaga (soat {target_dt.strftime('%H:%M')} ga) kechiktirildi."))


def schedule_course_reminders():
    """Faol kurslarning bugungi eslatmalarini rejalashtiradi."""
    try:
        today = app.today()
        active_items = db.q(
            "SELECT ci.* FROM health_course_items ci JOIN health_courses c ON c.id = ci.course_id "
            "WHERE ci.is_active = 1 AND c.is_active = 1 AND c.start_date <= ? AND c.end_date >= ?",
            (today.isoformat(), today.isoformat()),
        )
        for it in active_items:
            raw_times = it["schedule_times"].split(",")
            for t_str in raw_times:
                t_clean = t_str.strip()
                if not t_clean:
                    continue
                try:
                    dt = app.at(today, t_clean)
                    if dt > app.now():
                        app.schedule_job(dt, send_course_dose_reminder, [it["id"]], f"c_dose_{it['id']}_{t_clean.replace(':', '')}")
                except Exception:
                    pass
    except Exception:
        pass


# ------------------------------------------------------------------ 4. Virtual Dorixonachi (Triage Engine)

TRIAGE_SELECTED: dict[int, set[str]] = {}  # user/chat session selected symptom codes


def triage_selector_view(selected: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    rules = db.q("SELECT * FROM health_triage_rules ORDER BY is_red_flag DESC, id ASC")
    lines = [
        "👨‍⚕️ <b>Virtual Dorixonachi (Triage)</b>\n",
        "O'zingizdagi alomatlarni belgilang (bir nechta tanlash mumkin):",
    ]

    kb_rows = []
    for r in rules:
        code = r["symptom_code"]
        check = "✅" if code in selected else "⬜"
        flag = "🚨 " if r["is_red_flag"] else ""
        label = f"{check} {flag}{r['symptom_name'][:28]}"
        kb_rows.append([btn(label, f"hl:tr_tog:{code}")])

    action_row = []
    if selected:
        action_row.append(btn("🔍 Tahlil qilish", "hl:tr_run"))
        action_row.append(btn("🔄 Tozalash", "hl:tr_clear"))
    action_row.append(btn("⬅️ Orqaga", "hl:home"))
    kb_rows.append(action_row)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(F.data == "hl:tr:home")
async def cb_triage_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    TRIAGE_SELECTED[cb.from_user.id] = set()
    text, kb = triage_selector_view(set())
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("hl:tr_tog:"))
async def cb_triage_toggle(cb: CallbackQuery):
    code = cb.data.split(":")[2]
    user_id = cb.from_user.id
    selected = TRIAGE_SELECTED.setdefault(user_id, set())
    if code in selected:
        selected.remove(code)
    else:
        selected.add(code)
    await cb.answer()
    text, kb = triage_selector_view(selected)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "hl:tr_clear")
async def cb_triage_clear(cb: CallbackQuery):
    TRIAGE_SELECTED[cb.from_user.id] = set()
    await cb.answer("Tozalandi")
    text, kb = triage_selector_view(set())
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "hl:tr_run")
async def cb_triage_run(cb: CallbackQuery):
    user_id = cb.from_user.id
    selected = TRIAGE_SELECTED.get(user_id, set())
    if not selected:
        await cb.answer("Hech qanday alomat belgilanmadi!", show_alert=True)
        return

    rules = db.q(
        f"SELECT * FROM health_triage_rules WHERE symptom_code IN ({','.join('?' for _ in selected)})",
        tuple(selected),
    )
    has_red_flag = any(r["is_red_flag"] for r in rules)

    if has_red_flag:
        # QAT'IY QIZIL BAYROQ QOIDASI: Dori taklif qilinmaydi, darhol 103!
        red_rules = [r for r in rules if r["is_red_flag"]]
        lines = [
            "🚨 <b>SHOSHILINCH XAVF BELGISI ANIQLANDI!</b>\n",
            "Siz belgilagan alomat(lar) jiddiy xavf tug'dirishi mumkin. Zudlik bilan <b>103 Tez tibbiy yordam</b>ga qo'ng'iroq qiling yoki eng yaqin shifoxonaga boring!\n",
            "🚑 <b>Birinchi tez yordam choralari:</b>",
        ]
        for rr in red_rules:
            lines.append(f"• <b>{app.esc(rr['symptom_name'])}:</b> {app.esc(rr['natural_remedies'])}")
        lines.append("\n⛔ <b>HECH QANDAY DORI ICHMANG!</b> Shifokor ko'rigi zarur.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Alomatlar ro'yxati", "hl:tr:home")]])
        await cb.answer()
        await _edit_or_send(cb.message, "\n".join(lines), kb)
        return

    # YENGIL ALOMATLAR (3 bosqichli tahlil)
    lines = [
        "👨‍⚕️ <b>Virtual Dorixonachi Tahlili</b>\n",
        "🌿 <b>1. Tabiiy uy vositalari va dam olish:</b>",
    ]
    for r in rules:
        if r.get("natural_remedies"):
            lines.append(f"• <b>{app.esc(r['symptom_name'])}:</b> {app.esc(r['natural_remedies'])}")

    # 2. Uyda bor mos dorilar
    lines.append("\n🏠 <b>2. Sizning aptechkangizda mavjud mos vositalar:</b>")
    user_meds = db.q("SELECT * FROM health_items WHERE is_active = 1 AND remaining_quantity > 0")
    matching_meds = []
    otc_recommendations = []
    for r in rules:
        rec_otc = r.get("recommended_otc") or ""
        if rec_otc:
            otc_recommendations.append(rec_otc)
        for um in user_meds:
            for word in rec_otc.lower().split():
                if len(word) >= 4 and word in um["name"].lower():
                    if um not in matching_meds:
                        matching_meds.append(um)

    if matching_meds:
        for mm in matching_meds:
            loc_s = "Uyda" if mm["location"] == "home" else "Sumkada"
            lines.append(f"• <b>{app.esc(mm['name'])}</b> ({loc_s}, qoldiq: {fmt_qty(mm['remaining_quantity'])} {mm['unit']}) — doza: {mm['default_dosage']}")
    else:
        lines.append("• Aptechkangizda mos vosita topilmadi.")

    # 3. Retseptsiz (OTC) tavsiya
    lines.append("\n💊 <b>3. Tavsiya etiladigan retseptsiz (OTC) vositalar:</b>")
    for otc in set(otc_recommendations):
        lines.append(f"• {app.esc(otc)}")

    kb_rows = []
    if matching_meds:
        kb_rows.append([btn(f"💊 {matching_meds[0]['name'][:20]} ichdim", f"hl:drk:{matching_meds[0]['id']}")])
    kb_rows.append([btn("🔄 Boshqa alomatlar", "hl:tr:home"), btn("⬅️ Salomatlik", "hl:home")])
    await cb.answer()
    await _edit_or_send(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


# ------------------------------------------------------------------ 5. Skin Care & Tonggi Gigiyena

def skincare_view() -> tuple[str, InlineKeyboardMarkup]:
    items = db.q("SELECT * FROM health_items WHERE kind = 'skincare' AND is_active = 1 AND remaining_quantity > 0")
    lines = ["🧴 <b>Skin Care & Shaxsiy Parvarish javoni</b>\n"]
    if not items:
        lines.append("Parvarish javoningizda hozircha vosita yo'q.")
    else:
        for it in items:
            lines.append(f"• <b>{app.esc(it['name'])}</b>: {app.esc(it['notes'] or it['default_dosage'])}")

    lines.append("\n🧼 <b>Kundalik parvarish tartibi:</b>")
    lines.append("1. Tongda: Iliq suv + Yumshoq gel bilan Yuvinish, namlantiruvchi krem.")
    lines.append("2. Kunduzi: Quyoshga chiqishdan oldin SPF 30+ surtish.")
    lines.append("3. Kechqurun: Yuzni tozalash va tiklovchi krem.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🌅 Tonggi gigiyena eslatmasi", "hl:hyg_prompt")],
        [btn("⬅️ Salomatlik markazi", "hl:home")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "hl:sk:home")
async def cb_skincare_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = skincare_view()
    # Ob-havoga mos tavsiyani qo'shish
    weather_advice = await get_skincare_weather_advice()
    await _edit_or_send(cb.message, text + weather_advice, kb)


async def get_skincare_weather_advice() -> str:
    try:
        data = await asyncio.to_thread(sv.get_weather_data, False)
        if not data:
            return ""
        info = sv.analyze_weather_for_date(data["raw"], app.today())
        if not info:
            return ""
        advice = []
        cur = info.get("current")
        feels = cur["feels"] if cur else info.get("feels_min", 10)
        wind = cur["wind"] if cur else info.get("max_wind", 0)

        if feels <= 7 or wind >= 15:
            advice.append("❄️ Sovuq yoki kuchli shamol: lab balzami va namlantiruvchi kremdan foydalaning!")
        if info.get("temp_max", 0) >= 18:
            advice.append("☀️ Quyoshli kun: ko'chaga chiqishdan oldin SPF quyoshdan himoya kremini surting.")
        if advice:
            return "\n\n💡 <b>Ob-havoga mos tavsiya:</b>\n" + "\n".join(advice)
    except Exception:
        pass
    return ""


# Daily Morning Hygiene Reminder (06:45)
async def send_morning_hygiene_reminder(attempt: int = 1):
    today = app.today()
    if db.get_setting(f"hyg_done_{today.isoformat()}"):
        return

    text = (
        "🌅 <b>Xayrli tong! Tonggi gigiyena va parvarish:</b>\n\n"
        "1. Tishlarni tozalash 🪥\n"
        "2. Yuzni yumshoq yuvish va namlantirish 🧴\n"
        "3. Gigiyenik lab balzami va tartib ✨"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Bajarildi", "hl:hyg_done")]])
    await messenger.send(with_footer(text), "reminder", kb, actionable=True, bind_trigger=False)

    # 20 daqiqadan keyin qayta eslatish (maksimal 2 marta)
    if attempt < 3:
        next_dt = app.now() + timedelta(minutes=20)
        app.schedule_job(next_dt, send_morning_hygiene_reminder, [attempt + 1], f"hyg_rep_{attempt}")


@router.callback_query(F.data == "hl:hyg_prompt")
async def cb_hyg_prompt(cb: CallbackQuery):
    await cb.answer()
    await send_morning_hygiene_reminder(attempt=1)


@router.callback_query(F.data == "hl:hyg_done")
async def cb_hyg_done(cb: CallbackQuery):
    today = app.today()
    db.set_setting(f"hyg_done_{today.isoformat()}", "1")
    # Cancel repeat jobs
    for i in (1, 2):
        job = app.scheduler.get_job(f"hyg_rep_{i}")
        if job:
            job.remove()
    await cb.answer("Tonggi gigiyena bajarildi! Barakalla!", show_alert=True)
    await cb.message.edit_text(with_footer("✅ <b>Tonggi gigiyena va parvarish bajarildi.</b> Kunningiz unumli o'tsin!"))


# ------------------------------------------------------------------ 6. "Sog'lom Hafta" Reytingi

def get_weekly_health_stats(ref_date: date | None = None) -> dict:
    d = ref_date or app.today()
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)

    # Sleep average
    mon_iso = datetime(monday.year, monday.month, monday.day, 0, 0, tzinfo=app.TZ).isoformat()
    sun_iso = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59, tzinfo=app.TZ).isoformat()
    sleep_rows = db.q(
        "SELECT duration_min FROM sleep_log WHERE wake_at >= ? AND wake_at <= ? AND duration_min IS NOT NULL",
        (mon_iso, sun_iso),
    )
    sleep_cnt = len(sleep_rows)
    avg_sleep = round(sum(r["duration_min"] for r in sleep_rows) / sleep_cnt) if sleep_cnt else 0

    # Hygiene compliance
    hyg_days = 0
    cur_d = monday
    while cur_d <= min(sunday, app.today()):
        if db.get_setting(f"hyg_done_{cur_d.isoformat()}"):
            hyg_days += 1
        cur_d += timedelta(days=1)

    # Sick / Intake days
    sick_days_cnt = db.q1(
        "SELECT COUNT(DISTINCT DATE(taken_at)) AS c FROM health_intake_logs WHERE taken_at >= ? AND taken_at <= ?",
        (mon_iso, sun_iso),
    )["c"]

    # Sports occurrences completed
    sport_done = db.q1(
        "SELECT COUNT(*) AS c FROM occurrences WHERE date >= ? AND date <= ? AND status = 'done' "
        "AND (LOWER(title) LIKE '%sport%' OR LOWER(title) LIKE '%zal%' OR LOWER(title) LIKE '%mashq%' OR LOWER(title) LIKE '%yugurish%')",
        (monday.isoformat(), sunday.isoformat()),
    )["c"]

    score = 70
    if avg_sleep >= 420:  # >= 7 soat
        score += 15
    elif avg_sleep >= 360:
        score += 5
    score += min(15, hyg_days * 2)

    return {
        "monday": monday,
        "sunday": sunday,
        "avg_sleep_min": avg_sleep,
        "hyg_days": hyg_days,
        "sick_days": sick_days_cnt,
        "sport_done": sport_done,
        "score": min(100, score),
    }


def render_weekly_health_report(ref_date: date | None = None) -> str:
    s = get_weekly_health_stats(ref_date)
    h = s["avg_sleep_min"] // 60
    m = s["avg_sleep_min"] % 60
    lines = [
        f"🏥 <b>«Sog'lom Hafta» Salomatlik Reytingi ({s['monday'].strftime('%d.%m')} – {s['sunday'].strftime('%d.%m')})</b>\n",
        f"⭐ <b>Salomatlik indeksi:</b> <b>{s['score']}/100 ball</b>",
        f"😴 <b>O'rtacha uyqu:</b> {h} soat {m} daqiqa",
        f"🪥 <b>Tonggi gigiyena:</b> {s['hyg_days']}/7 kun bajarildi",
        f"🏋️ <b>Bajarilgan sport mashg'ulotlari:</b> {s['sport_done']} ta",
    ]
    if s["sick_days"] > 0:
        lines.append(f"💊 <b>Kasallik/dori qabul qilingan kunlar:</b> {s['sick_days']} kun (sport jarimasi bekor qilingan)")
    else:
        lines.append("✨ <b>Ushbu haftada kasallik alomatlari qayd etilmadi!</b>")
    return "\n".join(lines)


@router.callback_query(F.data == "hl:week_rep")
async def cb_health_week_rep(cb: CallbackQuery):
    await cb.answer()
    text = render_weekly_health_report()
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Salomatlik", "hl:home")]])
    await _edit_or_send(cb.message, text, kb)


# ------------------------------------------------------------------ 7. Tizim Diagnostikasi (/diagnostika)

def render_diagnostics() -> str:
    uptime = app.now() - app.STARTED_AT
    days = uptime.days
    hours = uptime.seconds // 3600
    minutes = (uptime.seconds % 3600) // 60
    uptime_s = f"{days} kun, {hours} soat, {minutes} daqiqa" if days else f"{hours} soat, {minutes} daqiqa"

    import os
    db_p = db.path()
    db_size = os.path.getsize(db_p) if os.path.exists(db_p) else 0
    if db_size >= 1024 * 1024:
        db_size_s = f"{round(db_size / (1024 * 1024), 2)} MB"
    else:
        db_size_s = f"{round(db_size / 1024, 1)} KB"

    jobs_cnt = len(app.scheduler.get_jobs())
    h24 = (app.now() - timedelta(hours=24)).isoformat()
    err_cnt = db.q1("SELECT COUNT(*) AS c FROM system_error_logs WHERE occurred_at >= ?", (h24,))["c"]

    lines = [
        "🛠 <b>AltronBek Tizim Diagnostikasi (v3.7)</b>\n",
        f"⏱ <b>Uptime:</b> {uptime_s}",
        f"💾 <b>Baza hajmi:</b> {db_size_s}",
        f"⏰ <b>Faol scheduler joblar:</b> {jobs_cnt} ta",
        f"⚠️ <b>Oxirgi 24 soatdagi xatoliklar:</b> {err_cnt} ta",
    ]
    return "\n".join(lines)


@router.message(Command("diagnostika"))
async def cmd_diagnostics(message: Message):
    text = render_diagnostics()
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🔄 Yangilash", "hl:diag:ref")]])
    await messenger.send(text, "info", kb)


@router.callback_query(F.data == "hl:diag")
@router.callback_query(F.data == "hl:diag:ref")
async def cb_diag(cb: CallbackQuery):
    await cb.answer()
    text = render_diagnostics()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔄 Yangilash", "hl:diag:ref"), btn("⬅️ Salomatlik", "hl:home")],
    ])
    await _edit_or_send(cb.message, text, kb)


def register_jobs(scheduler):
    # Har tong 06:45 da tonggi gigiyena eslatmasi
    scheduler.add_job(send_morning_hygiene_reminder, CronTrigger(hour=6, minute=45), id="morning_hygiene", replace_existing=True)
    # Davolanish kurslari jadvali
    schedule_course_reminders()
