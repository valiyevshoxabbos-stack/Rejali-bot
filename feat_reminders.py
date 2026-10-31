"""
Eslatmalar (3.0.1):
- Yuborish (izoh `note` bilan), tugmalar (bajardim/bajarmadim/kechiktirish/chek-point),
- Kunlik jurnal bilan integratsiya (javob berilgan xabar o'rniga jurnal yangilanadi),
- Ixtiyoriy rejim ("Boraman" bosilganda asosiy vaqtida eslatma),
- Tezkor belgilash, /qoshish oqimi, /royxat, /vaqt, /ball, /sinf, /izoh va /izohlar.
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
import feat_journal as journal
import feat_sleep as sleep
import messenger
import scoring
import services as sv

router = Router()
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
OPTIONAL_LEAD_MIN = 60  # ixtiyoriy taklif: boshlanishidan 1 soat oldin


# ------------------------------------------------------------------ yuborish

def status_line() -> str:
    monday, _ = scoring.week_bounds(app.today())
    s = scoring.week_summary(monday)
    return f"⭐ Bu hafta: {s['points']} ball ({s['done']}/{s['total']})"


def get_checkpoints(occ_id: int) -> list[dict]:
    return db.q(
        "SELECT c.id, c.title, COALESCE((SELECT done FROM checkpoint_status s "
        "WHERE s.checkpoint_id = c.id AND s.occurrence_id = ?), 0) AS done "
        "FROM checkpoints c WHERE c.reminder_id = (SELECT reminder_id FROM occurrences WHERE id = ?) "
        "ORDER BY c.position", (occ_id, occ_id))


def action_kb(occ_id: int) -> InlineKeyboardMarkup:
    rows = []
    for cp in get_checkpoints(occ_id):
        mark = "☑️" if cp["done"] else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {cp['title']}", callback_data=f"cp:{occ_id}:{cp['id']}")])
    rows.append([InlineKeyboardButton(text="✅ Bajardim", callback_data=f"done:{occ_id}"),
                 InlineKeyboardButton(text="❌ Bajarmadim", callback_data=f"skip:{occ_id}")])
    rows.append([InlineKeyboardButton(text="⏳ Kechiktirish", callback_data=f"post:{occ_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prayer_kb(occ_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ O'qidim", callback_data=f"done:{occ_id}")]
    ])


def optional_kb(occ_id: int, planned: bool = False) -> InlineKeyboardMarkup:
    first = (InlineKeyboardButton(text="✅ Bajardim", callback_data=f"done:{occ_id}") if planned
             else InlineKeyboardButton(text="👍 Boraman", callback_data=f"opt:plan:{occ_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        first, InlineKeyboardButton(text="⏭ Bugun emas", callback_data=f"opt:no:{occ_id}")]])


def _note_line(rem: dict | None) -> str:
    note = (rem.get("note") or "").strip() if rem else ""
    return f"\n📝 <i>{app.esc(note)}</i>" if note else ""


async def send_reminder(occ_id: int):
    occ = sv.get_occurrence(occ_id)
    if not occ or occ["status"] != "pending":
        return
    rem = sv.get_reminder_of(occ)
    today = app.today()
    if rem and (sv.pause_active(today) or sv.effective_mode(rem, today) == "off"):
        return
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    note_s = _note_line(rem)
    if occ["is_optional"]:
        text = f"💡 <b>Ixtiyoriy: {app.esc(occ['title'])}</b>\n🕒 {scheduled.strftime('%H:%M')}{note_s}\nBugun borasanmi?"
        await messenger.send(text, "reminder", optional_kb(occ_id), actionable=True, bind_trigger=False)
        return
    icon = scoring.PRIORITY_ICON.get(sv.get_priority(occ), "")
    kb = prayer_kb(occ_id) if occ.get("prayer") else action_kb(occ_id)
    text = f"⏰ {icon} <b>{app.esc(occ['title'])}</b>\n🕒 {scheduled.strftime('%H:%M')}{note_s}\n\n{status_line()}"
    await messenger.send(text, "reminder", kb, actionable=True, bind_trigger=False)


async def send_planned_optional(occ_id: int):
    """Ixtiyoriy eslatmada '👍 Boraman' bosilgach, asosiy boshlanish vaqtida eslatadi."""
    occ = sv.get_occurrence(occ_id)
    if not occ or occ["status"] != "planned":
        return
    rem = sv.get_reminder_of(occ)
    today = app.today()
    if rem and (sv.pause_active(today) or sv.effective_mode(rem, today) == "off"):
        return
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    text = f"⏰ 💡 <b>{app.esc(occ['title'])}</b>\n🕒 {scheduled.strftime('%H:%M')}{_note_line(rem)}"
    await messenger.send(text, "reminder", optional_kb(occ_id, planned=True), actionable=True, bind_trigger=False)


def schedule_reminder_today(rem: dict, d=None) -> int:
    """Bugungi eslatmani schedulerga qo'yadi (ixtiyoriy bo'lsa 1 soat oldin taklif sifatida)."""
    d = d or app.today()
    occ_id = sv.ensure_occurrence(rem, d)
    start = app.at(d, rem["start"])
    run_at = start
    if rem.get("eff_mode", rem.get("mode")) == "optional":
        run_at = start - timedelta(minutes=OPTIONAL_LEAD_MIN)
        if run_at <= app.now() < start:
            run_at = app.now() + timedelta(seconds=5)
    app.schedule_job(run_at, send_reminder, [occ_id], f"rem_{occ_id}")
    return occ_id


# ------------------------------------------------------------------ tugmalar

def fmt_late(minutes: int) -> str:
    if minutes < 1:
        return "vaqtida"
    if minutes < 60:
        return f"{minutes} daqiqa kechikib"
    h, m = divmod(minutes, 60)
    return f"{h} soat {m} daqiqa kechikib" if m else f"{h} soat kechikib"


@router.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery):
    occ_id = int(cb.data.split(":")[1])
    result = sv.mark_done(occ_id)
    if "error" in result:
        await cb.answer("Allaqachon belgilangan")
        return
    await cb.answer("Bajarildi!")
    line = f"✅ Bajarildi ({fmt_late(result['late_minutes'])})"
    if result["points"]:
        line += f"\n⭐ +{result['points']} ball" + (" (kechiktirilgan, yarim ball)" if result["was_postponed"] else "")
    occ = sv.get_occurrence(occ_id)
    rem = sv.get_reminder_of(occ)
    if rem and rem["slug"] == "yotish":
        start = sleep.on_bedtime_confirmed()
        line += f"\n😴 Uyqu {start.strftime('%H:%M')} dan hisoblanadi"
    occ_date = date.fromisoformat(occ["date"])
    await journal.record_and_replace(occ_date, cb.message, f"{cb.message.html_text}\n\n{line}")


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    occ_id = int(cb.data.split(":")[1])
    occ = sv.get_occurrence(occ_id)
    if not sv.mark_skipped(occ_id):
        await cb.answer("Allaqachon belgilangan")
        return
    await cb.answer("Bekor qilindi")
    occ_date = date.fromisoformat(occ["date"])
    await journal.record_and_replace(occ_date, cb.message, f"{cb.message.html_text}\n\n❌ Bajarilmadi")


@router.callback_query(F.data.startswith("opt:"))
async def cb_optional(cb: CallbackQuery):
    _, action, occ_id_s = cb.data.split(":")
    occ_id = int(occ_id_s)
    occ = sv.get_occurrence(occ_id)
    if occ is None:
        await cb.answer("Topilmadi")
        return
    occ_date = date.fromisoformat(occ["date"])
    if action == "plan":
        if sv.set_optional_status(occ_id, "planned"):
            await cb.answer("Yaxshi! Vaqti kelganda eslataman")
            scheduled = datetime.fromisoformat(occ["scheduled_at"])
            if scheduled > app.now():
                app.schedule_job(scheduled, send_planned_optional, [occ_id], f"rem_plan_{occ_id}")
                await journal.record_and_replace(occ_date, cb.message, f"{cb.message.html_text}\n\n👍 Boraman")
            else:
                await cb.message.edit_reply_markup(reply_markup=optional_kb(occ_id, planned=True))
                await journal.sync(occ_date)
            return
    elif action == "no":
        if sv.set_optional_status(occ_id, "declined"):
            await cb.answer("Tushunarli")
            job = app.scheduler.get_job(f"rem_plan_{occ_id}")
            if job:
                job.remove()
            await journal.record_and_replace(occ_date, cb.message, f"{cb.message.html_text}\n\n⏭ Bugun emas")
            return
    await cb.answer()


@router.callback_query(F.data.startswith("cp:"))
async def cb_checkpoint(cb: CallbackQuery):
    _, occ_id_s, cp_id_s = cb.data.split(":")
    occ_id, cp_id = int(occ_id_s), int(cp_id_s)
    row = db.q1("SELECT done FROM checkpoint_status WHERE checkpoint_id=? AND occurrence_id=?", (cp_id, occ_id))
    db.ex("INSERT INTO checkpoint_status (checkpoint_id, occurrence_id, done) VALUES (?,?,?) "
          "ON CONFLICT(checkpoint_id, occurrence_id) DO UPDATE SET done = excluded.done",
          (cp_id, occ_id, 0 if (row and row["done"]) else 1))
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=action_kb(occ_id))


# ------------------------------------------------------------------ kechiktirish

class Postpone(StatesGroup):
    waiting_date = State()


def postpone_kb(occ_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="+15 daqiqa", callback_data=f"pq:{occ_id}:15"),
         InlineKeyboardButton(text="+1 soat", callback_data=f"pq:{occ_id}:60")],
        [InlineKeyboardButton(text="Ertaga", callback_data=f"pq:{occ_id}:tom"),
         InlineKeyboardButton(text="Boshqa sana", callback_data=f"pq:{occ_id}:other")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"back:{occ_id}")],
    ])


@router.callback_query(F.data.startswith("post:"))
async def cb_postpone(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=postpone_kb(int(cb.data.split(":")[1])))


@router.callback_query(F.data.startswith("back:"))
async def cb_back(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=action_kb(int(cb.data.split(":")[1])))


async def finish_postpone(occ_id: int, target: datetime, message_id: int | None):
    old = sv.get_occurrence(occ_id)
    new_id = sv.postpone(occ_id, target)
    if not new_id or not old:
        await messenger.send("Kechiktirib bo'lmadi (allaqachon bajarilgan yoki topilmadi).", "confirm", bind_trigger=False)
        return
    if target.date() == app.today():
        app.schedule_job(target, send_reminder, [new_id], f"rem_{new_id}")
    text = f"⏳ Kechiktirildi: {target.strftime('%d.%m.%Y %H:%M')}"
    fallback = f"⏰ <b>{app.esc(old['title'])}</b>\n\n{text}"
    occ_date = date.fromisoformat(old["date"])
    await journal.record_and_replace_by_id(occ_date, message_id, fallback)


@router.callback_query(F.data.startswith("pq:"))
async def cb_postpone_choice(cb: CallbackQuery, state: FSMContext):
    _, occ_id_s, choice = cb.data.split(":")
    occ_id = int(occ_id_s)
    occ = sv.get_occurrence(occ_id)
    if occ is None:
        await cb.answer("Topilmadi")
        return
    if choice == "other":
        await cb.answer()
        await state.clear()
        await state.set_state(Postpone.waiting_date)
        await state.update_data(occ_id=occ_id, msg_id=cb.message.message_id, prompts=[], user_prompts=[])
        await common.ask(state, "Qachonga ko'chiramiz? <code>2026-11-05 09:30</code> yoki faqat sana "
                                "<code>2026-11-05</code> (asl vaqt saqlanadi).")
        return
    await cb.answer("Kechiktirildi")
    scheduled = datetime.fromisoformat(occ["scheduled_at"])
    target = scheduled + timedelta(days=1) if choice == "tom" else app.now() + timedelta(minutes=int(choice))
    await finish_postpone(occ_id, target, cb.message.message_id)


@router.message(Postpone.waiting_date)
async def process_postpone_date(message: Message, state: FSMContext):
    await common.track_user(state, message)
    data = await state.get_data()
    occ = sv.get_occurrence(data["occ_id"])
    text = (message.text or "").strip()
    target = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in ("%Y-%m-%d", "%d.%m.%Y") and occ:
                asl = datetime.fromisoformat(occ["scheduled_at"])
                parsed = parsed.replace(hour=asl.hour, minute=asl.minute)
            target = parsed.replace(tzinfo=app.TZ)
            break
        except ValueError:
            continue
    if target is None:
        await messenger.send("Format noto'g'ri. Masalan: <code>2026-11-05 09:30</code>", "confirm", bind_trigger=False)
        return
    if target <= app.now():
        await messenger.send("Bu vaqt allaqachon o'tgan. Kelajakdagi vaqtni yozing.", "confirm", bind_trigger=False)
        return
    await common.clear_prompts(state)
    await state.clear()
    await finish_postpone(data["occ_id"], target, data.get("msg_id"))


# ------------------------------------------------------------------ tezkor belgilash

@router.callback_query(F.data == "ql:list")
async def cb_quick_list(cb: CallbackQuery):
    choices = sv.quick_log_choices()
    if not choices:
        await cb.answer("Ballli eslatma yo'q", show_alert=True)
        return
    await cb.answer()
    rows, row = [], []
    for r in choices:
        row.append(InlineKeyboardButton(text=r["title"][:26], callback_data=f"ql:{r['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await messenger.send("➕ <b>Nimani bajardingiz?</b>\nEslatma kelmagan bo'lsa ham ball qo'shiladi.",
                         "info", InlineKeyboardMarkup(inline_keyboard=rows), bind_trigger=False)


@router.callback_query(F.data.regexp(r"^ql:\d+$"))
async def cb_quick_log(cb: CallbackQuery):
    rid = int(cb.data.split(":")[1])
    result = sv.quick_log(rid)
    if "error" in result:
        await cb.answer(f"{result.get('title', '')}: {result['error']}", show_alert=True)
        return
    await cb.answer("Qayd etildi!")
    line = f"✅ <b>{app.esc(result['title'])}</b> qayd etildi"
    if result["points"]:
        line += f"\n⭐ +{result['points']} ball"
    rem = db.q1("SELECT slug FROM reminders WHERE id = ?", (rid,))
    if rem and rem["slug"] == "yotish":
        line += f"\n😴 Uyqu {sleep.on_bedtime_confirmed().strftime('%H:%M')} dan hisoblanadi"
    await journal.record_and_replace(app.today(), cb.message, line)


# ------------------------------------------------------------------ /qoshish oqimi

class Add(StatesGroup):
    kind = State()
    weekday = State()
    date = State()
    time = State()
    title = State()
    note = State()
    checkpoints = State()
    priority = State()
    scored = State()
    points = State()


async def start_add(state: FSMContext):
    await state.clear()
    await state.set_state(Add.kind)
    await common.ask(state, "📝 <b>Yangi eslatma.</b> Qachon takrorlansin?", [
        [("🔁 Har kuni", "ak:daily")], [("📆 Haftaning bir kunida", "ak:weekly")], [("📌 Aniq sanada", "ak:once")]])


@router.message(Command("qoshish"))
async def cmd_add(message: Message, state: FSMContext):
    await start_add(state)


@router.callback_query(F.data == "add:rem")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await start_add(state)


@router.callback_query(Add.kind, F.data.startswith("ak:"))
async def add_kind(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    kind = cb.data.split(":")[1]
    await state.update_data(kind=kind)
    if kind == "daily":
        await state.set_state(Add.time)
        await common.ask(state, "Soatini yozing (masalan <code>22:00</code>):")
    elif kind == "weekly":
        await state.set_state(Add.weekday)
        rows = [[(n, f"aw:{i}")] for i, n in enumerate(sv.WEEKDAY_NAMES)]
        await common.ask(state, "Qaysi kuni?", [rows[i] + rows[i + 1] if i + 1 < 7 else rows[i] for i in range(0, 7, 2)])
    else:
        await state.set_state(Add.date)
        await common.ask(state, "Sanani yozing (<code>2026-11-05</code> yoki <code>05.11.2026</code>):")


@router.callback_query(Add.weekday, F.data.startswith("aw:"))
async def add_weekday(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(weekday=int(cb.data.split(":")[1]))
    await state.set_state(Add.time)
    await common.ask(state, "Soatini yozing (masalan <code>18:00</code>):")


@router.message(Add.date)
async def add_date(message: Message, state: FSMContext):
    await common.track_user(state, message)
    t = (message.text or "").strip()
    d = None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(t, fmt).date()
            break
        except ValueError:
            d = None
    if d is None:
        await messenger.send("Format: <code>2026-11-05</code> yoki <code>05.11.2026</code>", "confirm", bind_trigger=False)
        return
    await state.update_data(date=d.isoformat())
    await state.set_state(Add.time)
    await common.ask(state, "Soatini yozing (masalan <code>09:00</code>):")


@router.message(Add.time)
async def add_time(message: Message, state: FSMContext):
    await common.track_user(state, message)
    t = (message.text or "").strip()
    if not TIME_RE.match(t):
        await messenger.send("Format: <code>HH:MM</code> (masalan 09:30)", "confirm", bind_trigger=False)
        return
    h, m = sv.parse_hhmm(t)
    await state.update_data(time=f"{h:02d}:{m:02d}")
    await state.set_state(Add.title)
    await common.ask(state, "Sarlavha (qisqa nom), masalan <i>Seminarga tayyorlanish</i>:")


@router.message(Add.title)
async def add_title(message: Message, state: FSMContext):
    await common.track_user(state, message)
    t = (message.text or "").strip()
    if not t:
        await messenger.send("Sarlavhani matn sifatida yozing.", "confirm", bind_trigger=False)
        return
    await state.update_data(title=t)
    await state.set_state(Add.note)
    await common.ask(state, "Izoh (mavzu, havola, qo'shimcha ma'lumot). Kerak bo'lmasa <code>-</code> yozing:")


@router.message(Add.note)
async def add_note(message: Message, state: FSMContext):
    await common.track_user(state, message)
    n = (message.text or "").strip()
    await state.update_data(note="" if n == "-" else n)
    await state.set_state(Add.checkpoints)
    await common.ask(state, "Ichida chek-pointlar (kichik tekshiruvlar) bormi? Har birini yangi qatordan yozing, "
                            "bo'lmasa <code>-</code>:\n<code>Tish yuvish\nYuzni sovunlash</code>")


@router.message(Add.checkpoints)
async def add_checkpoints(message: Message, state: FSMContext):
    await common.track_user(state, message)
    raw = (message.text or "").strip()
    await state.update_data(checkpoints=[] if raw == "-" else [x.strip() for x in raw.splitlines() if x.strip()])
    await state.set_state(Add.priority)
    await common.ask(state, "Ustuvorlik:", [[("Oddiy", "ap:1"), ("Muhim ❗", "ap:2"), ("Juda muhim 🔥", "ap:3")]])


@router.callback_query(Add.priority, F.data.startswith("ap:"))
async def add_priority(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(priority=int(cb.data.split(":")[1]))
    await state.set_state(Add.scored)
    await common.ask(state, "Bu eslatma ballga ta'sir qilsinmi?", [[("Ha", "as:yes"), ("Yo'q", "as:no")]])


@router.callback_query(Add.scored, F.data.startswith("as:"))
async def add_scored(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    scored = cb.data.endswith("yes")
    await state.update_data(scored=scored)
    if scored:
        await state.set_state(Add.points)
        await common.ask(state, "Necha ball beriladi? (butun son, masalan <code>10</code>)")
    else:
        await finalize_add(state, 0)


@router.message(Add.points)
async def add_points(message: Message, state: FSMContext):
    await common.track_user(state, message)
    t = (message.text or "").strip()
    if not t.isdigit() or int(t) <= 0:
        await messenger.send("Musbat butun son yozing, masalan 10", "confirm", bind_trigger=False)
        return
    await finalize_add(state, int(t))


async def finalize_add(state: FSMContext, points: int):
    data = await state.get_data()
    kind, scored = data["kind"], data["scored"]
    rid = db.ins(
        "INSERT INTO reminders (source, kind, weekday, date, start, title, note, priority, scored, points) "
        "VALUES ('custom', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, data.get("weekday"), data.get("date"), data["time"], data["title"], data.get("note", ""),
         data["priority"], int(scored), points))
    for i, cp in enumerate(data.get("checkpoints", [])):
        db.ins("INSERT INTO checkpoints (reminder_id, title, position) VALUES (?,?,?)", (rid, cp, i))
    await common.clear_prompts(state)
    await state.clear()

    today = app.today()
    if not sv.pause_active(today) and (
        kind == "daily"
        or (kind == "weekly" and data.get("weekday") == today.weekday())
        or (kind == "once" and data.get("date") == today.isoformat())
    ):
        rem = db.q1("SELECT * FROM reminders WHERE id = ?", (rid,))
        rem["eff_mode"] = "required"
        schedule_reminder_today(rem, today)

    cps = data.get("checkpoints", [])
    extra = f"\nChek-pointlar: {len(cps)} ta" if cps else ""
    ball = f", ball: {points}" if scored else ", ball: yo'q"
    await messenger.send(
        f"✅ Eslatma qo'shildi (#{rid}): <b>{app.esc(data['title'])}</b>{extra}\n"
        f"Ustuvorlik: {scoring.PRIORITY_NAMES[data['priority']]}{ball}",
        "confirm", bind_trigger=False,
    )


# ------------------------------------------------------------------ /royxat, /ochirish

def custom_list():
    rows = db.q("SELECT * FROM reminders WHERE source = 'custom' AND active = 1 ORDER BY id")
    if not rows:
        return "Maxsus eslatmalar yo'q. Qo'shish: /qoshish", None
    lines = ["<b>Maxsus eslatmalar:</b>\n"]
    kb = []
    for r in rows:
        if r["kind"] == "daily":
            when = f"har kuni {r['start']}"
        elif r["kind"] == "weekly":
            when = f"har {sv.WEEKDAY_NAMES[r['weekday']]} {r['start']}"
        else:
            when = f"{r['date']} {r['start']}"
        lines.append(f"{sv.MODE_ICON[r['mode']]} #{r['id']} {app.esc(r['title'])} ({when})")
        kb.append([InlineKeyboardButton(text=f"🗑 #{r['id']} {r['title'][:20]}", callback_data=f"rd:{r['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb[:20])


@router.message(Command("royxat"))
async def cmd_list(message: Message):
    text, kb = custom_list()
    await messenger.send(text, "info", kb)


@router.callback_query(F.data.startswith("rd:"))
async def cb_remove(cb: CallbackQuery):
    rid = int(cb.data.split(":")[1])
    db.ex("UPDATE reminders SET active = 0 WHERE id = ? AND source = 'custom'", (rid,))
    await cb.answer("O'chirildi")
    import jobs
    await jobs.schedule_today()
    text, kb = custom_list()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.message(Command("ochirish"))
async def cmd_delete(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().lstrip("#").isdigit():
        await messenger.send("Format: /ochirish 3 (ID ni /royxat dan ko'ring)", "confirm")
        return
    rid = int(parts[1].strip().lstrip("#"))
    n = db.ex("UPDATE reminders SET active = 0 WHERE id = ? AND source = 'custom' AND active = 1", (rid,))
    if n:
        import jobs
        await jobs.schedule_today()
    await messenger.send(f"✅ #{rid} o'chirildi." if n else f"#{rid} topilmadi.", "confirm")


# ------------------------------------------------------------------ rejimlar (sozlamalardan chaqiriladi)

def set_mode(key: str, mode: str, off_days: int = 0) -> int:
    """key: 's:<slug>' (standart) yoki 'c:<id>' (maxsus). off_days=0 bo'lsa muddatsiz."""
    off_until = None
    if mode == "off" and off_days:
        off_until = (app.today() + timedelta(days=off_days)).isoformat()
    kind, value = key.split(":", 1)
    where, arg = ("source = 'standard' AND slug = ?", value) if kind == "s" else ("id = ?", int(value))
    n = db.ex(f"UPDATE reminders SET mode = ?, off_until = ? WHERE {where}", (mode, off_until, arg))
    ids = [r["id"] for r in db.q(f"SELECT id FROM reminders WHERE {where}", (arg,))]
    if mode == "off":
        for rid in ids:
            db.ex("DELETE FROM occurrences WHERE reminder_id = ? AND date >= ? AND status IN ('pending', 'planned')",
                  (rid, app.today().isoformat()))
    elif mode == "optional":
        for rid in ids:
            db.ex("UPDATE occurrences SET is_optional = 1 WHERE reminder_id = ? AND date >= ? AND status = 'pending'",
                  (rid, app.today().isoformat()))
    else:
        for rid in ids:
            db.ex("UPDATE occurrences SET is_optional = 0 WHERE reminder_id = ? AND date >= ? AND status = 'pending'",
                  (rid, app.today().isoformat()))
    return n


# ------------------------------------------------------------------ /vaqt, /ball, /ballqoshish, /sinf, /izoh, /izohlar

@router.message(Command("vaqt"))
async def cmd_time(message: Message):
    """/vaqt zal 09:30 (bugun uchun) yoki /vaqt zal 09:30 doimiy"""
    parts = (message.text or "").split()
    if len(parts) < 3:
        slugs = [r["slug"] for r in db.q("SELECT DISTINCT slug FROM reminders WHERE source='standard' "
                                         "AND slug IS NOT NULL ORDER BY slug")]
        await messenger.send("Format:\n<code>/vaqt zal 09:30</code> — faqat bugun\n"
                             "<code>/vaqt zal 09:30 doimiy</code> — doimiy\n\n"
                             f"Nomlar: {', '.join(slugs)}", "info")
        return
    slug, t = parts[1].lower(), parts[2]
    permanent = len(parts) > 3 and parts[3].lower() in ("doimiy", "d")
    if not TIME_RE.match(t):
        await messenger.send("Vaqt formati: HH:MM (masalan 09:30)", "confirm")
        return
    h, m = sv.parse_hhmm(t)
    t = f"{h:02d}:{m:02d}"
    wd = app.today().weekday()
    if permanent:
        ok = db.ex("UPDATE reminders SET start = ? WHERE source='standard' AND slug = ? AND active = 1 "
                   "AND (kind = 'daily' OR weekday = ?)", (t, slug, wd)) > 0
    else:
        row = db.q1("SELECT id FROM reminders WHERE source='standard' AND slug = ? AND active = 1 "
                    "AND (kind = 'daily' OR weekday = ?)", (slug, wd))
        ok = row is not None
        if ok:
            db.ex("INSERT INTO overrides (reminder_id, date, start) VALUES (?,?,?) "
                  "ON CONFLICT(reminder_id, date) DO UPDATE SET start = excluded.start",
                  (row["id"], app.today().isoformat(), t))
    if not ok:
        await messenger.send(f"Bugun uchun '{app.esc(slug)}' topilmadi. Nomlar: /vaqt", "confirm")
        return
    import jobs
    await jobs.schedule_today()
    await messenger.send(f"✅ {app.esc(slug)} → {t} ({'doimiy' if permanent else 'faqat bugun'})", "confirm")


@router.message(Command("ball"))
async def cmd_points(message: Message):
    """/ball -> ro'yxat; /ball zal 12 -> standart eslatma balli"""
    parts = (message.text or "").split()
    if len(parts) < 3:
        rows = db.q("SELECT slug, title, points FROM reminders WHERE source='standard' AND scored=1 "
                    "AND active=1 GROUP BY slug ORDER BY slug")
        lines = ["<b>Ballli standart eslatmalar:</b>\n"] + [
            f"{r['slug']}: {r['points']} ball ({app.esc(r['title'])})" for r in rows]
        await messenger.send("\n".join(lines) + "\n\nO'zgartirish: <code>/ball zal 12</code>", "info")
        return
    if not parts[2].isdigit():
        await messenger.send("Ball musbat butun son bo'lishi kerak.", "confirm")
        return
    n = db.ex("UPDATE reminders SET points = ?, scored = 1 WHERE source='standard' AND slug = ? AND active = 1",
              (int(parts[2]), parts[1].lower()))
    await messenger.send(f"✅ {app.esc(parts[1])} → {parts[2]} ball" if n else f"'{app.esc(parts[1])}' topilmadi. /ball",
                         "confirm")


@router.message(Command("ballqoshish"))
async def cmd_custom_points(message: Message):
    parts = (message.text or "").split()
    if len(parts) != 3 or not all(x.isdigit() for x in parts[1:]):
        await messenger.send("Format: <code>/ballqoshish 24 12</code> (ID, yangi ball)", "confirm")
        return
    n = db.ex("UPDATE reminders SET points = ?, scored = 1 WHERE id = ? AND source='custom' AND active = 1",
              (int(parts[2]), int(parts[1])))
    await messenger.send(f"✅ #{parts[1]} → {parts[2]} ball" if n else f"#{parts[1]} topilmadi.", "confirm")


@router.message(Command("sinf"))
async def cmd_school(message: Message):
    """/sinf juma 7b 8b 11b 9b 10b"""
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[1].lower() not in sv.WEEKDAY_LOOKUP or len(parts) > 9:
        await messenger.send("Format: <code>/sinf juma 7b 8b 11b 9b 10b</code>", "confirm")
        return
    wd = sv.WEEKDAY_LOOKUP[parts[1].lower()]
    sv.set_school_classes(wd, parts[2:])
    if wd == app.today().weekday():
        import jobs
        await jobs.schedule_today()
    await messenger.send(f"✅ {sv.WEEKDAY_NAMES[wd]}: {', '.join(parts[2:])}", "confirm")


class NoteState(StatesGroup):
    waiting = State()


def _save_note(text: str):
    db.ins("INSERT INTO notes (date, text, created_at) VALUES (?,?,?)",
           (app.today().isoformat(), text, app.now().isoformat()))


def _notes_text_kb():
    rows = db.q("SELECT id, date, text FROM notes ORDER BY id DESC LIMIT 15")
    if not rows:
        return "📒 Saqlangan izohlar yo'q. Qo'shish: <code>/izoh matn</code>", None
    lines = ["📒 <b>Oxirgi izohlar:</b>\n"]
    kb = []
    for r in rows:
        lines.append(f"• [{r['date'][5:]}] {app.esc(r['text'])}")
        kb.append([InlineKeyboardButton(text=f"🗑 {r['text'][:24]}", callback_data=f"nt:del:{r['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb[:15])


@router.message(Command("izoh"))
async def cmd_note(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await messenger.send("Format: <code>/izoh matn</code> (ko'rish: /izohlar)", "confirm")
        return
    _save_note(parts[1].strip())
    await messenger.send("📝 Izoh saqlandi.", "confirm")


@router.message(Command("izohlar"))
async def cmd_notes_list(message: Message):
    text, kb = _notes_text_kb()
    await messenger.send(text, "info", kb)


@router.callback_query(F.data == "nt:list")
async def cb_notes_list(cb: CallbackQuery):
    await cb.answer()
    text, kb = _notes_text_kb()
    await messenger.send(text, "info", kb, bind_trigger=False)


@router.callback_query(F.data.startswith("nt:del:"))
async def cb_note_del(cb: CallbackQuery):
    nid = int(cb.data.split(":")[2])
    db.ex("DELETE FROM notes WHERE id = ?", (nid,))
    await cb.answer("O'chirildi")
    text, kb = _notes_text_kb()
    await cb.message.edit_text(text, reply_markup=messenger.with_delete(kb))


@router.callback_query(F.data == "add:note")
async def cb_add_note(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(NoteState.waiting)
    await common.ask(state, "📝 Izoh matnini yozing:")


@router.message(NoteState.waiting)
async def process_note(message: Message, state: FSMContext):
    await common.track_user(state, message)
    if not (message.text or "").strip():
        await messenger.send("Matn yozing.", "confirm", bind_trigger=False)
        return
    _save_note(message.text.strip())
    await common.clear_prompts(state)
    await state.clear()
    await messenger.send("📝 Izoh saqlandi.", "confirm", bind_trigger=False)
