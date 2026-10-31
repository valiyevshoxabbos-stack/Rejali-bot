"""
Zaxiradan tiklash (3.0.1): /tiklash yoki Sozlamalar → Ma'lumotlar → Zaxiradan tiklash.

Oqim:
1) Foydalanuvchi `.db` hujjat yuboradi.
2) Baza vaqtincha faylga yuklanib tekshiriladi: SQLite formati, `PRAGMA integrity_check`,
   `user_version` (1..joriy), barcha kerakli jadvallar mavjudligi.
3) Ichidagi ma'lumotlar soni ko'rsatilib tasdiqlash so'raladi.
4) Tasdiqlangach joriy bazadan `<baza>.before-restore-<vaqt>.bak` olinadi, yangi baza o'rnatiladi,
   migratsiya yuritiladi, `epoch` yangilanadi (eski tugmalar bloklanadi) va `schedule_today()` chaqiriladi.
"""

import os
import tempfile

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import app
import db
import feat_common as common
import jobs
import messenger

router = Router()
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


class RestoreState(StatesGroup):
    waiting_file = State()
    confirm = State()


def _cleanup_tmp(tmp_path: str | None):
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def start_restore(state: FSMContext):
    data = await state.get_data()
    _cleanup_tmp(data.get("restore_tmp"))
    await state.clear()
    await state.set_state(RestoreState.waiting_file)
    await common.ask(
        state,
        "📥 <b>Zaxiradan tiklash</b>\n\n"
        "Oldin olingan <code>.db</code> zaxira faylini hujjat sifatida yuboring.\n"
        "Tiklashdan oldin joriy bazadan avtomatik xavfsizlik nusxasi olinadi.",
    )


@router.message(Command("tiklash"))
async def cmd_restore(message: Message, state: FSMContext):
    await start_restore(state)


@router.callback_query(F.data == "rs:start")
async def cb_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await start_restore(state)


@router.message(RestoreState.waiting_file, F.document)
async def got_file(message: Message, state: FSMContext):
    doc = message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await messenger.send("⚠️ Fayl juda katta (50 MB dan oshmasligi kerak).", "confirm", bind_trigger=False)
        return

    fd, tmp_path = tempfile.mkstemp(suffix=".restore.db")
    os.close(fd)
    try:
        await app.bot.download(doc, destination=tmp_path)
    except Exception as e:
        _cleanup_tmp(tmp_path)
        app.log.error(f"Zaxira faylni yuklab olishda xatolik: {e}")
        await messenger.send("⚠️ Faylni yuklab bo'lmadi. Qayta urinib ko'ring.", "confirm", bind_trigger=False)
        return

    check = db.validate_backup_file(tmp_path)
    if not check["ok"]:
        _cleanup_tmp(tmp_path)
        await messenger.send(f"❌ Tiklab bo'lmaydi: {app.esc(check['error'])}", "confirm", bind_trigger=False)
        return

    c = check["counts"]
    await state.set_state(RestoreState.confirm)
    await state.update_data(restore_tmp=tmp_path)
    await common.ask(
        state,
        f"🗂 <b>Zaxira tekshiruvdan o'tdi</b> (sxema v{check['version']})\n\n"
        f"• Tug'ilgan kunlar: <b>{c['birthdays']}</b>\n"
        f"• Maxsus eslatmalar: <b>{c['custom_rem']}</b>\n"
        f"• Reja/namoz yozuvlari: <b>{c['occurrences']}</b>\n"
        f"• Uyqu tunlari: <b>{c['sleep']}</b>\n"
        f"• Vazn yozuvlari: <b>{c['weight']}</b>\n"
        f"• Ochiq qazolar: <b>{c['qazo_open']}</b>\n\n"
        "Joriy baza <code>.before-restore-*.bak</code> sifatida saqlanib, ustidan shu zaxira tiklansinmi?",
        [[("✅ Ha, tiklash", "rs:ok")]],
    )


@router.message(RestoreState.waiting_file)
async def waiting_non_doc(message: Message, state: FSMContext):
    await common.track_user(state, message)
    await messenger.send("Iltimos, <code>.db</code> zaxira faylini hujjat sifatida yuboring yoki ❌ Bekor bosing.",
                         "confirm", bind_trigger=False)


@router.callback_query(RestoreState.confirm, F.data == "rs:ok")
async def cb_confirm_restore(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tmp_path = data.get("restore_tmp")
    if not tmp_path or not os.path.exists(tmp_path):
        await cb.answer("Vaqtincha fayl topilmadi, qaytadan yuboring", show_alert=True)
        await common.clear_prompts(state)
        await state.clear()
        return

    await cb.answer("Tiklanmoqda...")
    await common.clear_prompts(state)
    await state.clear()

    stamp = app.now().strftime("%Y%m%d-%H%M%S")
    try:
        safety_bak = db.restore_from_file(tmp_path, stamp)
    except Exception as e:
        app.log.exception(f"Tiklashda xatolik: {e}")
        await messenger.send(f"❌ Tiklashda xatolik yuz berdi: {app.esc(e)}", "confirm", bind_trigger=False)
        return
    finally:
        _cleanup_tmp(tmp_path)

    # Tiklashdan oldingi barcha eski tugmalar yangi bazadagi boshqa yozuvni o'zgartirmasligi uchun
    db.set_setting("epoch", app.now().replace(microsecond=0).isoformat())
    await jobs.schedule_today()
    await messenger.send(
        f"✅ <b>Zaxira muvaffaqiyatli tiklandi!</b>\n"
        f"Oldingi baza nusxasi: <code>{app.esc(os.path.basename(safety_bak))}</code>\n"
        "Bugungi reja qaytadan rejalashtirildi.",
        "system", bind_trigger=False,
    )
