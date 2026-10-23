"""Statistika: bo'limlar (Reja, Namoz va qazo, Uyqu, Vazn), har birida Hafta/Oy."""

from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import app
import db
import messenger
import scoring
from features import sleep, weight

router = Router()
BARS = "▁▂▃▄▅▆▇█"
SECTIONS = [("plan", "📋 Reja"), ("pray", "🕌 Namoz va qazo"), ("sleep", "😴 Uyqu"),
            ("weight", "⚖️ Vazn"), ("market", "🛒 Bozorlik")]


def bars(values: list[float]) -> str:
    if not values:
        return ""
    top = max(values) or 1
    return "".join(BARS[min(7, int(v / top * 7))] for v in values)


def menu_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"sx:{key}:week")] for key, label in SECTIONS]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def period_row(key: str, period: str) -> list:
    w = ("• " if period == "week" else "") + "Hafta"
    m = ("• " if period == "month" else "") + "Oy"
    return [InlineKeyboardButton(text=w, callback_data=f"sx:{key}:week"),
            InlineKeyboardButton(text=m, callback_data=f"sx:{key}:month"),
            InlineKeyboardButton(text="⬅️", callback_data="sx:menu")]


# ------------------------------------------------------------------ bo'lim matnlari

def plan_text(period: str) -> str:
    today = app.today()
    if period == "week":
        hist = scoring.weekly_history(4, today)
        lines = ["<b>📋 Reja: haftalik natijalar</b>\n"]
        for i, h in enumerate(hist):
            label = "Bu hafta" if i == 0 else f"{h['monday'].strftime('%d.%m')} haftasi"
            bonus = f" +{h['bonus']} ixtiyoriy" if h["bonus"] else ""
            lines.append(f"{label}: {h['points']} ball, {h['done']}/{h['total']} ({h['percent']}%){bonus}")
        lines.append(f"\nO'tgan haftaga nisbatan: {scoring.delta_text(hist[0]['points'], hist[1]['points'])}")
        rec = scoring.record_week(today)
        if rec and rec["points"] > 0:
            lines.append(f"🏆 Rekord: {rec['points']} ball ({rec['monday'].strftime('%d.%m')} haftasi)")
        lines.append("Ball: " + bars([h["points"] for h in reversed(hist)]))
        return "\n".join(lines)
    m = scoring.month_summary(today.year, today.month)
    return (f"<b>📋 Reja: bu oy</b>\n\n{m['points']} ball, {m['done']}/{m['total']} ({m['percent']}%)"
            + (f", +{m['bonus']} ixtiyoriy" if m["bonus"] else ""))


def pray_text(period: str) -> str:
    days = 7 if period == "week" else 30
    since = (app.today() - timedelta(days=days - 1)).isoformat()
    rows = db.q("SELECT status FROM occurrences WHERE prayer IS NOT NULL AND date >= ? AND scheduled_at <= ?",
                (since, app.now().isoformat()))
    if not rows:
        return "<b>🕌 Namoz va qazo</b>\n\nHali ma'lumot yo'q."
    total = len(rows)
    on_time = sum(1 for r in rows if r["status"] == "done")
    qazo_made = sum(1 for r in rows if r["status"] in ("qazo", "qazo_done"))
    qazo_done = sum(1 for r in rows if r["status"] == "qazo_done")
    unmarked = total - on_time - qazo_made
    opened = db.q1("SELECT COUNT(*) AS c FROM qazo WHERE status = 'open'")["c"]
    return (f"<b>🕌 Namoz va qazo</b> ({'7 kun' if days == 7 else '30 kun'})\n\n"
            f"Vaqtida belgilangan: {on_time}/{total} ({round(on_time / total * 100)}%)\n"
            f"Qazo bo'lgan: {qazo_made} (shundan o'qilgan: {qazo_done})\n"
            f"Belgilanmagan: {unmarked}\nHozir ochiq qazo: {opened}")


def sleep_text(period: str) -> str:
    days = 7 if period == "week" else 30
    s = sleep.stats(days)
    if not s["nights"]:
        return ("<b>😴 Uyqu</b>\n\nHali ma'lumot yo'q. Yotish eslatmasida ✅ bosing va ertalab "
                "\"uyg'ondim\" xabariga javob bering.")
    return (f"<b>😴 Uyqu</b> ({days} kun)\n\nYozilgan tunlar: {s['nights']}\n"
            f"O'rtacha uyqu: <b>{sleep.fmt_min(s['avg'])}</b>\nO'rtacha uyquga ketish: {s['avg_start']}\n"
            f"Tunlar: {bars(s['series'][-14:])}\n\n"
            "<i>Hisob taxminiy: yotishni tasdiqlagan payt + kechikish.</i>")


def weight_text(period: str) -> str:
    s = weight.stats(8 if period == "week" else 26)
    if not s["series"]:
        return "<b>⚖️ Vazn</b>\n\nHali ma'lumot yo'q. /vazn 72,4"
    lines = ["<b>⚖️ Vazn: haftalik trend</b>\n"]
    prev = None
    for monday, kg, primary, n in s["series"][-8:]:
        diff = "" if prev is None else f" ({'+' if kg - prev > 0 else ''}{round(kg - prev, 1)})"
        lines.append(f"{monday[5:]}: <b>{kg} kg</b>{diff}{'' if primary else ' *'}")
        prev = kg
    lines.append("\n* haftalik asosiy o'lchov yo'q, haftaning o'rtachasi")
    lines.append("Trend: " + bars([x[1] - min(y[1] for y in s['series']) + 0.1 for x in s['series'][-8:]]))
    return "\n".join(lines)


def market_text(period: str) -> str:
    return "<b>🛒 Bozorlik</b>\n\nBu bo'lim kalkulyator (3.2) bilan birga qo'shiladi."


RENDER = {"plan": plan_text, "pray": pray_text, "sleep": sleep_text, "weight": weight_text, "market": market_text}


@router.message(Command("statistika"))
async def cmd_stats(message: Message):
    await messenger.send("📊 <b>Statistika</b>: bo'limni tanlang", "info", menu_kb())


@router.callback_query(F.data == "sx:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text("📊 <b>Statistika</b>: bo'limni tanlang", reply_markup=messenger.with_delete(menu_kb()))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^sx:(plan|pray|sleep|weight|market):(week|month)$"))
async def cb_section(cb: CallbackQuery):
    _, key, period = cb.data.split(":")
    kb = InlineKeyboardMarkup(inline_keyboard=[period_row(key, period)])
    await cb.message.edit_text(RENDER[key](period), reply_markup=messenger.with_delete(kb))
    await cb.answer()
