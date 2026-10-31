"""Kunlik rejalashtirish: har kuni 00:05 da va kerak bo'lganda qayta chaqiriladi."""

from datetime import datetime, timedelta

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
JOB_PREFIXES = ("rem_", "info_", "wake_prompt", "uni_", "qazo_dl_")


async def send_info(text: str):
    await messenger.send(f"ℹ️ {app.esc(text)}", "info", bind_trigger=False)


def _clear_day_jobs():
    for job in app.scheduler.get_jobs():
        if job.id.startswith(JOB_PREFIXES):
            job.remove()


async def schedule_today():
    """Bugungi barcha eslatmalarni qayta quradi (vaqt o'zgargan bo'lsa eskilarini olib tashlaydi)."""
    today = app.today()
    now = app.now()

    resumed = sv.resume_expired_off()
    if resumed:
        await messenger.send("🔔 Eslatma qaytdi: " + ", ".join(app.esc(t) for t in resumed), "info", bind_trigger=False)

    _clear_day_jobs()

    valid = set()
    for rem in sv.reminders_for_day(today):
        valid.add(feat_reminders.schedule_reminder_today(rem, today))

    # Vaqti o'zgargani yoki o'chirilgani sabab yaroqsiz bo'lgan kelajakdagi yozuvlarni tozalash
    for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND reminder_id IS NOT NULL "
                    "AND status = 'pending' AND is_rescheduled = 0 AND is_manual = 0", (today.isoformat(),)):
        if row["id"] not in valid and row["scheduled_at"] > now.isoformat():
            db.ex("DELETE FROM occurrences WHERE id = ?", (row["id"],))

    # Kechiktirib bugunga ko'chirilgan eslatmalar
    for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND status = 'pending' "
                    "AND is_rescheduled = 1", (today.isoformat(),)):
        app.schedule_job(datetime.fromisoformat(row["scheduled_at"]), feat_reminders.send_reminder, [row["id"]],
                         f"rem_{row['id']}")

    # Ixtiyoriy eslatmalardan "Boraman" deb belgilanganlari
    for row in db.q("SELECT id, scheduled_at FROM occurrences WHERE date = ? AND status = 'planned' "
                    "AND is_optional = 1", (today.isoformat(),)):
        app.schedule_job(datetime.fromisoformat(row["scheduled_at"]), feat_reminders.send_planned_optional,
                         [row["id"]], f"rem_plan_{row['id']}")

    times = await feat_summary.prayer_times_today()
    if times:
        dts = [app.at(today, times[name]) for name in PRAYERS]
        for idx, name in enumerate(PRAYERS):
            occ_id = sv.ensure_prayer_occurrence(name, today, times[name])
            app.schedule_job(dts[idx], feat_reminders.send_reminder, [occ_id], f"rem_{occ_id}")
            if idx < 4:
                dl = dts[idx + 1] + timedelta(minutes=qz.QAZO_GRACE_MINUTES)
                app.schedule_job(dl, feat_qazo.check_qazo, [], f"qazo_dl_{idx}")
        # Kechagi Xufton qazosi (bugungi Bomdod + 15 daqiqa)
        app.schedule_job(dts[0] + timedelta(minutes=qz.QAZO_GRACE_MINUTES), feat_qazo.check_qazo, [], "qazo_dl_prev")
        if not feat_sleep.woke_today(today):
            app.schedule_job(dts[0], feat_sleep.send_wake_prompt, [], "wake_prompt")

    if not sv.pause_active(today):
        for item in sv.school_lesson_reminders(today):
            app.schedule_job(item["at"], send_info, [item["text"]], f"info_{item['at'].strftime('%H%M')}")
        uni = sv.university_reminder(today)
        if uni:
            app.schedule_job(uni["at"], send_info, [uni["text"]], "uni_info")
    app.log.info(f"{today} uchun eslatmalar rejalashtirildi.")


def register_jobs(scheduler):
    scheduler.add_job(schedule_today, CronTrigger(hour=0, minute=5), id="daily_scheduler", replace_existing=True)
