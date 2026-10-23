"""
AltronBek 3.0: kirish nuqtasi.

Funksiyalar alohida modullar (features/). Asosiy modullar majburiy: yo'q bo'lsa bot
ishga tushmaydi (xato darhol ko'rinadi). Ixtiyoriy modullar (weather, calculator ...)
yo'q bo'lsa, menyu "keyingi versiyada" deb javob beradi.
"""

import asyncio
import importlib
from datetime import datetime

from aiogram import BaseMiddleware
from aiogram.types import BotCommand, ErrorEvent

import app
import db
import jobs
import messenger
import services as sv

CORE = ["menu", "reminders", "qazo_feature", "sleep", "birthdays", "weight", "stats",
        "settings", "cleanup", "summary", "status"]
OPTIONAL = ["weather", "calculator"]

COMMANDS = [("bugun", "Bugungi reja"), ("qoshish", "Yangi eslatma"), ("royxat", "Maxsus eslatmalar"),
            ("qazolar", "Ochiq qazolar"), ("statistika", "Statistika"), ("vazn", "Vazn kiritish"),
            ("uyqu", "Uyqu"), ("tkunlar", "Tug'ilgan kunlar"), ("sozlamalar", "Sozlamalar"),
            ("holat", "Bot holati"), ("bekor", "Jarayonni bekor qilish")]


class OwnerOnly(BaseMiddleware):
    """
    1) Faqat CHAT_ID egasidan kelgan update'lar o'tadi; begonalarniki jimgina tashlab yuboriladi.
    2) Baza yaratilishidan oldin yuborilgan xabarlarning tugmalari rad etiladi: oldingi versiya
       (2.0) dan qolgan eski tugma yangi bazadagi boshqa yozuvni belgilab yubormasligi uchun.
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
                    cb.id, text="Bu xabar oldingi versiyadan qolgan, tugmalari ishlamaydi. Menyudan foydalaning.",
                    show_alert=True)
            except Exception as e:
                app.log.warning(f"Eski tugmaga javob berib bo'lmadi: {e}")
            return None
        return await handler(event, data)

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
        loaded.append(importlib.import_module(f"features.{name}"))
    for name in OPTIONAL:
        try:
            mod = importlib.import_module(f"features.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"features.{name}":
                raise  # modul bor, lekin ichidagi import xato: yashirmaymiz
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
        app.dp.include_router(mod.router)
    for mod in features + [jobs]:
        if hasattr(mod, "register_jobs"):
            mod.register_jobs(app.scheduler)
    return features


async def amain():
    db.init_db()
    if db.get_setting("epoch") is None:
        db.set_setting("epoch", datetime.now(app.TZ).replace(microsecond=0).isoformat())  # shu paytdan oldingi xabar tugmalari ishlamaydi
    sv.seed_standard_reminders()
    setup()
    app.scheduler.start()
    await jobs.schedule_today()
    try:
        await app.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception as e:
        app.log.warning(f"Buyruqlar ro'yxatini o'rnatib bo'lmadi: {e}")
    from features import status
    await status.startup_message()
    await app.dp.start_polling(app.bot)


def run():
    asyncio.run(amain())


if __name__ == "__main__":
    run()
