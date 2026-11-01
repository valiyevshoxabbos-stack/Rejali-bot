"""
AltronBek 3.0.1: xabarlar bilan ishlash.

- Bot yuborgan barcha xabarlar msg_log'da toifasi bilan qayd etiladi.
- Harakat talab qilmaydigan xabarlar tagiga "🗑 O'chirish" tugmasi qo'shiladi.
- Bot xabari o'chganda (qo'lda yoki avto), uni chaqirgan foydalanuvchi xabari ham o'chadi
  (Sozlamalarda `delete_user_msgs` yoqiq bo'lsa; standart yoqiq).
- Biror toifa 7 kunda 5 marta o'chirilsa, u avtomatik o'chadigan bo'ladi (bir marta xabar beriladi).
- O'chirilgan xabar matni 7 kun saqlanadi; "tiklash" = matnni qayta yuborish.
- Telegram cheklovi: 48 soatdan eski xabarni bot o'chira olmaydi.
"""

from contextvars import ContextVar
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import app
import db

current_trigger: ContextVar[int | None] = ContextVar("current_trigger", default=None)

LABELS = {
    "info": "Ma'lumot xabarlari",
    "summary": "Kunlik xulosalar",
    "confirm": "Tasdiqlash xabarlari",
    "birthday": "Tug'ilgan kunlar",
    "reminder_done": "Javob berilgan eslatmalar",
    "system": "Tizim xabarlari",
}
DEFAULT_DELAY = 120
DELAY_STEPS = [30, 120, 360, 1440]
LEARN_THRESHOLD = 5
LEARN_DAYS = 7
TRASH_DAYS = 7


def delay_text(minutes: int) -> str:
    return f"{minutes} daq" if minutes < 60 else f"{minutes // 60} soat"


def user_msgs_enabled() -> bool:
    return db.get_setting("delete_user_msgs", "1") == "1"


def set_user_msgs_enabled(enabled: bool):
    db.set_setting("delete_user_msgs", "1" if enabled else "0")


def with_delete(kb: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
    rows = [list(r) for r in kb.inline_keyboard] if kb else []
    rows.append([InlineKeyboardButton(text="🗑 O'chirish", callback_data="del")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _quiet() -> bool:
    """Jim rejim: kechqurundan bomdodgacha ovozsiz."""
    t = app.now().strftime("%H:%M")
    return t >= db.get_setting("quiet_start", "22:30") or t < db.get_setting("quiet_end", "05:25")


async def send(
    text: str,
    category: str = "info",
    kb=None,
    actionable: bool = False,
    deletable: bool | None = None,
    bind_trigger: bool = True,
):
    """Xabar yuboradi va qayd etadi. Muvaffaqiyatsiz bo'lsa None."""
    if deletable is None:
        deletable = (not actionable) and not isinstance(kb, ReplyKeyboardMarkup)
    markup = kb
    if deletable and (kb is None or isinstance(kb, InlineKeyboardMarkup)):
        markup = with_delete(kb)
    try:
        msg = await app.bot.send_message(app.CHAT_ID, text, reply_markup=markup, disable_notification=_quiet())
    except Exception as e:
        app.log.error(f"Xabar yuborishda xatolik: {e}")
        return None
    now_iso = app.now().isoformat()
    db.ins(
        "INSERT INTO msg_log (message_id, category, text, actionable, sent_at) VALUES (?,?,?,?,?)",
        (msg.message_id, category, text, int(actionable), now_iso),
    )
    if bind_trigger and deletable:
        trig = current_trigger.get()
        if trig is not None:
            db.set_setting(f"trg_{msg.message_id}", f"{trig}|{now_iso}")
            current_trigger.set(None)
    return msg


async def edit_actionable(message, text: str, kb: InlineKeyboardMarkup, category: str = "wake"):
    """Xabarni harakat talab qiladigan holatga o'tkazib tahrirlaydi (avto-o'chirish tegmaydi, 🗑 qo'shilmaydi)."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            app.log.error(f"Xabarni tahrirlashda xatolik: {e}")
    db.ex(
        "UPDATE msg_log SET category = ?, actionable = 1, text = ? WHERE message_id = ? AND deleted_at IS NULL",
        (category, text, message.message_id),
    )


async def edit_result(message, text: str, category: str = "reminder_done"):
    """Javob berilgan xabarni yangilaydi: endi harakat talab qilmaydi, o'chirish tugmasi qo'shiladi."""
    try:
        await message.edit_text(text, reply_markup=with_delete(None))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            app.log.error(f"Xabarni yangilashda xatolik: {e}")
    db.ex(
        "UPDATE msg_log SET category = ?, actionable = 0, text = ? WHERE message_id = ? AND deleted_at IS NULL",
        (category, text, message.message_id),
    )


async def edit_result_by_id(message_id: int, text: str, category: str = "reminder_done"):
    try:
        await app.bot.edit_message_text(text, chat_id=app.CHAT_ID, message_id=message_id,
                                        reply_markup=with_delete(None))
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            app.log.error(f"Xabarni yangilashda xatolik: {e}")
    db.ex("UPDATE msg_log SET category = ?, actionable = 0, text = ? WHERE message_id = ? AND deleted_at IS NULL",
          (category, text, message_id))


async def _tg_delete(message_id: int) -> bool:
    try:
        await app.bot.delete_message(app.CHAT_ID, message_id)
        return True
    except TelegramBadRequest as e:
        # Allaqachon o'chirilgan bo'lsa maqsadga erishilgan; 48 soatdan eski bo'lsa False qaytaradi
        return "not found" in str(e).lower()
    except Exception as e:
        app.log.error(f"O'chirishda xatolik: {e}")
        return False


async def _delete_linked_user_msg(bot_message_id: int):
    """Bot xabariga bog'langan foydalanuvchi xabarini o'chiradi (sozlama yoqiq bo'lsa)."""
    key = f"trg_{bot_message_id}"
    raw = db.get_setting(key)
    if not raw:
        return
    db.del_setting(key)
    if not user_msgs_enabled():
        return
    uid_part = raw.split("|", 1)[0]
    if uid_part.isdigit():
        await _tg_delete(int(uid_part))


async def delete_quiet_bool(message_id: int) -> bool:
    """Oqim/jurnal xabarlarini jimgina o'chirish: tiklash ro'yxatiga tushmaydi."""
    if await _tg_delete(message_id):
        db.ex("UPDATE msg_log SET deleted_at = ?, deleted_by = 'flow', text = NULL "
              "WHERE message_id = ? AND deleted_at IS NULL", (app.now().isoformat(), message_id))
        await _delete_linked_user_msg(message_id)
        return True
    return False


async def delete_quiet(message_id: int):
    await delete_quiet_bool(message_id)


async def delete_user_quiet(message_id: int):
    """FSM oqimidagi foydalanuvchi javobini o'chirish (sozlama yoqiq bo'lsa, 48s xatosi yutiladi)."""
    if user_msgs_enabled():
        await _tg_delete(message_id)


# ------------------------------------------------------------------ afzalliklar va o'rganish

def get_pref(category: str) -> dict:
    row = db.q1("SELECT * FROM cleanup_prefs WHERE category = ?", (category,))
    return row or {"category": category, "auto_delete": 0, "delay_min": DEFAULT_DELAY}


def set_pref(category: str, auto: bool | None = None, delay: int | None = None):
    cur = get_pref(category)
    new_auto = cur["auto_delete"] if auto is None else int(auto)
    new_delay = cur["delay_min"] if delay is None else delay
    db.ex(
        "INSERT INTO cleanup_prefs (category, auto_delete, delay_min) VALUES (?,?,?) "
        "ON CONFLICT(category) DO UPDATE SET auto_delete = excluded.auto_delete, delay_min = excluded.delay_min",
        (category, new_auto, new_delay),
    )
    if auto is False:
        db.ex("DELETE FROM cleanup_events WHERE category = ?", (category,))


def _learn(category: str) -> bool:
    """Toifa avtomatik o'chadigan bo'lib qolgan bo'lsa True."""
    if get_pref(category)["auto_delete"]:
        return False
    since = (app.now() - timedelta(days=LEARN_DAYS)).isoformat()
    count = db.q1("SELECT COUNT(*) AS c FROM cleanup_events WHERE category = ? AND at >= ?", (category, since))["c"]
    if count >= LEARN_THRESHOLD:
        set_pref(category, auto=True, delay=get_pref(category)["delay_min"])
        return True
    return False


async def user_delete(message_id: int) -> dict:
    """Foydalanuvchi 🗑 bosdi."""
    row = db.q1("SELECT * FROM msg_log WHERE message_id = ? AND deleted_at IS NULL ORDER BY id DESC", (message_id,))
    if not await _tg_delete(message_id):
        return {"ok": False}
    await _delete_linked_user_msg(message_id)
    category = row["category"] if row else "info"
    if row:
        db.ex("UPDATE msg_log SET deleted_at = ?, deleted_by = 'user' WHERE id = ?", (app.now().isoformat(), row["id"]))
        db.ins("INSERT INTO cleanup_events (category, at) VALUES (?,?)", (category, app.now().isoformat()))
    return {"ok": True, "category": category, "switched": _learn(category) if row else False}


async def cleanup_tick():
    """Har 10 daqiqada: avto-o'chadigan toifalarni tozalaydi, eski yozuvlarni yo'qotadi."""
    now = app.now()
    prefs = {r["category"]: r for r in db.q("SELECT * FROM cleanup_prefs WHERE auto_delete = 1")}
    if prefs:
        for r in db.q("SELECT * FROM msg_log WHERE deleted_at IS NULL AND actionable = 0"):
            p = prefs.get(r["category"])
            if not p:
                continue
            age = now - datetime.fromisoformat(r["sent_at"])
            if age < timedelta(minutes=p["delay_min"]):
                continue
            if age > timedelta(hours=47):
                db.ex("UPDATE msg_log SET deleted_at = ?, deleted_by = 'expired' WHERE id = ?", (now.isoformat(), r["id"]))
                db.del_setting(f"trg_{r['message_id']}")
            elif await _tg_delete(r["message_id"]):
                await _delete_linked_user_msg(r["message_id"])
                db.ex("UPDATE msg_log SET deleted_at = ?, deleted_by = 'auto' WHERE id = ?", (now.isoformat(), r["id"]))
            else:
                db.ex("UPDATE msg_log SET deleted_at = ?, deleted_by = 'expired' WHERE id = ?", (now.isoformat(), r["id"]))
                db.del_setting(f"trg_{r['message_id']}")
    cutoff = (now - timedelta(days=TRASH_DAYS + 1)).isoformat()
    db.ex("DELETE FROM msg_log WHERE sent_at < ?", (cutoff,))
    db.ex("DELETE FROM cleanup_events WHERE at < ?", ((now - timedelta(days=LEARN_DAYS + 1)).isoformat(),))
    for row in db.q("SELECT key, value FROM settings WHERE key LIKE 'trg_%'"):
        parts = row["value"].split("|", 1)
        if len(parts) < 2 or parts[1] < cutoff:
            db.del_setting(row["key"])


def recent_deleted(limit: int = 8) -> list[dict]:
    since = (app.now() - timedelta(days=TRASH_DAYS)).isoformat()
    return db.q(
        "SELECT id, category, text, deleted_at FROM msg_log WHERE deleted_by IN ('user','auto') "
        "AND text IS NOT NULL AND deleted_at >= ? ORDER BY deleted_at DESC LIMIT ?", (since, limit))


async def restore(log_id: int) -> bool:
    row = db.q1("SELECT * FROM msg_log WHERE id = ? AND deleted_by IN ('user','auto') AND text IS NOT NULL", (log_id,))
    if not row:
        return False
    msg = await send(row["text"], "restored", deletable=True, bind_trigger=False)
    if msg is None:
        return False
    db.ex("UPDATE msg_log SET deleted_by = 'restored' WHERE id = ?", (log_id,))
    return True
