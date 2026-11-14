"""
AltronBek 3.6: kirish nuqtasi (yassi tuzilma: feat_*.py) va Startup Changelog mexanizmi.

Asosiy modullar majburiy: yo'q bo'lsa bot ishga tushmaydi (xato darhol ko'rinadi).
Ixtiyoriy modullar (feat_weather, feat_calculator, feat_challenges, feat_finance, feat_market) mavjud bo'lsa avtomatik ulanadi.
"""

import asyncio
import importlib
import os
import traceback
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
OPTIONAL = ["weather", "calculator", "challenges", "finance", "market", "health", "education"]

CHANGELOGS: dict[str, str] = {
    "3.6": (
        "🚀 <b>AltronBek v3.6 yangilanishi muvaffaqiyatli o'rnatildi!</b>\n"
        "• 🛒 <b>Smart Bozorlik:</b> Rejalashtirilgan ro'yxat va ayni damdagi xarid rejimlari.\n"
        "• 📈 <b>Inflyatsiya monitoringi:</b> Narxlarning oldingi xaridga nisbatan dinamikasi.\n"
        "• 🛡 <b>Anti-Cheat:</b> Narxning asossiz sakrashi (+35%) bo'yicha ogohlantirish.\n"
        "• 📍 <b>Savdo joylari tahlili:</b> Teglar bo'yicha qayerda arzonroq ekanini aniqlash."
    ),
    "3.7": (
        "🚀 <b>AltronBek v3.7 yangilanishi muvaffaqiyatli o'rnatildi!</b>\n"
        "• 💊 <b>Oddiy Dorixona:</b> Uyda va sumkadagi dorilar zaxirasi, tugash va takroriy doza ogohlantirishlari.\n"
        "• 🩺 <b>Davolanish kursi:</b> Shifokor buyurgan dorilar jadvali va eslatmalari.\n"
        "• 👨‍⚕️ <b>Virtual Dorixonachi:</b> Belgilar tahlili, xavf (Red Flag) filtri va tabiiy tavsiyalar.\n"
        "• 🧴 <b>Skin Care & Tonggi gigiyena:</b> Har kunlik 06:45 parvarish va ob-havoga mos himoya."
    ),
    "3.8": (
        "🚀 <b>AltronBek v3.8 yangilanishi muvaffaqiyatli o'rnatildi!</b>\n"
        "• 🎓 <b>Maktab Ta'lim Moduli:</b> O'qituvchi uchun to'liq dinamik dars va TMR boshqaruvi.\n"
        "• 📥 <b>Excel (.xlsx) TMR Importer:</b> Taqvim-mavzu rejalarni fayldan yuklash va joriy mavzuni tanlash.\n"
        "• 🔬 <b>Laboratoriya va Nazorat ogohlantirishlari:</b> 1 kun oldin (20:00 da) reja va maxsus eslatmalar.\n"
        "• 🔔 <b>Darsdan 10 daqiqa oldin eslatma:</b> Mavzu, uy ishi va sinfga oid maxsus eslatmalar.\n"
        "• 🏁 <b>Dars tasdiqlash:</b> Dars tugaganda o'tildimi so'rovi va avtomatik keyingi mavzuga siljitish.\n"
        "• 📅 <b>2026-2027 Ta'til va bayramlar taqvimi:</b> Dam olish kunlarida dars eslatmalari avtomatik to'xtatiladi.\n"
        "• 🧹 <b>Eski maktab va universitet eslatmalari to'liq tozalandi.</b>"
    ),
    "3.8.1": (
        "🚀 <b>AltronBek v3.8.1 yangilanishi muvaffaqiyatli o'rnatildi!</b>\n"
        "• ⏰ <b>Dars eslatmalari barqarorligi:</b> Kechki 20:00 preview saqlanishi va dars jadvali o'zgarganda darhol rejalashtirish tuzatildi.\n"
        "• 🌅 <b>06:30 Tonggi Xulosa:</b> Bugungi maktab darslari, mavzulari va izohlari to'g'ridan-to'g'ri tonggi xabarda chiqadi.\n"
        "• ⏭ <b>Navbatdagi dars ko'rsatkichi:</b> Dars tugaganda 10 daqiqadan so'ng boshlanadigan keyingi dars haqida darhol bildirishnoma beriladi.\n"
        "• 📋 <b>Tezkor ko'rsatkich:</b> Maktab menyusiga «Bugungi / Ertagi darslar» tugmasi qo'shildi."
    ),
}

COMMANDS = [
    ("bugun", "Bugungi reja"),
    ("maktab", "Maktab ta'lim moduli (darslar, TMR, jadval)"),
    ("talim", "Maktab ta'lim moduli"),
    ("chelenjlar", "Chelenjlar paneli"),
    ("moliya", "Moliya boshqaruvi"),
    ("bozorlik", "Smart Bozorlik va savat"),
    ("salomatlik", "Salomatlik va dorixona markazi"),
    ("dorixona", "Uy aptechkasi"),
    ("diagnostika", "Tizim diagnostikasi"),
    ("narx", "Mahsulot narxi va joylar tahlili"),
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


ERROR_ALERT_TIMESTAMPS: dict[str, list[float]] = {}
MAX_ALERTS_PER_HOUR = 3


def setup():
    app.dp.update.outer_middleware(OwnerOnly())

    @app.dp.errors()
    async def on_error(event: ErrorEvent):
        exc = event.exception
        err_type = type(exc).__name__
        err_msg = str(exc)
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        ctx_info = str(getattr(event, "update", ""))

        app.log.exception(f"Kutilmagan xatolik: {exc}")
        try:
            db.ins(
                "INSERT INTO system_error_logs (error_type, error_message, traceback_text, context_info, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (err_type, err_msg, tb_text, ctx_info[:500], app.now().isoformat()),
            )
        except Exception as log_err:
            app.log.error(f"Xatoni bazaga yozishda xatolik: {log_err}")

        # Anti-spam: max 3 alerts per hour per error signature
        sig = f"{err_type}:{err_msg}"
        now_ts = app.now().timestamp()
        history = [t for t in ERROR_ALERT_TIMESTAMPS.get(sig, []) if now_ts - t < 3600]
        if len(history) < MAX_ALERTS_PER_HOUR:
            history.append(now_ts)
            ERROR_ALERT_TIMESTAMPS[sig] = history
            try:
                alert_text = (
                    f"⚠️ <b>Tizimda xatolik yuz berdi!</b>\n"
                    f"<b>Turi:</b> {app.esc(err_type)}\n"
                    f"<b>Xabar:</b> <code>{app.esc(err_msg[:200])}</code>"
                )
                await messenger.send(alert_text, "system", bind_trigger=False)
            except Exception:
                pass
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


async def check_startup_changelog() -> bool:
    """
    PRAGMA user_version yoki joriy release (app.VERSION) o'zgargan bo'lsa,
    Telegramga yangilanish (Startup Changelog) kartochkasini bir marta yuboradi.
    """
    cur_schema = str(db.schema_version())
    prev_schema = db.get_setting("last_schema_version")
    prev_release = db.get_setting("last_release")
    schema_updated = (db.LAST_MIGRATED_FROM is not None) or (prev_schema != cur_schema)
    release_updated = (prev_release != app.VERSION)
    if not (schema_updated or release_updated):
        return False
    db.set_setting("last_schema_version", cur_schema)
    db.set_setting("last_release", app.VERSION)
    card = CHANGELOGS.get(app.VERSION)
    if not card:
        return False
    await messenger.send(card, "system", bind_trigger=False)
    return True


async def amain():
    backups = db.init_db()
    if db.get_setting("epoch") is None:
        db.set_setting("epoch", app.now().replace(microsecond=0).isoformat())
    sv.seed_standard_reminders()
    db.seed_health_data()
    db.seed_edu_data()
    setup()
    if "finance" in app.OPTIONAL:
        app.OPTIONAL["finance"].seed_finance_categories()
    app.scheduler.start()
    await jobs.schedule_today()
    try:
        await app.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    except Exception as e:
        app.log.warning(f"Buyruqlar ro'yxatini o'rnatib bo'lmadi: {e}")
    if backups:
        await _send_migration_backups(backups)
    await check_startup_changelog()
    import feat_status
    await feat_status.startup_message()
    if "finance" in app.OPTIONAL:
        await app.OPTIONAL["finance"].startup_sync()
    await app.dp.start_polling(app.bot)


def run():
    asyncio.run(amain())


if __name__ == "__main__":
    run()
