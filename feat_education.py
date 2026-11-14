"""
AltronBek 3.8 — "Maktab Ta'lim Moduli (O'qituvchi Paneli)" (feat_education.py)

Quyidagi imkoniyatlarni taqdim etadi:
1. Dinamik sinflar va fanlar (CRUD)
2. Excel (.xlsx) TMR (Taqvim-mavzu reja) import + Auto-tagging ('regular', 'lab', 'exam')
3. Joriy mavzu Smart Pointer (ko'rsatkichi) va uni istalgan payt siljitish
4. Haftalik dars jadvali (1-soatdan 6-soatgacha, Dushanbadan Shanbagacha)
5. Sinf izohlari (keyingi dars uchun maxsus eslatmalar va ularni yopish)
6. 2026-2027 Ta'til va bayramlar taqvimi (dam olish kunlarida dars eslatmalari avtomatik to'xtatiladi)
7. Aqlli dars eslatmalari:
   - 1 kun oldin (20:00 da) ertangi reja + Laboratoriya/Nazorat ogohlantirishlari
   - Dars boshlanishidan 10 daqiqa oldin eslatma + sinf izohlari
   - Dars tugashida tasdiqlash ([✅ O'tildi] / [⏭ O'tilmadi] / [📝 Izoh])
"""

import io
import re
from datetime import date, datetime, timedelta
import xml.etree.ElementTree as ET
import zipfile

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

# Standart qo'ng'iroqlar jadvali (soat -> (boshlanish, tugash))
EDU_BELLS: dict[int, tuple[str, str]] = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("09:50", "10:35"),
    4: ("10:50", "11:35"),
    5: ("11:45", "12:30"),
    6: ("12:40", "13:25"),
}

WEEKDAY_NAMES_UZ = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba"]
PAGE_SIZE = 8


class EduFSM(StatesGroup):
    add_class_name = State()
    add_subject_name = State()
    upload_excel = State()
    select_current_topic = State()
    add_note = State()
    add_off_title = State()
    add_off_start = State()
    add_off_end = State()
    add_off_type = State()


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _edit_or_send(target_msg, text: str, kb: InlineKeyboardMarkup | None):
    markup = messenger.with_delete(kb)
    mid = getattr(target_msg, "message_id", None)
    if mid is not None:
        try:
            await app.bot.edit_message_text(
                text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=markup
            )
            return mid
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return mid
        except Exception:
            pass
    sent = await messenger.send(text, "info", kb, bind_trigger=False)
    return sent.message_id if sent else None


# ------------------------------------------------------------------ 1. Ta'til va Bayramlar (Off-Days)

def is_off_day(d: date) -> tuple[bool, str | None, str | None]:
    """Qaytadi: (is_off, title, off_type)"""
    iso = d.isoformat()
    row = db.q1(
        "SELECT title, off_type FROM edu_calendar_off_days WHERE start_date <= ? AND end_date >= ? LIMIT 1",
        (iso, iso),
    )
    if row:
        return True, row["title"], row["off_type"]
    return False, None, None


def get_all_off_days() -> list[dict]:
    return db.q("SELECT * FROM edu_calendar_off_days ORDER BY start_date ASC")


def add_off_day(title: str, start_date: str, end_date: str, off_type: str = "holiday") -> int:
    return db.ins(
        "INSERT INTO edu_calendar_off_days (title, start_date, end_date, off_type) VALUES (?, ?, ?, ?)",
        (title.strip(), start_date.strip(), end_date.strip(), off_type.strip()),
    )


def delete_off_day(off_id: int):
    db.ex("DELETE FROM edu_calendar_off_days WHERE id = ?", (off_id,))


# ------------------------------------------------------------------ 2. Excel TMR Parser & Auto-Tagging

def _parse_xlsx_fallback(file_bytes: bytes) -> list[list]:
    """Agar openpyxl mavjud bo'lmasa, zipfile + xml orqali .xlsx birinchi varag'ini o'qiydi."""
    zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    shared_strings = []
    if "xl/sharedStrings.xml" in zf.namelist():
        tree = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
            text_nodes = si.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
            shared_strings.append("".join(t.text or "" for t in text_nodes))

    sheet_xml = zf.read("xl/worksheets/sheet1.xml")
    stree = ET.fromstring(sheet_xml)
    rows_data = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    for row in stree.findall(f".//{ns}row"):
        row_vals = []
        for cell in row.findall(f".//{ns}c"):
            cell_type = cell.attrib.get("t")
            v_node = cell.find(f".//{ns}v")
            val = v_node.text if v_node is not None else None
            if cell_type == "s" and val is not None and val.isdigit():
                idx = int(val)
                val = shared_strings[idx] if idx < len(shared_strings) else val
            row_vals.append(val)
        if any(v is not None and str(v).strip() for v in row_vals):
            rows_data.append(row_vals)
    return rows_data


def parse_tmr_excel(file_bytes: bytes) -> list[dict]:
    """
    Excel (.xlsx) faylni o'qiydi va TMR mavzular ro'yxatini qaytaradi:
    [{'order': 1, 'title': '...', 'homework': '...', 'topic_type': 'regular'|'lab'|'exam'}, ...]
    """
    raw_rows = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
    except Exception:
        try:
            raw_rows = _parse_xlsx_fallback(file_bytes)
        except Exception as e:
            app.log.warning(f"Excel faylni o'qishda xatolik: {e}")
            return []

    if not raw_rows:
        return []

    # Sarlavha qatorini va ustun indekslarini topish
    order_col = 0
    title_col = 1
    hw_col = 2
    data_start_idx = 0

    for r_idx, row in enumerate(raw_rows[:6]):
        str_cells = [str(c).strip().lower() for c in row if c is not None]
        if any(any(k in c for k in ("mavzu", "dars", "t/r", "№", "tartib")) for c in str_cells):
            data_start_idx = r_idx + 1
            for c_idx, cell in enumerate(row):
                if cell is None:
                    continue
                c_str = str(cell).strip().lower()
                if any(k in c_str for k in ("t/r", "№", "tartib", "tartibi")):
                    order_col = c_idx
                elif any(k in c_str for k in ("mavzu", "mavzusi", "mavzular", "nomi")):
                    title_col = c_idx
                elif any(k in c_str for k in ("uy", "vazifa", "topshiriq")):
                    hw_col = c_idx
            break

    topics = []
    auto_order = 1
    for row in raw_rows[data_start_idx:]:
        if not row or not any(row):
            continue
        # Order
        order_val = None
        if len(row) > order_col and row[order_col] is not None:
            clean_s = re.sub(r"[^\d]", "", str(row[order_col]))
            if clean_s:
                try:
                    order_val = int(clean_s)
                except ValueError:
                    order_val = None

        if order_val is None or order_val <= 0:
            order_val = auto_order
        auto_order = max(auto_order, order_val) + 1

        # Title
        title_val = ""
        if len(row) > title_col and row[title_col] is not None:
            title_val = str(row[title_col]).strip()
        elif len(row) > 1 and row[1] is not None:
            title_val = str(row[1]).strip()

        if len(title_val) < 2:
            continue

        # Homework
        hw_val = None
        if len(row) > hw_col and row[hw_col] is not None:
            hw_str = str(row[hw_col]).strip()
            if hw_str and hw_str.lower() not in ("none", "null", "-", "—"):
                hw_val = hw_str

        # Auto-tagging: laboratoriya / amaliy -> lab, nazorat -> exam, boshqa -> regular
        t_low = title_val.lower()
        if "laboratoriya" in t_low or "amaliy" in t_low:
            t_type = "lab"
        elif "nazorat" in t_low:
            t_type = "exam"
        else:
            t_type = "regular"

        topics.append({
            "order": order_val,
            "title": title_val,
            "homework": hw_val,
            "topic_type": t_type,
        })

    topics.sort(key=lambda x: x["order"])
    return topics


# ------------------------------------------------------------------ 3. Kurslar (Sinf + Fan) & TMR Dvigateli

def create_course(class_name: str, subject_name: str) -> int:
    return db.ins(
        "INSERT INTO edu_courses (class_name, subject_name, current_topic_order, is_active) VALUES (?, ?, 1, 1) "
        "ON CONFLICT(class_name, subject_name) DO UPDATE SET is_active = 1",
        (class_name.strip(), subject_name.strip()),
    )


def get_course(course_id: int) -> dict | None:
    return db.q1("SELECT * FROM edu_courses WHERE id = ?", (course_id,))


def get_courses(active_only: bool = True) -> list[dict]:
    where = "WHERE is_active = 1" if active_only else ""
    return db.q(f"SELECT * FROM edu_courses {where} ORDER BY class_name ASC, subject_name ASC")


def delete_course(course_id: int):
    db.ex("UPDATE edu_courses SET is_active = 0 WHERE id = ?", (course_id,))
    db.ex("DELETE FROM edu_timetable WHERE course_id = ?", (course_id,))


def import_course_topics(course_id: int, topics: list[dict]) -> int:
    """Yangi TMR mavzularini kiritadi (eski mavzularni tozalab o'rniga yozadi)."""
    db.ex("DELETE FROM edu_topics WHERE course_id = ?", (course_id,))
    count = 0
    for t in topics:
        db.ex(
            "INSERT INTO edu_topics (course_id, topic_order, title, homework, topic_type, is_completed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (course_id, t["order"], t["title"], t.get("homework"), t.get("topic_type", "regular")),
        )
        count += 1
    db.ex("UPDATE edu_courses SET current_topic_order = 1 WHERE id = ?", (course_id,))
    return count


def set_course_current_topic(course_id: int, target_order: int):
    """
    Smart Pointer: target_order gacha bo'lgan barcha mavzularni is_completed = 1,
    target_order va undan keyingilarini is_completed = 0 qiladi.
    """
    db.ex("UPDATE edu_courses SET current_topic_order = ? WHERE id = ?", (target_order, course_id))
    db.ex(
        "UPDATE edu_topics SET is_completed = 1, completed_at = ? WHERE course_id = ? AND topic_order < ?",
        (app.now().isoformat(), course_id, target_order),
    )
    db.ex(
        "UPDATE edu_topics SET is_completed = 0, completed_at = NULL WHERE course_id = ? AND topic_order >= ?",
        (course_id, target_order),
    )


def mark_topic_completed(course_id: int, topic_order: int) -> int:
    """Mavzuni o'tildi deb belgilaydi va pointer'ni bittaga suradi."""
    db.ex(
        "UPDATE edu_topics SET is_completed = 1, completed_at = ? WHERE course_id = ? AND topic_order = ?",
        (app.now().isoformat(), course_id, topic_order),
    )
    new_order = topic_order + 1
    db.ex("UPDATE edu_courses SET current_topic_order = ? WHERE id = ?", (new_order, course_id))
    return new_order


def get_course_topics(course_id: int) -> list[dict]:
    return db.q("SELECT * FROM edu_topics WHERE course_id = ? ORDER BY topic_order ASC", (course_id,))


# ------------------------------------------------------------------ 4. Sinf Izohlari (Class Notes)

def add_class_note(course_id: int, note_text: str) -> int:
    return db.ins(
        "INSERT INTO edu_class_notes (course_id, note_text, is_resolved, created_at) VALUES (?, ?, 0, ?)",
        (course_id, note_text.strip(), app.now().isoformat()),
    )


def resolve_class_note(note_id: int):
    db.ex("UPDATE edu_class_notes SET is_resolved = 1 WHERE id = ?", (note_id,))


def get_unresolved_notes(course_id: int) -> list[dict]:
    return db.q(
        "SELECT * FROM edu_class_notes WHERE course_id = ? AND is_resolved = 0 ORDER BY id DESC",
        (course_id,),
    )


# ------------------------------------------------------------------ 5. Dars Jadvali (Timetable)

def get_timetable_for_day(weekday: int) -> list[dict]:
    return db.q(
        "SELECT t.id AS slot_id, t.weekday, t.period_number, t.course_id, "
        "c.class_name, c.subject_name, c.current_topic_order "
        "FROM edu_timetable t "
        "LEFT JOIN edu_courses c ON c.id = t.course_id AND c.is_active = 1 "
        "WHERE t.weekday = ? ORDER BY t.period_number ASC",
        (weekday,),
    )


def set_timetable_slot(weekday: int, period_number: int, course_id: int | None):
    if course_id is None:
        db.ex("DELETE FROM edu_timetable WHERE weekday = ? AND period_number = ?", (weekday, period_number))
    else:
        db.ex(
            "INSERT INTO edu_timetable (weekday, period_number, course_id) VALUES (?, ?, ?) "
            "ON CONFLICT(weekday, period_number) DO UPDATE SET course_id = excluded.course_id",
            (weekday, period_number, course_id),
        )


# ------------------------------------------------------------------ 6. Menyu va Kartochka Ko'rinishlari

def main_edu_view() -> tuple[str, InlineKeyboardMarkup]:
    courses = get_courses()
    off, off_title, off_type = is_off_day(app.today())

    lines = ["🎓 <b>Maktab Ta'lim Moduli (O'qituvchi Paneli)</b>\n"]
    if off:
        type_str = "Ta'til" if off_type == "vacation" else "Bayram"
        lines.append(f"🏖 <b>Bugun dam olish kuni ({type_str}):</b> {app.esc(off_title)}")
        lines.append("<i>Dars eslatmalari to'xtatilgan.</i>\n")

    lines.append(f"📚 <b>Faol sinflar va fanlar:</b> {len(courses)} ta")

    # Bugungi darslar
    today_wd = app.today().weekday()
    if today_wd < 6:
        tt = get_timetable_for_day(today_wd)
        active_tt = [s for s in tt if s.get("course_id")]
        lines.append(f"🔔 <b>Bugungi darslar ({WEEKDAY_NAMES_UZ[today_wd]}):</b> {len(active_tt)} ta")
    else:
        lines.append("🔔 <b>Bugun Yakshanba:</b> Darslar yo'q.")

    kb_rows = [
        [btn("📚 Sinflar va Fanlar", "ed:courses"), btn("🗓 Dars jadvali", f"ed:tt:{min(today_wd, 5)}")],
        [btn("📅 Ta'til va Bayramlar", "ed:cal:0"), btn("🔔 Bugungi darslar", "ed:today")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def courses_list_view() -> tuple[str, InlineKeyboardMarkup]:
    courses = get_courses()
    lines = ["📚 <b>Sinflar va Fanlar Ro'yxati</b>\n"]
    if not courses:
        lines.append("Hozircha hech qanday sinf kiritilmagan.\n[➕ Yangi sinf qo'shish] orqali boshlang.")
    else:
        for c in courses:
            t_cnt = db.q1("SELECT COUNT(*) AS c FROM edu_topics WHERE course_id = ?", (c["id"],))["c"]
            notes_cnt = len(get_unresolved_notes(c["id"]))
            note_alert = f" 📌({notes_cnt} izoh)" if notes_cnt > 0 else ""
            lines.append(
                f"• <b>{app.esc(c['class_name'])} — {app.esc(c['subject_name'])}</b> | "
                f"Mavzu: №{c['current_topic_order']}/{t_cnt} ta{note_alert}"
            )

    kb_rows = []
    c_btns = []
    for c in courses:
        c_btns.append(btn(f"{c['class_name']} {c['subject_name'][:10]}", f"ed:c:{c['id']}"))
    for i in range(0, len(c_btns), 2):
        kb_rows.append(c_btns[i : i + 2])

    kb_rows.append([btn("➕ Yangi sinf qo'shish", "ed:add_c"), btn("⬅️ Maktab menyusi", "ed:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def course_detail_view(course_id: int) -> tuple[str, InlineKeyboardMarkup]:
    c = get_course(course_id)
    if not c:
        return "⚠️ Sinf topilmadi.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Ro'yxat", "ed:courses")]])

    topics = get_course_topics(course_id)
    curr_order = c["current_topic_order"]
    curr_t = next((t for t in topics if t["topic_order"] == curr_order), None)

    comp_cnt = sum(1 for t in topics if t["is_completed"])
    notes = get_unresolved_notes(course_id)

    type_map = {"regular": "Oddiy dars", "lab": "🔬 Laboratoriya", "exam": "📝 Nazorat ishi"}
    lines = [
        f"🏫 <b>{app.esc(c['class_name'])} — {app.esc(c['subject_name'])}</b>\n",
        f"📊 <b>Reja:</b> {len(topics)} ta mavzudan {comp_cnt} tasi o'tildi",
        f"📍 <b>Joriy mavzu:</b> №{curr_order}",
    ]
    if curr_t:
        lines.append(f"  📖 <b>Nomi:</b> {app.esc(curr_t['title'])}")
        lines.append(f"  🏷 <b>Turi:</b> {type_map.get(curr_t['topic_type'], 'Oddiy')}")
        if curr_t.get("homework"):
            lines.append(f"  🏠 <b>Uyga vazifa:</b> {app.esc(curr_t['homework'])}")
    else:
        lines.append("  <i>(Mavzular yuklanmagan yoki reja tugagan)</i>")

    if notes:
        lines.append("\n📌 <b>Keyingi dars uchun eslatmalar:</b>")
        for n in notes:
            lines.append(f"• {app.esc(n['note_text'])}")

    kb_rows = [
        [btn("📖 Mavzular rejasini ko'rish", f"ed:tpl:{course_id}:0")],
        [btn("📥 Excel (TMR) yuklash", f"ed:up_xlsx:{course_id}"), btn("📍 Joriy mavzuni surish", f"ed:set_pt:{course_id}:0")],
        [btn("📝 Darsga izoh qo'shish", f"ed:add_nt:{course_id}"), btn("📋 Izohlarni ko'rish", f"ed:nts:{course_id}")],
        [btn("🗑 Sinfni o'chirish", f"ed:del_c:{course_id}"), btn("⬅️ Sinflar ro'yxati", "ed:courses")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def topics_list_view(course_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    c = get_course(course_id)
    if not c:
        return "⚠️ Sinf topilmadi.", InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Ro'yxat", "ed:courses")]])

    topics = get_course_topics(course_id)
    total = len(topics)
    max_page = max(0, (total - 1) // PAGE_SIZE)
    page = min(max(0, page), max_page)
    offset = page * PAGE_SIZE
    page_topics = topics[offset : offset + PAGE_SIZE]

    lines = [f"📖 <b>{app.esc(c['class_name'])} — {app.esc(c['subject_name'])}</b> (TMR Rejasi)\n"]
    if not page_topics:
        lines.append("Reja bo'sh. [📥 Excel (TMR) yuklash] orqali to'ldiring.")
    else:
        for t in page_topics:
            icon = "✅" if t["is_completed"] else ("📍" if t["topic_order"] == c["current_topic_order"] else "⚪️")
            t_icon = "🔬 " if t["topic_type"] == "lab" else ("📝 " if t["topic_type"] == "exam" else "")
            lines.append(f"{icon} №{t['topic_order']}. {t_icon}<b>{app.esc(t['title'])}</b>")
            if t.get("homework"):
                lines.append(f"   🏠 <i>{app.esc(t['homework'])}</i>")

    kb_rows = []
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Oldingi", f"ed:tpl:{course_id}:{page - 1}"))
    if page < max_page:
        nav.append(btn("Keyingi ➡️", f"ed:tpl:{course_id}:{page + 1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([btn("📍 Joriy mavzuni belgilash", f"ed:set_pt:{course_id}:0"), btn("⬅️ Sinfga qaytish", f"ed:c:{course_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def pointer_selector_view(course_id: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    c = get_course(course_id)
    topics = get_course_topics(course_id)
    total = len(topics)
    max_page = max(0, (total - 1) // 10)
    page = min(max(0, page), max_page)
    offset = page * 10
    slice_topics = topics[offset : offset + 10]

    lines = [
        f"📍 <b>Joriy mavzuni tanlash: {app.esc(c['class_name'])} — {app.esc(c['subject_name'])}</b>\n",
        f"Hozirda belgilangan: <b>№{c['current_topic_order']}</b>",
        "Keyingi o'tiladigan dars raqamini tanlang:\n(Unga qadar bo'lgan mavzular o'tildi deb belgilanadi)",
    ]

    btns_row = []
    for t in slice_topics:
        o = t["order"] if isinstance(t, dict) and "order" in t else t["topic_order"]
        btns_row.append(btn(f"[{o}]", f"ed:set_ord:{course_id}:{o}"))

    kb_rows = []
    for i in range(0, len(btns_row), 5):
        kb_rows.append(btns_row[i : i + 5])

    nav = []
    if page > 0:
        nav.append(btn("⬅️ Oldingi", f"ed:set_pt:{course_id}:{page - 1}"))
    if page < max_page:
        nav.append(btn("Keyingi ➡️", f"ed:set_pt:{course_id}:{page + 1}"))
    if nav:
        kb_rows.append(nav)

    kb_rows.append([btn("⬅️ Orqaga", f"ed:c:{course_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def timetable_view(weekday: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    weekday = min(max(0, weekday), 5)
    slots = {s["period_number"]: s for s in get_timetable_for_day(weekday)}
    day_name = WEEKDAY_NAMES_UZ[weekday]

    lines = [f"🗓 <b>Haftalik Dars Jadvali — {day_name}</b>\n"]
    for p in range(1, 7):
        start_t, end_t = EDU_BELLS[p]
        slot = slots.get(p)
        if slot and slot.get("course_id"):
            c_txt = f"<b>{app.esc(slot['class_name'])}</b> — {app.esc(slot['subject_name'])}"
        else:
            c_txt = "<i>(Dars yo'q)</i>"
        lines.append(f"• <b>{p}-soat ({start_t}–{end_t}):</b> {c_txt}")

    # Hafta kunlari tugmalari
    day_btns = []
    short_days = ["Du", "Se", "Chor", "Pay", "Juma", "Shan"]
    for i, sd in enumerate(short_days):
        t_str = f"• {sd} •" if i == weekday else sd
        day_btns.append(btn(t_str, f"ed:tt:{i}"))

    kb_rows = [day_btns]

    # Slotlarni tahrirlash tugmalari
    edit_slot_btns = []
    for p in range(1, 7):
        slot = slots.get(p)
        label = f"{p}-soat" + (f" ({slot['class_name']})" if slot and slot.get("course_id") else "")
        edit_slot_btns.append(btn(label, f"ed:slot:{weekday}:{p}"))
    for i in range(0, len(edit_slot_btns), 2):
        kb_rows.append(edit_slot_btns[i : i + 2])

    kb_rows.append([btn("⬅️ Maktab menyusi", "ed:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def timetable_slot_picker_view(weekday: int, period: int) -> tuple[str, InlineKeyboardMarkup]:
    start_t, end_t = EDU_BELLS[period]
    day_name = WEEKDAY_NAMES_UZ[weekday]
    courses = get_courses()

    lines = [
        f"🗓 <b>Dars jadvali: {day_name}, {period}-soat ({start_t}–{end_t})</b>\n",
        "Ushbu soatga qaysi sinf biriktirilsin?",
    ]

    kb_rows = []
    c_btns = []
    for c in courses:
        c_btns.append(btn(f"{c['class_name']} — {c['subject_name']}", f"ed:set_slot:{weekday}:{period}:{c['id']}"))
    for i in range(0, len(c_btns), 2):
        kb_rows.append(c_btns[i : i + 2])

    kb_rows.append([btn("❌ Dars yo'q (Bo'shatish)", f"ed:set_slot:{weekday}:{period}:0")])
    kb_rows.append([btn("⬅️ Jadvalga qaytish", f"ed:tt:{weekday}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


def calendar_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    all_offs = get_all_off_days()
    lines = ["📅 <b>2026-2027 Ta'til va Bayramlar Taqvimi</b>\n"]
    if not all_offs:
        lines.append("Ta'til yoki bayram kunlari kiritilmagan.")
    else:
        for o in all_offs:
            t_icon = "🏖" if o["off_type"] == "vacation" else "🎉"
            span = o["start_date"] if o["start_date"] == o["end_date"] else f"{o['start_date']} — {o['end_date']}"
            lines.append(f"• {t_icon} <b>{app.esc(o['title'])}:</b> {span}")

    kb_rows = [
        [btn("➕ Yangi dam olish kuni", "ed:add_off")],
        [btn("⬅️ Maktab menyusi", "ed:home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ------------------------------------------------------------------ 7. APScheduler Dars Eslatmalari

async def schedule_today_lessons():
    """Bugungi kun uchun 10 daqiqa oldin eslatma va tugash vaqtida tasdiqlash joblarini qo'yadi."""
    today = app.today()
    off, off_title, _ = is_off_day(today)
    if off:
        app.log.info(f"Bugun dam olish kuni ({off_title}), maktab eslatmalari qo'yilmadi.")
        return

    today_wd = today.weekday()
    if today_wd >= 6:
        return

    slots = get_timetable_for_day(today_wd)
    now = app.now()

    for s in slots:
        cid = s.get("course_id")
        p = s["period_number"]
        if not cid:
            continue

        start_s, end_s = EDU_BELLS[p]
        start_dt = app.at(today, start_s)
        end_dt = app.at(today, end_s)

        # 10 daqiqa oldin ogohlantirish
        warn_dt = start_dt - timedelta(minutes=10)
        if warn_dt > now:
            app.schedule_job(warn_dt, send_pre_lesson_reminder, [p, cid], f"edu_pre_{p}_{cid}")

        # Dars tugashida tasdiqlash
        if end_dt > now:
            app.schedule_job(end_dt, send_post_lesson_confirmation, [p, cid], f"edu_post_{p}_{cid}")


async def send_evening_preview():
    """Har oqshom soat 20:00 da ertangi darslar ro'yxatini va ogohlantirishlarini yuboradi."""
    tomorrow = app.today() + timedelta(days=1)
    off, off_title, _ = is_off_day(tomorrow)
    if off:
        return

    tomorrow_wd = tomorrow.weekday()
    if tomorrow_wd >= 6:
        return

    slots = [s for s in get_timetable_for_day(tomorrow_wd) if s.get("course_id")]
    if not slots:
        return

    lines = [f"📚 <b>Ertangi darslar rejasi ({WEEKDAY_NAMES_UZ[tomorrow_wd]}, {tomorrow.strftime('%d.%m')}):</b>\n"]
    alerts = []

    for s in slots:
        p = s["period_number"]
        c_name = s["class_name"]
        s_name = s["subject_name"]
        start_t, end_t = EDU_BELLS[p]
        c_order = s["current_topic_order"]
        topic = db.q1("SELECT * FROM edu_topics WHERE course_id = ? AND topic_order = ?", (s["course_id"], c_order))

        if topic:
            t_title = topic["title"]
            hw_str = f" (Uyga: {topic['homework']})" if topic.get("homework") else ""
            lines.append(f"• <b>{p}-soat ({start_t})</b>: <b>{app.esc(c_name)}</b> — {app.esc(s_name)}\n  📖 Mavzu №{c_order}: <i>{app.esc(t_title)}</i>{app.esc(hw_str)}")
            if topic["topic_type"] == "lab":
                alerts.append(
                    f"🔬 <b>DIQQAT! Ertaga {app.esc(c_name)} da laboratoriya/amaliy mashg'ulot ({app.esc(t_title)}). "
                    f"Laborantdan jihozlarni oldindan olishni unutmang!</b>"
                )
            elif topic["topic_type"] == "exam":
                alerts.append(f"📝 <b>DIQQAT! Ertaga {app.esc(c_name)} da Nazorat ishi ({app.esc(t_title)})!</b>")
        else:
            lines.append(f"• <b>{p}-soat ({start_t})</b>: <b>{app.esc(c_name)}</b> — {app.esc(s_name)}\n  📖 Mavzu №{c_order}")

    if alerts:
        lines.append("\n" + "\n".join(alerts))

    await messenger.send("\n".join(lines), "info", bind_trigger=False)


async def send_pre_lesson_reminder(period: int, course_id: int):
    """Dars boshlanishidan 10 daqiqa oldin kartochka yuborish."""
    today = app.today()
    if is_off_day(today)[0]:
        return

    c = get_course(course_id)
    if not c:
        return

    start_t, end_t = EDU_BELLS[period]
    c_order = c["current_topic_order"]
    topic = db.q1("SELECT * FROM edu_topics WHERE course_id = ? AND topic_order = ?", (course_id, c_order))
    notes = get_unresolved_notes(course_id)

    lines = [
        f"🔔 <b>{period}-soat ({start_t}–{end_t}) | {app.esc(c['class_name'])} — {app.esc(c['subject_name'])}</b>\n",
    ]
    if topic:
        lines.append(f"📖 <b>Mavzu №{c_order}:</b> {app.esc(topic['title'])}")
        lines.append(f"🏠 <b>Uyga vazifa:</b> {app.esc(topic['homework'] or 'berilmagan')}")
    else:
        lines.append(f"📖 <b>Mavzu №{c_order}</b>")

    kb_rows = []
    if notes:
        lines.append("\n📌 <b>Sinfga oid maxsus eslatma(lar):</b>")
        for n in notes:
            lines.append(f"• {app.esc(n['note_text'])}")
            kb_rows.append([btn(f"✅ Izohni yopish: {n['note_text'][:20]}", f"ed:res_nt:{n['id']}")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    await messenger.send("\n".join(lines), "info", kb, bind_trigger=False)


async def send_post_lesson_confirmation(period: int, course_id: int):
    """Dars tugashida tasdiqlash so'rovi yuborish."""
    today = app.today()
    if is_off_day(today)[0]:
        return

    c = get_course(course_id)
    if not c:
        return

    c_order = c["current_topic_order"]
    topic = db.q1("SELECT * FROM edu_topics WHERE course_id = ? AND topic_order = ?", (course_id, c_order))
    t_name = topic["title"] if topic else f"№{c_order}"

    text = (
        f"🏁 <b>{app.esc(c['class_name'])} ({app.esc(c['subject_name'])})</b> darsi tugadi.\n\n"
        f"Mavzu №{c_order} (<i>{app.esc(t_name)}</i>) o'tildimi?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("✅ O'tildi (Keyingi mavzuga)", f"ed:done:{course_id}:{c_order}")],
        [btn("⏭ O'tilmadi (Keyingisiga qoldi)", f"ed:skip:{course_id}:{c_order}")],
        [btn("📝 Keyingi darsga izoh", f"ed:add_nt:{course_id}")],
    ])
    await messenger.send(text, "confirm", kb, bind_trigger=False)


# ------------------------------------------------------------------ 8. Router va Callback Handlers

async def entry(message: Message):
    text, kb = main_edu_view()
    await messenger.send(text, "info", kb)


@router.message(Command("maktab"))
@router.message(Command("talim"))
async def cmd_maktab_entry(message: Message, state: FSMContext):
    await state.clear()
    await entry(message)


@router.callback_query(F.data == "ed:home")
async def cb_edu_home(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = main_edu_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "ed:courses")
async def cb_edu_courses(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = courses_list_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:c:"))
async def cb_course_detail(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    text, kb = course_detail_view(cid)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:tpl:"))
async def cb_topics_list(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    cid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    await cb.answer()
    await state.clear()
    text, kb = topics_list_view(cid, page)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:set_pt:"))
async def cb_set_pointer_view(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    cid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 0
    await cb.answer()
    text, kb = pointer_selector_view(cid, page)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:set_ord:"))
async def cb_set_pointer_confirm(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    cid = int(parts[2])
    ord_val = int(parts[3])
    set_course_current_topic(cid, ord_val)
    await cb.answer(f"Joriy mavzu №{ord_val} ga belgilandi!", show_alert=True)
    await state.clear()
    text, kb = course_detail_view(cid)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:tt:"))
async def cb_timetable_day(cb: CallbackQuery, state: FSMContext):
    wd = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    text, kb = timetable_view(wd)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:slot:"))
async def cb_timetable_slot(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    wd = int(parts[2])
    p = int(parts[3])
    await cb.answer()
    text, kb = timetable_slot_picker_view(wd, p)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:set_slot:"))
async def cb_set_slot_val(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    wd = int(parts[2])
    p = int(parts[3])
    cid = int(parts[4])
    set_timetable_slot(wd, p, None if cid == 0 else cid)
    await cb.answer("Jadval yangilandi!")
    text, kb = timetable_view(wd)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:cal:"))
async def cb_calendar_view(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, kb = calendar_view()
    await _edit_or_send(cb.message, text, kb)


# --- Dars yakunini tasdiqlash ---

@router.callback_query(F.data.startswith("ed:done:"))
async def cb_lesson_done(cb: CallbackQuery):
    parts = cb.data.split(":")
    cid = int(parts[2])
    order = int(parts[3])
    new_o = mark_topic_completed(cid, order)
    c = get_course(cid)
    c_name = c["class_name"] if c else "Sinf"
    s_name = c["subject_name"] if c else "Fan"
    await cb.answer("Mavzu o'tildi deb belgilandi!")
    await _edit_or_send(
        cb.message,
        f"✅ <b>{app.esc(c_name)} ({app.esc(s_name)})</b>: Mavzu №{order} muvaffaqiyatli o'tildi deb qayd etildi.\n"
        f"Keyingi dars: <b>№{new_o}</b>.",
        InlineKeyboardMarkup(inline_keyboard=[[btn("📚 Sinf tafsilotlari", f"ed:c:{cid}")]]),
    )


@router.callback_query(F.data.startswith("ed:skip:"))
async def cb_lesson_skip(cb: CallbackQuery):
    parts = cb.data.split(":")
    cid = int(parts[2])
    order = int(parts[3])
    c = get_course(cid)
    c_name = c["class_name"] if c else "Sinf"
    s_name = c["subject_name"] if c else "Fan"
    await cb.answer("Mavzu keyingi darsga qoldirildi.")
    await _edit_or_send(
        cb.message,
        f"⏭ <b>{app.esc(c_name)} ({app.esc(s_name)})</b>: Mavzu №{order} keyingi darsga qoldirildi (o'zgarmadi).",
        InlineKeyboardMarkup(inline_keyboard=[[btn("📚 Sinf tafsilotlari", f"ed:c:{cid}")]]),
    )


# --- Sinf izohlari ---

@router.callback_query(F.data.startswith("ed:res_nt:"))
async def cb_resolve_note(cb: CallbackQuery):
    nid = int(cb.data.split(":")[2])
    resolve_class_note(nid)
    await cb.answer("Izoh yopildi!")
    await _edit_or_send(cb.message, "✅ Izoh muvaffaqiyatli yopildi.", None)


@router.callback_query(F.data.startswith("ed:add_nt:"))
async def cb_add_note_prompt(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    await state.set_state(EduFSM.add_note)
    await state.update_data(course_id=cid)
    c = get_course(cid)
    c_name = c["class_name"] if c else "Sinf"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", f"ed:c:{cid}")]])
    await _edit_or_send(
        cb.message,
        f"📝 <b>{app.esc(c_name)}</b> uchun keyingi darsga eslatma/izoh matnini yozing:\n"
        f"<i>(Masalan: Nurmatov jarima uy ishi olgan, daftarlarni yig'ish)</i>",
        kb,
    )


@router.message(EduFSM.add_note)
async def process_add_note(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if len(txt) < 2:
        await messenger.send("⚠️ Izoh kamida 2 belgidan iborat bo'lsin.", "info")
        return
    data = await state.get_data()
    cid = data["course_id"]
    add_class_note(cid, txt)
    await state.clear()
    text, kb = course_detail_view(cid)
    await messenger.send(f"✅ Izoh saqlandi!\n\n" + text, "info", kb)


# --- Yangi sinf qo'shish FSM ---

@router.callback_query(F.data == "ed:add_c")
async def cb_start_add_course(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(EduFSM.add_class_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "ed:courses")]])
    await _edit_or_send(cb.message, "➕ <b>Yangi sinf kiritish</b>\n\nSinf nomini kiriting (masalan: <code>8-B</code>):", kb)


@router.message(EduFSM.add_class_name)
async def process_class_name(message: Message, state: FSMContext):
    c_name = (message.text or "").strip()
    if len(c_name) < 2:
        await messenger.send("⚠️ Sinf nomi kamida 2 belgidan iborat bo'lsin.", "info")
        return
    await state.update_data(class_name=c_name)
    await state.set_state(EduFSM.add_subject_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "ed:courses")]])
    await messenger.send(f"<b>{app.esc(c_name)}</b> uchun fan nomini kiriting (masalan: <code>Fizika</code>):", "info", kb)


@router.message(EduFSM.add_subject_name)
async def process_subject_name(message: Message, state: FSMContext):
    s_name = (message.text or "").strip()
    if len(s_name) < 2:
        await messenger.send("⚠️ Fan nomi kamida 2 belgidan iborat bo'lsin.", "info")
        return
    data = await state.get_data()
    c_name = data["class_name"]
    cid = create_course(c_name, s_name)
    await state.clear()
    text, kb = course_detail_view(cid)
    await messenger.send(f"✅ <b>{app.esc(c_name)} — {app.esc(s_name)}</b> muvaffaqiyatli yaratildi!\n\n" + text, "info", kb)


# --- Excel TMR Upload FSM ---

@router.callback_query(F.data.startswith("ed:up_xlsx:"))
async def cb_start_upload_xlsx(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    await state.set_state(EduFSM.upload_excel)
    await state.update_data(course_id=cid)
    c = get_course(cid)
    c_name = c["class_name"] if c else "Sinf"
    s_name = c["subject_name"] if c else "Fan"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", f"ed:c:{cid}")]])
    await _edit_or_send(
        cb.message,
        f"📥 <b>{app.esc(c_name)} — {app.esc(s_name)}</b> uchun Excel (.xlsx) TMR faylini yuboring:\n\n"
        f"<i>(Fayl ichidagi birinchi varaq o'qiladi: T/R, Mavzu, Uyga vazifa ustunlari avtomatik ajratib olinadi).</i>",
        kb,
    )


@router.message(EduFSM.upload_excel, F.document)
async def process_excel_doc(message: Message, state: FSMContext):
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".xlsx", ".xls")):
        await messenger.send("⚠️ Iltimos, faqat <code>.xlsx</code> formatidagi Excel fayl yuboring.", "info")
        return

    data = await state.get_data()
    cid = data["course_id"]

    try:
        file = await app.bot.get_file(doc.file_id)
        file_bytes = await app.bot.download_file(file.file_path)
        if isinstance(file_bytes, io.BytesIO):
            raw = file_bytes.getvalue()
        elif hasattr(file_bytes, "read"):
            raw = file_bytes.read()
        else:
            raw = bytes(file_bytes)

        topics = parse_tmr_excel(raw)
        if not topics:
            await messenger.send("⚠️ Fayldan mavzular topilmadi. Jadval ustunlari to'g'ri ekanligini tekshiring.", "info")
            return

        cnt = import_course_topics(cid, topics)
        # Smart pointer selector ekraniga o'tamiz
        await state.clear()
        lines, kb = pointer_selector_view(cid, 0)
        intro = f"📥 <b>{cnt} ta mavzu yuklandi!</b>\n\nHozir qaysi mavzudasiz? Keyingi o'tiladigan dars raqamini tanlang:\n\n"
        await messenger.send(intro + lines, "info", kb)
    except Exception as e:
        app.log.error(f"Excel import xatosi: {e}")
        await messenger.send(f"⚠️ Faylni qayta ishlashda xatolik yuz berdi: {e}", "info")


@router.callback_query(F.data.startswith("ed:del_c:"))
async def cb_del_course(cb: CallbackQuery):
    cid = int(cb.data.split(":")[2])
    delete_course(cid)
    await cb.answer("Sinf o'chirildi!")
    text, kb = courses_list_view()
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data == "ed:today")
async def cb_edu_today(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    today = app.today()
    off, off_title, off_type = is_off_day(today)
    if off:
        type_str = "Ta'til" if off_type == "vacation" else "Bayram"
        text = f"🏖 <b>Bugun dam olish kuni ({type_str}): {app.esc(off_title)}</b>\n\nDarslar o'tilmaydi."
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Maktab", "ed:home")]])
        await _edit_or_send(cb.message, text, kb)
        return

    today_wd = today.weekday()
    if today_wd >= 6:
        text = "🔔 <b>Bugun Yakshanba:</b> Darslar yo'q."
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ Maktab", "ed:home")]])
        await _edit_or_send(cb.message, text, kb)
        return

    text, kb = timetable_view(today_wd)
    await _edit_or_send(cb.message, text, kb)


@router.callback_query(F.data.startswith("ed:nts:"))
async def cb_notes_list(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[2])
    await cb.answer()
    await state.clear()
    c = get_course(cid)
    notes = db.q("SELECT * FROM edu_class_notes WHERE course_id = ? ORDER BY is_resolved ASC, id DESC", (cid,))
    lines = [f"📋 <b>{app.esc(c['class_name'] if c else 'Sinf')} izohlari</b>\n"]
    if not notes:
        lines.append("Izohlar mavjud emas.")
    else:
        for n in notes:
            st = "✅ Bajarilgan" if n["is_resolved"] else "📌 Faol"
            lines.append(f"• ({st}): {app.esc(n['note_text'])}")

    kb_rows = []
    for n in notes:
        if not n["is_resolved"]:
            kb_rows.append([btn(f"✅ Yopish: {n['note_text'][:20]}", f"ed:res_nt:{n['id']}")])
    kb_rows.append([btn("➕ Yangi izoh qo'shish", f"ed:add_nt:{cid}"), btn("⬅️ Sinfga qaytish", f"ed:c:{cid}")])
    await _edit_or_send(cb.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data == "ed:add_off")
async def cb_start_add_off(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(EduFSM.add_off_title)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "ed:cal:0")]])
    await _edit_or_send(cb.message, "➕ <b>Yangi dam olish kuni qo'shish</b>\n\nNomini kiriting (masalan: <code>Qishki ta'til</code> yoki <code>Yangi yil</code>):", kb)


@router.message(EduFSM.add_off_title)
async def process_off_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if len(title) < 2:
        await messenger.send("⚠️ Nom kamida 2 belgidan iborat bo'lsin.", "info")
        return
    await state.update_data(off_title=title)
    await state.set_state(EduFSM.add_off_start)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "ed:cal:0")]])
    await messenger.send(f"<b>{app.esc(title)}</b> boshlanish sanasini kiriting (YYYY-MM-DD formatida, masalan: <code>2027-01-01</code>):", "info", kb)


@router.message(EduFSM.add_off_start)
async def process_off_start(message: Message, state: FSMContext):
    s_date = (message.text or "").strip()
    try:
        date.fromisoformat(s_date)
    except ValueError:
        await messenger.send("⚠️ Noto'g'ri sana formati. YYYY-MM-DD ko'rinishida kiriting (masalan: 2027-01-01):", "info")
        return
    await state.update_data(off_start=s_date)
    await state.set_state(EduFSM.add_off_end)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Bekor qilish", "ed:cal:0")]])
    await messenger.send("Tugash sanasini kiriting (YYYY-MM-DD, 1 kunlik bayram bo'lsa xuddi shu sanani kiriting):", "info", kb)


@router.message(EduFSM.add_off_end)
async def process_off_end(message: Message, state: FSMContext):
    e_date = (message.text or "").strip()
    try:
        date.fromisoformat(e_date)
    except ValueError:
        await messenger.send("⚠️ Noto'g'ri sana formati. YYYY-MM-DD ko'rinishida kiriting:", "info")
        return
    data = await state.get_data()
    title = data["off_title"]
    s_date = data["off_start"]
    off_type = "vacation" if "ta'til" in title.lower() else "holiday"
    add_off_day(title, s_date, e_date, off_type)
    await state.clear()
    text, kb = calendar_view()
    await messenger.send(f"✅ <b>{app.esc(title)}</b> kalendarga qo'shildi!\n\n" + text, "info", kb)

