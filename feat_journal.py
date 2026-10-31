"""
Kunlik jurnal (3.0.1):
- Javob berilgan eslatma (✅/❌/⏳/⏭) xabari o'chadi, o'rniga shu kunning bitta xabari tahrirlanib boradi.
- Matn har safar bazadan qaytadan quriladi; xabar id si `journal_<sana>` kalitida saqlanadi.
- Tahrirlab bo'lmasa (o'chirilgan bo'lsa) yangisi ovozsiz va 🗑 tugmasiz yaratiladi.
- Eslatma xabari 48 soatdan eski bo'lib o'chmasa yoki jurnal yangilanmasa, natija o'sha xabarda qoladi.
"""

from datetime import date, datetime

from aiogram.exceptions import TelegramBadRequest

import app
import db
import messenger
import schedule_data as sd
import scoring
import services as sv


def _fmt_late_short(late: int) -> str:
    if late < 1:
        return "vaqtida"
    if late < 60:
        return f"{late} daq kech"
    h, m = divmod(late, 60)
    return f"{h} s {m} daq kech" if m else f"{h} s kech"


def _fmt_postponed(target_iso: str | None, d: date) -> str:
    if not target_iso:
        return "?"
    dt = datetime.fromisoformat(target_iso)
    return dt.strftime("%H:%M") if dt.date() == d else dt.strftime("%d.%m %H:%M")


def _prayer_line(d: date) -> str | None:
    rows = {
        r["prayer"]: r
        for r in db.q("SELECT prayer, status, scheduled_at, done_at FROM occurrences WHERE prayer IS NOT NULL AND date = ?",
                      (d.isoformat(),))
    }
    if not rows:
        return None
    now_iso = app.now().isoformat()
    parts = []
    for p in sd.PRAYER_NAMES:
        r = rows.get(p)
        if not r:
            parts.append(p)
            continue
        st = r["status"]
        if st == "done":
            hm = datetime.fromisoformat(r["done_at"]).strftime("%H:%M") if r.get("done_at") else ""
            parts.append(f"{p} ✅{hm}")
        elif st == "qazo":
            parts.append(f"{p} ⚠️")
        elif st == "qazo_done":
            parts.append(f"{p} ☑️")
        elif r["scheduled_at"] <= now_iso:
            parts.append(f"{p} ⬜")
        else:
            parts.append(p)
    return "🕌 " + " · ".join(parts)


def build_text(d: date) -> str:
    lines = [f"📓 <b>Kunlik jurnal — {d.strftime('%d.%m')} ({sv.WEEKDAY_NAMES[d.weekday()]})</b>\n"]
    if sv.pause_active(d):
        p_until = sv.pause_until()
        lines.append(f"⏸ Pauza rejimi ({p_until.strftime('%d.%m')} gacha)")

    for r in sv.disabled_for_day(d):
        until = f" ({r['off_until'][8:10]}.{r['off_until'][5:7]} gacha)" if r.get("off_until") else ""
        lines.append(f"🔕 {app.esc(r['title'])} — o'chirilgan{until}")

    occs = db.q(
        "SELECT * FROM occurrences WHERE date = ? AND prayer IS NULL AND status != 'pending' "
        "ORDER BY COALESCE(done_at, scheduled_at), id",
        (d.isoformat(),),
    )
    for o in occs:
        st = o["status"]
        title = app.esc(o["title"])
        if st == "done":
            hm = datetime.fromisoformat(o["done_at"]).strftime("%H:%M") if o.get("done_at") else ""
            late_s = _fmt_late_short(o.get("late_minutes") or 0)
            pts_s = f", +{o['points']}" if o.get("points") else ""
            at_s = f" {hm} da" if hm else ""
            lines.append(f"✅ {title}{at_s} tasdiqlandi ({late_s}{pts_s})")
        elif st == "skipped":
            lines.append(f"❌ {title} bekor qilindi")
        elif st == "postponed":
            lines.append(f"⏳ {title} kechiktirildi → {_fmt_postponed(o.get('postponed_to'), d)}")
        elif st == "declined":
            lines.append(f"⏭ {title} — bugun emas")
        elif st == "planned":
            lines.append(f"👍 {title} — boraman")

    pline = _prayer_line(d)
    if pline:
        lines.append("\n" + pline)

    ds = scoring.day_summary(d)
    bonus = f", +{ds['bonus']} ixtiyoriy" if ds["bonus"] else ""
    lines.append(f"⭐ {ds['points']} ball ({ds['done']}/{ds['total']}{bonus})")
    return "\n".join(lines)


async def sync(d: date) -> bool:
    """Shu kun jurnalini tahrirlaydi yoki yangisini yaratadi. Muvaffaqiyatli bo'lsa True."""
    key = f"journal_{d.isoformat()}"
    text = build_text(d)
    mid_s = db.get_setting(key)
    if mid_s and mid_s.isdigit():
        mid = int(mid_s)
        try:
            await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=mid, reply_markup=None)
            db.ex("UPDATE msg_log SET text = ? WHERE message_id = ? AND deleted_at IS NULL", (text, mid))
            return True
        except TelegramBadRequest as e:
            low = str(e).lower()
            if "not modified" in low:
                return True
            if "not found" in low or "can't be edited" in low or "message_id_invalid" in low:
                db.del_setting(key)
            else:
                app.log.error(f"Jurnalni tahrirlashda xatolik: {e}")
                return False
        except Exception as e:
            app.log.error(f"Jurnalni tahrirlashda kutilmagan xato: {e}")
            return False

    try:
        msg = await app.bot.send_message(app.CHAT_ID, text, reply_markup=None, disable_notification=True)
    except Exception as e:
        app.log.error(f"Jurnal yaratishda xatolik: {e}")
        return False
    db.set_setting(key, str(msg.message_id))
    db.ins(
        "INSERT INTO msg_log (message_id, category, text, actionable, sent_at) VALUES (?,?,?,?,?)",
        (msg.message_id, "journal", text, 1, app.now().isoformat()),
    )
    return True


async def record_and_replace(occ_date: date, reminder_message, fallback_text: str):
    """Avval jurnalni yangilaydi; o'xshasa eslatma xabarini o'chiradi, aks holda natijani xabarda qoldiradi."""
    if await sync(occ_date):
        if await messenger.delete_quiet_bool(reminder_message.message_id):
            return
    await messenger.edit_result(reminder_message, fallback_text)


async def record_and_replace_by_id(occ_date: date, message_id: int | None, fallback_text: str):
    if await sync(occ_date):
        if message_id is None or await messenger.delete_quiet_bool(message_id):
            return
    if message_id is not None:
        await messenger.edit_result_by_id(message_id, fallback_text)
    else:
        await messenger.send(fallback_text, "confirm")
