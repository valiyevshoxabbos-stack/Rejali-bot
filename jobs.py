"""Kunlik rejalashtirish: har kuni 00:05 da va kerak bo'lganda qayta chaqiriladi."""

from datetime import datetime, timedelta
import traceback

from apscheduler.triggers.cron import CronTrigger

import app
import db
import feat_qazo
import feat_reminders
import feat_sleep
import feat_summary
import messenger
import qazo as qz
import services as sv

PRAYERS = ("Bomdod", "Peshin", "Asr", "Shom", "Xufton")
JOB_PREFIXES = ("rem_", "info_", "qazo_dl_", "edu_pre_", "edu_post_", "uni_pre_", "uni_bag_", "uni_leave_")


async def send_info(text: str):
    await messenger.send(f"ℹ️ {app.esc(text)}", "info", bind_trigger=False)


def _clear_day_jobs():
    for job in app.scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIXES):
            job.remove()


async def schedule_today():
    """
    Bugungi barcha eslatmalarni to'liq izolyatsiya qilingan holda qayta tuzadi.
    Tashqi API (Ob-havo yoki Namoz) xato bersa ham, barcha lokal eslatmalar (Zal, Darslar, Dorixona)
    100% scheduler'ga qo'shiladi va xatolar system_error_logs ga yoziladi.
    """
    today = app.today()
    now = app.now()

    # 1. Muddati tugagan off holatidagi eslatmalarni tiklash
    try:
        resumed = sv.resume_expired_off()
        if resumed:
            await messenger.send("🔔 Eslatma qaytdi: " + ", ".join(app.esc(t) for t in resumed), "info", bind_trigger=False)
    except Exception as e:
        db.log_error("ResumeExpiredOffError", str(e), traceback.format_exc(), "jobs.schedule_today")

    _clear_day_jobs()

    valid = set()
    open_sleep = feat_sleep.has_open_sleep()

    # 2. Standart va maxsus kunlik eslatmalar (Zal, Yotish, Checkpoints)
    try:
        for rem in sv.reminders_for_day(today):
            if rem.get("slug") == "yotish" and open_sleep:
                continue
            valid.add(feat_reminders.schedule_reminder_today(rem, today))

        for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND reminder_id IS NOT NULL "
                        "AND status = 'pending' AND is_rescheduled = 0 AND is_manual = 0", (today.isoformat(),)):
            if row["id"] not in valid and row["scheduled_at"] > now.isoformat():
                db.ex("DELETE FROM occurrences WHERE id = ?", (row["id"],))
    except Exception as e:
        db.log_error("StandardRemindersError", str(e), traceback.format_exc(), "jobs.schedule_today")

    # 3. Kechiktirib ko'chirilgan va rejalashtirilgan eslatmalar
    try:
        for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND status = 'pending' "
                        "AND is_rescheduled = 1", (today.isoformat(),)):
            app.schedule_job(datetime.fromisoformat(row["scheduled_at"]), feat_reminders.send_reminder, [row["id"]],
                             f"rem_{row['id']}")

        for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND status = 'planned' "
                        "AND is_optional = 1", (today.isoformat(),)):
            app.schedule_job(datetime.fromisoformat(row["scheduled_at"]), feat_reminders.send_planned_optional,
                             [row["id"]], f"rem_plan_{row['id']}")
    except Exception as e:
        db.log_error("OccurrencesError", str(e), traceback.format_exc(), "jobs.schedule_today")

    # 4. Namoz vaqtlari va qazo muddatlari (Tashqi API)
    try:
        times = await feat_summary.prayer_times_today()
        if times:
            dts = [app.at(today, times[name]) for name in PRAYERS]
            for idx, name in enumerate(PRAYERS):
                occ_id = sv.ensure_prayer_occurrence(name, today, times[name])
                if dts[idx] > now:
                    app.schedule_job(dts[idx], feat_reminders.send_reminder, [occ_id], f"rem_{occ_id}")
                if idx < 4:
                    dl = dts[idx + 1] + timedelta(minutes=qz.QAZO_GRACE_MINUTES)
                    if dl > now:
                        app.schedule_job(dl, feat_qazo.check_qazo, [], f"qazo_dl_{idx}")
            # Kechagi Xufton qazosi
            dl_prev = dts[0] + timedelta(minutes=qz.QAZO_GRACE_MINUTES)
            if dl_prev > now:
                app.schedule_job(dl_prev, feat_qazo.check_qazo, [], "qazo_dl_prev")
        else:
            db.log_error("PrayerScheduleError", "Namoz vaqtlarini olib bo'lmadi (API ishlamayapti)", "", "jobs.schedule_today")
    except Exception as e:
        db.log_error("PrayerScheduleError", str(e), traceback.format_exc(), "jobs.schedule_today")
        app.log.warning(f"Namoz vaqtlarini rejalashtirishda xatolik (lokal eslatmalar daxlsiz qoldi): {e}")

    # 5. Maktab ta'lim darslari
    if "education" in app.OPTIONAL:
        try:
            await app.OPTIONAL["education"].schedule_today_lessons()
        except Exception as e:
            db.log_error("EducationScheduleError", str(e), traceback.format_exc(), "jobs.schedule_today")
            app.log.warning(f"Maktab darslarini rejalashtirishda xatolik: {e}")

    # 6. Universitet darslari
    if "uni" in app.OPTIONAL:
        try:
            await app.OPTIONAL["uni"].schedule_today_uni()
        except Exception as e:
            db.log_error("UniScheduleError", str(e), traceback.format_exc(), "jobs.schedule_today")
            app.log.warning(f"Universitet darslarini rejalashtirishda xatolik: {e}")

    # 7. Salomatlik va davolanish kurslari
    if "health" in app.OPTIONAL:
        try:
            if hasattr(app.OPTIONAL["health"], "schedule_course_reminders"):
                app.OPTIONAL["health"].schedule_course_reminders()
        except Exception as e:
            db.log_error("HealthScheduleError", str(e), traceback.format_exc(), "jobs.schedule_today")
            app.log.warning(f"Davolanish kurslarini rejalashtirishda xatolik: {e}")

    app.log.info(f"{today} uchun eslatmalar to'liq izolyatsiyalangan holda rejalashtirildi.")


# Alias: plan_day
plan_day = schedule_today


async def self_healing_check():
    """
    Self-Healing: Har 30 daqiqada orqa fonda tekshiradi.
    Agar tunda Namoz vaqtlari API ishlamay qolgan bo'lsa, qayta ulanib bugungi qolgan namozlarni tiklaydi.
    """
    today = app.today()
    now = app.now()
    occ_cnt = db.q1("SELECT COUNT(*) AS c FROM occurrences WHERE date = ? AND prayer IS NOT NULL", (today.isoformat(),))["c"]
    if occ_cnt < 5:
        try:
            times = await feat_summary.prayer_times_today()
            if times:
                dts = [app.at(today, times[name]) for name in PRAYERS]
                for idx, name in enumerate(PRAYERS):
                    occ_id = sv.ensure_prayer_occurrence(name, today, times[name])
                    if dts[idx] > now:
                        app.schedule_job(dts[idx], feat_reminders.send_reminder, [occ_id], f"rem_{occ_id}")
                    if idx < 4:
                        dl = dts[idx + 1] + timedelta(minutes=qz.QAZO_GRACE_MINUTES)
                        if dl > now:
                            app.schedule_job(dl, feat_qazo.check_qazo, [], f"qazo_dl_{idx}")
                app.log.info(f"Self-healing: {today} uchun namoz eslatmalari avto-tiklandi.")
        except Exception as e:
            app.log.debug(f"Self-healing tekshiruvida kutish: {e}")


def register_jobs(scheduler):
    scheduler.add_job(schedule_today, CronTrigger(hour=0, minute=5), id="daily_scheduler", replace_existing=True)
    scheduler.add_job(self_healing_check, CronTrigger(minute="*/30"), id="self_healing_job", replace_existing=True)
    if "education" in app.OPTIONAL:
        scheduler.add_job(
            app.OPTIONAL["education"].send_evening_preview,
            CronTrigger(hour=20, minute=0),
            id="edu_evening_preview",
            replace_existing=True,
        )
