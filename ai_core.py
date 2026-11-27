"""
AltronBek 4.0.1: Gemini AI Core — Tizim Pasporti (Startup Snapshot),
Multimodal Ovozli Xabarlar, Function Calling Tsikli va Xavfsizlik Qatlami.
"""

import json
import re
import traceback
import uuid
from typing import Any

import app
import db
import feat_schedules as schedules
from features import ai_router, ai_tools

HISTORY_WINDOW = 12
MAX_TOOL_ROUNDS = 5

HEAVY_KEYWORDS = (
    "tahlil", "analiz", "reja tuz", "haftalik reja", "fizika", "masala",
    "formula", "astronomiya", "solishtir", "hisobot", "batafsil", "strategiya",
    "bmi", "kurs ishi", "optimallashtir",
)

MEDICAL_KEYWORDS = (
    "dori", "aptechka", "dorixona", "isitma", "og'riq", "ogriq", "shamollash",
    "yo'tal", "yotal", "bosh og", "oshqozon", "allergiya", "tabletka", "sirop",
    "shifokor", "kasallik", "davolanish", "paratsetamol", "ibuprofen", "tashxis",
    "salomatlik", "harorat", "bosim", "qon",
)


def get_or_create_session_id() -> str:
    sid = db.get_ai_setting("current_session_id")
    if not sid:
        sid = uuid.uuid4().hex[:12]
        db.set_ai_setting("current_session_id", sid)
    return sid


def start_new_session() -> str:
    sid = uuid.uuid4().hex[:12]
    db.set_ai_setting("current_session_id", sid)
    return sid


def save_chat_message(session_id: str, role: str, content: str):
    r = "model" if role == "model" else "user"
    db.ins(
        "INSERT INTO ai_chat_history (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (session_id, r, str(content)[:4000], app.now().isoformat()),
    )


def get_recent_history(session_id: str, limit: int = HISTORY_WINDOW) -> list[dict]:
    rows = db.q(
        "SELECT id, role, content, created_at FROM ai_chat_history "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, int(limit)),
    )
    rows.reverse()
    return rows


def select_tier(text: str | None, has_audio: bool = False) -> str:
    t = (text or "").strip().lower()
    if len(t) >= 220 or any(kw in t for kw in HEAVY_KEYWORDS):
        return "heavy"
    return "light"


def build_system_passport() -> str:
    """
    Ilk startda va har kunlik yangilanishda botning joriy holatidan ixcham
    «Tizim Pasporti» (System Prompt konteksti) yig'adi.
    """
    now_dt = app.now()
    weekday_uz = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"][now_dt.weekday()]

    try:
        accs = db.q("SELECT name, last_4, type FROM finance_accounts WHERE is_active = 1 ORDER BY usage_count DESC LIMIT 10")
        acc_str = ", ".join(f"{a['name']}{'*' + a['last_4'] if a.get('last_4') else ''} ({a['type']})" for a in accs) or "hali yo'q"
    except Exception:
        acc_str = "noma'lum"

    try:
        chs = db.q("SELECT title FROM challenges WHERE status = 'active' LIMIT 8")
        ch_str = ", ".join(c["title"] for c in chs) or "yo'q"
    except Exception:
        ch_str = "yo'q"

    try:
        courses = db.q("SELECT class_name, subject_name FROM edu_courses WHERE is_active = 1 LIMIT 15")
        cls_str = ", ".join(f"{c['class_name']} ({c['subject_name']})" for c in courses) or "hali yo'q"
    except Exception:
        cls_str = "hali yo'q"

    try:
        scheds = schedules.list_system_schedules()
        sched_str = ", ".join(
            f"{s['key']}={schedules.describe_schedule(s)}{'' if int(s['is_enabled']) else '(off)'}"
            for s in scheds
        ) or "yo'q"
    except Exception:
        sched_str = "yo'q"

    modules_str = ", ".join(sorted(app.OPTIONAL.keys())) or "asosiy"

    return (
        "Sen AltronBek v4.0.1 shaxsiy AI yordamchisisan (o'zbek tilida aniq, samimiy va ixcham javob berasan).\n"
        "Foydalanuvchi profili: Umarkhan — fizika va astronomiya o'qituvchisi hamda universitet talabasi.\n"
        f"Joriy sana va vaqt (Asia/Tashkent): {now_dt.strftime('%Y-%m-%d %H:%M')} ({weekday_uz}).\n"
        f"Faol modullar: {modules_str}.\n"
        f"Mavjud moliya hisoblari: {acc_str}.\n"
        f"Faol chelenjlar: {ch_str}.\n"
        f"Maktab sinflari: {cls_str}.\n"
        f"Tizim jadvali kalitlari (system_schedules): {sched_str}.\n\n"
        "QAT'IY QOIDALAR:\n"
        "1. READ-ONLY ma'lumotlar uchun tegishli `get_*` asbobini chaqirib darhol javob ber.\n"
        "2. Bazaga yozish, tahrirlash, o'chirish yoki jadval/eslatma vaqtini o'zgartirish uchun "
        "HECH QACHON bajarildi dema — FAQAT `propose_actions(summary_text, actions)` asbobini chaqir! "
        "Bir xabarda bir nechta modulga oid buyruq bo'lsa, hammasini bitta `propose_actions` ichida jamla.\n"
        "3. CLARIFY FIRST: Agar biror muhim detal noaniq bo'lsa (qaysi karta/kategoriya, vaqtni faqat bugunga surishmi "
        "yoki doimiy, ovozli xabar tushunarsiz bo'lsa) — TAXMIN QILMA, `propose_actions` chaqirma, "
        "qisqa va aniq aniqlashtiruvchi savol ber.\n"
        "4. TIBBIY XAVFSIZLIK: Salomatlik yoki dorilar haqidagi HAR QANDAY javobda 103 tez yordam ogohlantirishini eslat.\n"
        "5. Qo'llab-quvvatlanadigan `propose_actions` turlari: "
        + ", ".join(sorted(ai_tools.SUPPORTED_WRITE_ACTIONS))
        + "."
    )


def refresh_startup_snapshot() -> str:
    """Startup va kunlik yangilanishda tizim pasportini yangilaydi."""
    passport = build_system_passport()
    try:
        db.set_ai_setting("system_passport_updated_at", app.now().isoformat())
    except Exception:
        pass
    return passport


def _needs_medical_disclaimer(user_text: str, reply_text: str, used_tools: set[str]) -> bool:
    if "get_health_status" in used_tools:
        return True
    combined = f"{user_text or ''} {reply_text or ''}".lower()
    return any(kw in combined for kw in MEDICAL_KEYWORDS)


def _extract_function_calls(resp: Any) -> list[dict]:
    """google-genai Response yoki MockResponse dan function_calls ro'yxatini ajratadi."""
    out = []
    fcalls = getattr(resp, "function_calls", None)
    if fcalls:
        for fc in fcalls:
            if isinstance(fc, dict):
                out.append({"name": fc.get("name"), "args": fc.get("args") or {}})
            else:
                name = getattr(fc, "name", None)
                args = getattr(fc, "args", None) or {}
                if not isinstance(args, dict):
                    try:
                        args = dict(args)
                    except Exception:
                        args = {}
                if name:
                    out.append({"name": name, "args": args})
        return out

    # Fallback: candidates[0].content.parts
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc:
                name = getattr(fc, "name", None)
                args = getattr(fc, "args", None) or {}
                if not isinstance(args, dict):
                    try:
                        args = dict(args)
                    except Exception:
                        args = {}
                if name:
                    out.append({"name": name, "args": args})
    return out


def _extract_text(resp: Any) -> str:
    txt = getattr(resp, "text", None)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    candidates = getattr(resp, "candidates", None) or []
    chunks = []
    for cand in candidates:
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            pt = getattr(part, "text", None)
            if isinstance(pt, str) and pt.strip():
                chunks.append(pt.strip())
    return "\n".join(chunks).strip()


def _build_gemini_config(system_instruction: str) -> Any:
    """google.genai.types.GenerateContentConfig yoki mock-friendly dict yaratadi."""
    decls = ai_tools.get_gemini_tool_declarations()
    try:
        from google.genai import types
        tool_obj = types.Tool(function_declarations=decls)
        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[tool_obj],
            temperature=0.3,
        )
    except Exception:
        return {
            "system_instruction": system_instruction,
            "tools": [{"function_declarations": decls}],
            "temperature": 0.3,
        }


def _make_audio_part(audio_bytes: bytes, audio_mime: str) -> Any:
    try:
        from google.genai import types
        return types.Part.from_bytes(data=audio_bytes, mime_type=audio_mime)
    except Exception:
        return {"mime_type": audio_mime, "data": audio_bytes}


def _build_contents(history: list[dict], user_text: str | None, audio_bytes: bytes | None, audio_mime: str) -> list[dict]:
    contents: list[dict] = []
    for h in history:
        contents.append({"role": h["role"], "parts": [{"text": h["content"]}]})
    cur_parts: list[Any] = []
    if audio_bytes:
        cur_parts.append(_make_audio_part(audio_bytes, audio_mime))
        cur_parts.append({
            "text": (
                user_text.strip() if user_text else
                "Foydalanuvchi ovozli xabar yubordi. Uni diqqat bilan tingla, tushun va vazifani bajar. "
                "Agar audio noaniq yoki muhim detal yetishmasa, taxmin qilma — qisqa aniqlashtiruvchi savol ber."
            )
        })
    else:
        cur_parts.append({"text": (user_text or "").strip()})
    contents.append({"role": "user", "parts": cur_parts})
    return contents


def _execute_tool_call(name: str, args: dict) -> tuple[dict, dict | None]:
    """
    Bitta Gemini tool chaqiruvini bajaradi.
    Returns: (tool_result_dict, pending_action_if_proposed)
    """
    if name in ai_tools.READ_ONLY_TOOLS:
        fn = ai_tools.READ_ONLY_TOOLS[name]
        try:
            res = fn(**(args or {}))
        except TypeError:
            res = fn()
        return {"ok": True, "data": res}, None

    if name == "propose_actions":
        summary_text = str((args or {}).get("summary_text") or "")
        actions = (args or {}).get("actions") or []
        res = ai_tools.propose_actions(summary_text, actions)
        if "pending_action_id" in res:
            return {"ok": True, "data": res}, res
        return {"ok": False, "error": res.get("error", "Xatolik")}, None

    return {"ok": False, "error": f"Noma'lum asbob: {name}"}, None


async def process_user_turn(
    user_text: str | None = None,
    audio_bytes: bytes | None = None,
    audio_mime: str = "audio/ogg",
    session_id: str | None = None,
    router_instance: ai_router.SmartModelRouter | None = None,
) -> dict:
    """
    Foydalanuvchi matnli yoki ovozli so'rovini Gemini AI Core orqali qayta ishlaydi.
    HECH QACHON boshqarilmagan exception chiqarmaydi (Full Isolation).
    Returns:
        {
            "ok": bool,
            "reply_text": str,
            "pending_action": dict | None,
            "model_used": str | None,
            "latency_ms": float,
            "session_id": str,
        }
    """
    sid = session_id or get_or_create_session_id()
    clean_text = (user_text or "").strip()
    if not clean_text and not audio_bytes:
        return {
            "ok": False,
            "reply_text": "Matn yoki ovozli xabar yuboring.",
            "pending_action": None,
            "model_used": None,
            "latency_ms": 0.0,
            "session_id": sid,
        }

    router = router_instance or ai_router.SmartModelRouter()
    tier = select_tier(clean_text, has_audio=bool(audio_bytes))
    history = get_recent_history(sid, limit=HISTORY_WINDOW)
    passport = build_system_passport()
    config = _build_gemini_config(passport)
    contents = _build_contents(history, clean_text, audio_bytes, audio_mime)

    used_tools: set[str] = set()
    proposed_action: dict | None = None
    used_model: str | None = None
    total_latency = 0.0
    reply_text = ""

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp, used_model, lat_ms = await router.generate_content(contents=contents, tier=tier, config=config)
            total_latency += lat_ms
            fcalls = _extract_function_calls(resp)
            if not fcalls:
                reply_text = _extract_text(resp)
                break

            tool_results_parts = []
            for fc in fcalls:
                tname = fc["name"]
                targs = fc["args"]
                used_tools.add(tname)
                t_res, pending_info = _execute_tool_call(tname, targs)
                if pending_info:
                    proposed_action = pending_info
                tool_results_parts.append({
                    "function_response": {
                        "name": tname,
                        "response": t_res,
                    }
                })

            # Agar write/mutating amal taklif qilingan bo'lsa, darhol foydalanuvchiga tasdiqlash kartochkasini chiqaramiz
            if proposed_action is not None:
                model_text = _extract_text(resp)
                reply_text = model_text or proposed_action["summary_text"]
                break

            contents.append({"role": "model", "parts": [{"text": f"[Asboblar chaqirildi: {', '.join(fc['name'] for fc in fcalls)}]"}]})
            contents.append({"role": "user", "parts": [{"text": json.dumps(tool_results_parts, ensure_ascii=False, default=str)}]})

        if not reply_text:
            reply_text = "So'rovingiz qayta ishlandi, lekin aniq javob shakllanmadi. Iltimos, qayta so'rab ko'ring."

        if _needs_medical_disclaimer(clean_text, reply_text, used_tools) and "103" not in reply_text:
            reply_text = reply_text + ai_tools.MEDICAL_DISCLAIMER

        saved_user_content = clean_text if clean_text else "🎤 [Ovozli xabar]"
        save_chat_message(sid, "user", saved_user_content)
        save_chat_message(sid, "model", reply_text)

        return {
            "ok": True,
            "reply_text": reply_text,
            "pending_action": proposed_action,
            "model_used": used_model,
            "latency_ms": round(total_latency, 1),
            "session_id": sid,
        }

    except ai_router.AIMissingKeyError as exc:
        return {
            "ok": False,
            "reply_text": f"⚠️ {exc}",
            "pending_action": None,
            "model_used": None,
            "latency_ms": 0.0,
            "session_id": sid,
        }
    except ai_router.AIUnavailableError as exc:
        return {
            "ok": False,
            "reply_text": f"⚠️ {exc}",
            "pending_action": None,
            "model_used": None,
            "latency_ms": round(total_latency, 1),
            "session_id": sid,
        }
    except Exception as exc:
        db.log_error("AICoreUnexpectedError", str(exc), traceback.format_exc(), "features.ai_core.process_user_turn")
        app.log.exception(f"AI Core kutilmagan xatolik: {exc}")
        return {
            "ok": False,
            "reply_text": (
                "⚠️ AI modulida vaqtinchalik xatolik yuz berdi. "
                "Xatolik `/diagnostika` jurnaliga yozildi, botning qolgan barcha bo'limlari normal ishlamoqda."
            ),
            "pending_action": None,
            "model_used": used_model,
            "latency_ms": round(total_latency, 1),
            "session_id": sid,
        }
