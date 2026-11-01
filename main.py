"""
AltronBek 3.1: kirish nuqtasi (yassi tuzilma: feat_*.py).

Asosiy modullar majburiy: yo'q bo'lsa bot ishga tushmaydi (xato darhol ko'rinadi).
Ixtiyoriy modullar (feat_weather, feat_calculator ...) yo'q bo'lsa, menyu "keyingi versiyada" deb javob beradi.
"""

import asyncio
import importlib
import os
from datetime import datetime

from aiogram import BaseMiddleware
from aiogram.types import BotCommand, ErrorEvent, FSInputFile

import app
import db
import jobs
import messenger
import services as sv

CORE = [
    "menu", "reminders", "qazo", "sleep", "birthdays", "weight",
    "stats", "settings", "cleanup", "summary", "status", "restore",
]
OPTIONAL = ["weather", "calculator"]

COMMANDS = [
    ("bugun", "Bugungi reja"),
    ("obhavo", "Ob-havo va kiyim tavsiyasi"),
    ("hisob", "Xarajatlar kalkulyatori"),
    ("qoshish", "Yangi eslatma"),
    ("royxat", "Maxsus eslatmalar"),
    ("qazolar", "Ochiq qazolar"),
    ("statistika", "Statistika"),
    ("vazn", "Vazn kiritish"),
    ("uyqu", "Uyqu"),
    ("yotdim", "Tungi uyquni boshlash"),
    ("uyqu_kiritish", "Uyquni qo'lda kiritish"),
    ("tkunlar", "Tug'ilgan kunlar"),
    ("izohlar", "Saqlangan izohlar"),
    ("tiklash", "Zaxiradan tiklash"),
    ("sozlamalar", "Sozlamalar"),
    ("holat", "Bot holati"),
    ("bekor", "Jarayonni bekor qilish"),
]


class OwnerOnly(BaseMiddleware):
    """
    1) Faqat CHAT_ID egasidan kelgan update'lar o'tadi; begonalarniki jimgina tashlab yuboriladi.
    2) `epoch` dan oldin yuborilgan xabarlarning tugmalari rad etiladi (eski versiya yoki tiklashdan oldingi xabar).
    3) Foydalanuvchi matnli xabari id si `messenger.current_trigger` contextvar'ga yoziladi.
    """

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != app.CHAT_ID:
            app.log.warning(f"Ruxsatsiz urinish: id={getattr(user, 'id', None)}")
            return None
        cb = getattr(event, "callback_query", None)
        if cb is not None and self._is_stale(cb):
            try:
                await app.bot.answer_callback_query(
                    cb.id, text="Bu xabar oldingi baza/versiyadan qolgan, tugmalari ishlamaydi. Menyudan foydalaning.",
                    show_alert=True)
            except Exception as e:
                app.log.warning(f"Eski tugmaga javob berib bo'lmadi: {e}")
            return None
        msg = getattr(event, "message", None)
        trig_id = msg.message_id if (msg is not None and getattr(msg, "document", None) is None) else None
        token = messenger.current_trigger.set(trig_id)
        try:
            return await handler(event, data)
        finally:
            messenger.current_trigger.reset(token)

    @staticmethod
    def _is_stale(cb) -> bool:
        epoch = db.get_setting("epoch")
        sent = getattr(getattr(cb, "message", None), "date", None)
        if not epoch or not isinstance(sent, datetime):
            return False
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=app.TZ)
        return sent < datetime.fromisoformat(epoch)


def load_features() -> list:
    loaded = []
    for name in CORE:
        loaded.append(importlib.import_module(f"feat_{name}"))
    for name in OPTIONAL:
        mod_name = f"feat_{name}"
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as e:
            if e.name != mod_name:
                raise
            continue
        app.OPTIONAL[name] = mod
        loaded.append(mod)
    return loaded


def setup():
    app.dp.update.outer_middleware(OwnerOnly())

    @app.dp.errors()
    async def on_error(event: ErrorEvent):
        app.log.exception(f"Kutilmagan xatolik: {event.exception}")
        return True

    features = load_features()
    for mod in features:
        if hasattr(mod, "router"):
            app.dp.include_router(mod.router)
    for mod in features + [jobs]:
        if hasattr(mod, "register_jobs"):
            mod.register_jobs(app.scheduler)
    return features


async def _send_migration_backups(backups: list[str]):
    for bak in backups:
        if not os.path.exists(bak):
            continue
        try:
            await app.bot.send_document(
                app.CHAT_ID,
                FSInputFile(bak, filename=os.path.basename(bak)),
                caption=f"🛡 Baza yangilanishidan oldingi avtomatik zaxira ({os.path.basename(bak)})",
            )
        except Exception as e:
            app.log.error(f"Migratsiya zaxirasini yuborishda xatolik: {e}")


async def amain():
    backups = db.init_db()
    if db.get_setting("epoch") is None:
        db.set_setting("epoch", app.now().replace(microsecond=0).isoformat())
    sv.seed_standard_reminders()
    setup()
    app.scheduler.start()
    await jobs.schedule_today()
    try:
        await app.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception as e:
        app.log.warning(f"Buyruqlar ro'yxatini o'rnatib bo'lmadi: {e}")
    if backups:
        await _send_migration_backups(backups)
    import feat_status
    await feat_status.startup_message()
    await app.dp.start_polling(app.bot)


def run():
    asyncio.run(amain())


if __name__ == "__main__":
    run()
