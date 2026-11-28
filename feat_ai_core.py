"""
AltronBek 4.0.1: Gemini AI Core — Tizim Pasporti (Startup Snapshot),
Multimodal Ovozli Xabarlar, Function Calling Tsikli va Xavfsizlik Qatlami.
"""

import inspect
import json
import re
import traceback
import uuid
from typing import Any, Callable

import app
import db
import feat_schedules as schedules
import feat_ai_router as ai_router
import feat_ai_tools as ai_tools

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
    "salomatlik", "harorat", "bosim", "qon", "tyubik", "flakon", "maz", "krem",
    "gel", "sprey", "tomchi", "kapsula", "ampula", "retsept",
)

RECEIPT_KEYWORDS = (
    "jami", "itogo", "total", "chek", "xarid", "to'lov", "tolov", "so'm", "som",
    "uzs", "summa", "narx", "do'kon", "dokon", "market", "supermarket",
    "korzinka", "havas", "makro", "dorixona", "apteka",
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


def _needs_medical_disclaimer(
    user_text: str,
    reply_text: str,
    used_tools: set[str],
    pending_action: dict | None = None,
) -> bool:
    if "get_health_status" in used_tools:
        return True
    if pending_action and pending_action.get("has_health_action"):
        return True
    combined = f"{user_text or ''} {reply_text or ''}".lower()
    return any(kw in combined for kw in MEDICAL_KEYWORDS)


def extract_ocr_receipt_hint(reply_text: str, user_text: str = "") -> dict | None:
    """
    OCR rasm tahlili natijasidan xarid/chiqim cheki ekanligini, umumiy summani va
    aniqlangan mahsulot qatorlarini xavfsiz ajratib oladi (balansdan HECH QACHON avtomatik yechmaydi!).
    """
    txt = (reply_text or "").strip()
    if not txt:
        return None
    low = f"{txt} {user_text or ''}".lower()
    if not any(kw in low for kw in RECEIPT_KEYWORDS):
        return None

    # 1) Mahsulot qatorlarini ajratish (`- Olma 2 kg — 24 000 so'm`, `1. Non: 8 000 so'm`)
    items: list[dict] = []
    total_amount = 0
    store_name = ""

    for raw_line in txt.splitlines():
        line = re.sub(r"<[^>]+>", "", raw_line).strip()
        if not line:
            continue
        line_low = line.lower()

        if not store_name:
            sm = re.match(r"^(?:do['ʻ’]?kon|dorixona|joy|manzil|market)\s*[:\-—]\s*(.+)$", line, re.IGNORECASE)
            if sm:
                store_name = sm.group(1).strip().strip("*_")
                continue

        # Jami summa qatori
        if any(k in line_low for k in ("jami", "itogo", "total", "umumiy", "to'lov", "tolov")):
            nums = re.findall(r"\d[\d\s.,]*\s*(?:ming|mln)?", line, re.IGNORECASE)
            for cand in reversed(nums):
                try:
                    val = ai_tools._normalize_amount(cand)
                    if val >= 100:
                        total_amount = val
                        break
                except ValueError:
                    continue
            continue

        # Mahsulot qatori
        im = re.match(
            r"^(?:[-•*]|\d+[.)])\s*(?P<name>[^:\-—\n]+?)\s*(?:[:\-—=]+|\s+)\s*(?P<price>\d[\d\s.,]*(?:\s*(?:ming|mln))?)\s*(?:so['ʻ’]?m|som|uzs)\b",
            line,
            re.IGNORECASE,
        )
        if im:
            raw_name = im.group("name").strip().strip("*_")
            try:
                price_v = ai_tools._normalize_amount(im.group("price"))
            except ValueError:
                price_v = 0
            if raw_name and price_v > 0:
                qty = 1.0
                unit = "dona"
                qm = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|dona|litr|l|metr|tyubik|flakon|tabletka|kapsula)\b", raw_name, re.IGNORECASE)
                if qm:
                    qty = float(qm.group(1).replace(",", "."))
                    unit = qm.group(2).lower()
                    raw_name = (raw_name[:qm.start()] + raw_name[qm.end():]).strip(" (),.-")
                if raw_name:
                    items.append({
                        "item_name": raw_name,
                        "quantity": qty if qty > 0 else 1.0,
                        "unit": unit,
                        "total_price": price_v,
                    })

    if total_amount <= 0 and items:
        total_amount = sum(int(it["total_price"]) for it in items)

    # Agar hali ham summa topilmasa, matndagi `so'm` oldidagi eng katta raqamni olamiz
    if total_amount <= 0:
        som_nums = re.findall(r"(\d[\d\s.,]*(?:\s*(?:ming|mln))?)\s*(?:so['ʻ’]?m|som|uzs)\b", txt, re.IGNORECASE)
        parsed = []
        for s_n in som_nums:
            try:
                v = ai_tools._normalize_amount(s_n)
                if v >= 100:
                    parsed.append(v)
            except ValueError:
                pass
        if parsed:
            total_amount = max(parsed)

    if total_amount <= 0 and not items:
        return None

    is_pharmacy = any(
        kw in low for kw in ("dorixona", "apteka", "aptechka", "tabletka", "sirop", "tyubik", "flakon", "dori", "paratsetamol", "ibuprofen")
    )
    return {
        "total_amount": int(total_amount),
        "items": items,
        "is_pharmacy": is_pharmacy,
        "store_name": store_name,
        "summary": txt[:500],
    }


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


def _make_media_part(media_bytes: bytes, media_mime: str) -> Any:
    try:
        from google.genai import types
        return types.Part.from_bytes(data=media_bytes, mime_type=media_mime)
    except Exception:
        return {"mime_type": media_mime, "data": media_bytes}


def _make_audio_part(audio_bytes: bytes, audio_mime: str) -> Any:
    return _make_media_part(audio_bytes, audio_mime)


def _build_contents(
    history: list[dict],
    user_text: str | None,
    audio_bytes: bytes | None,
    audio_mime: str,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> list[dict]:
    contents: list[dict] = []
    for h in history:
        contents.append({"role": h["role"], "parts": [{"text": h["content"]}]})
    cur_parts: list[Any] = []
    if image_bytes:
        cur_parts.append(_make_media_part(image_bytes, image_mime))
        caption_s = (user_text or "").strip()
        ocr_prompt = (
            "Foydalanuvchi rasm/chek yubordi (OCR tahlil). Rasm ichidagi matn, do'kon/dorixona nomi, "
            "mahsulotlar/xizmatlar (miqdori va qator narxi bilan) hamda Jami summani aniq o'qi va o'zbek tilida qisqa, tushunarli ro'yxat qilib chiqar.\n"
            "QAT'IY QOIDA: Balansdan o'zboshimchalik bilan yechma! "
            "Agar izohda aniq hisob (masalan 'Naqd' yoki karta raqami) ko'rsatilgan bo'lsa `propose_actions` chaqir, "
            "aks holda faqat o'qilgan natijani chiqarib, foydalanuvchidan qaysi hisobdan moliya chiqimi yoki bozorlik/dorixonaga yozishni so'ra."
        )
        if caption_s:
            ocr_prompt = f"Foydalanuvchi izohi: {caption_s}\n\n{ocr_prompt}"
        cur_parts.append({"text": ocr_prompt})
    elif audio_bytes:
        cur_parts.append(_make_media_part(audio_bytes, audio_mime))
        cur_parts.append({
            "text": (
                user_text.strip() if user_text else
                "Foydalanuvchi ovozli xabar yubordi. Uni diqqat bilan tingla, tushun va vazifani bajar "
                "(bozorlik, dorixona, moliya, jadval/eslatma yoki oddiy suhbat bo'lishi mumkin). "
                "Agar audio noaniq eshitilsa yoki muhim detal (hisob: Naqd/karta, summa, vaqt) yetishmasa, "
                "taxmin qilma — `propose_actions` chaqirmasdan qisqa aniqlashtiruvchi savol ber."
            )
        })
    else:
        cur_parts.append({"text": (user_text or "").strip()})
    contents.append({"role": "user", "parts": cur_parts})
    return contents


def _execute_tool_call(name: str, args: dict | None, user_text: str | None = None) -> tuple[dict, dict | None]:
    """
    Bitta Gemini tool chaqiruvini xavfsiz bajaradi.
    Returns: (tool_result_dict, pending_action_if_proposed)
    """
    safe_args = args if isinstance(args, dict) else {}
    if name in ai_tools.READ_ONLY_TOOLS:
        fn = ai_tools.READ_ONLY_TOOLS[name]
        try:
            res = fn(**safe_args)
        except TypeError:
            try:
                res = fn()
            except Exception as exc:
                return {"ok": False, "error": f"{name} xatoligi: {exc}"}, None
        except Exception as exc:
            return {"ok": False, "error": f"{name} xatoligi: {exc}"}, None
        return {"ok": True, "data": res}, None

    if name == "propose_actions":
        summary_text = str(safe_args.get("summary_text") or "")
        actions = safe_args.get("actions") or []
        res = ai_tools.propose_actions(summary_text, actions, user_text=user_text)
        if "pending_action_id" in res:
            return {"ok": True, "data": res}, res
        if res.get("needs_clarification"):
            return {
                "ok": False,
                "needs_clarification": True,
                "clarification_question": res.get("clarification_question") or res.get("error", "Aniqlashtirish kerak."),
                "error": res.get("error", "Aniqlashtirish kerak."),
            }, None
        return {"ok": False, "error": res.get("error", "Xatolik")}, None

    return {"ok": False, "error": f"Noma'lum asbob: {name}"}, None


async def process_user_turn(
    user_text: str | None = None,
    audio_bytes: bytes | None = None,
    audio_mime: str = "audio/ogg",
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    session_id: str | None = None,
    router_instance: ai_router.SmartModelRouter | None = None,
    on_progress: Callable[[str], Any] | None = None,
) -> dict:
    """
    Foydalanuvchi matnli, ovozli yoki rasm/chek (OCR) so'rovini Gemini AI Core orqali qayta ishlaydi.
    HECH QACHON boshqarilmagan exception chiqarmaydi (Full Isolation).
    Returns:
        {
            "ok": bool,
            "reply_text": str,
            "pending_action": dict | None,
            "ocr_receipt": dict | None,
            "has_health_context": bool,
            "model_used": str | None,
            "latency_ms": float,
            "session_id": str,
        }
    """
    sid = session_id or get_or_create_session_id()
    clean_text = (user_text or "").strip()
    if not clean_text and not audio_bytes and not image_bytes:
        return {
            "ok": False,
            "reply_text": "Matn, ovozli xabar yoki rasm/chek yuboring.",
            "pending_action": None,
            "ocr_receipt": None,
            "has_health_context": False,
            "model_used": None,
            "latency_ms": 0.0,
            "session_id": sid,
        }

    async def _notify(stage: str):
        if on_progress is None:
            return
        try:
            maybe = on_progress(stage)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            pass

    router = router_instance or ai_router.SmartModelRouter()
    tier = select_tier(clean_text, has_audio=bool(audio_bytes))
    history = get_recent_history(sid, limit=HISTORY_WINDOW)
    passport = build_system_passport()
    config = _build_gemini_config(passport)
    contents = _build_contents(
        history,
        clean_text,
        audio_bytes,
        audio_mime,
        image_bytes=image_bytes,
        image_mime=image_mime,
    )

    used_tools: set[str] = set()
    proposed_action: dict | None = None
    used_model: str | None = None
    total_latency = 0.0
    reply_text = ""

    try:
        for round_idx in range(MAX_TOOL_ROUNDS):
            if round_idx == 0:
                await _notify("ocr" if image_bytes else "thinking")
            else:
                await _notify("finalizing")
            resp, used_model, lat_ms = await router.generate_content(contents=contents, tier=tier, config=config)
            total_latency += lat_ms
            fcalls = _extract_function_calls(resp)
            if not fcalls:
                reply_text = _extract_text(resp)
                break

            tool_results_parts = []
            clarification_text: str | None = None
            for fc in fcalls:
                tname = fc["name"]
                targs = fc["args"]
                used_tools.add(tname)
                await _notify(f"tool:{tname}")
                t_res, pending_info = _execute_tool_call(tname, targs, user_text=clean_text)
                if pending_info:
                    proposed_action = pending_info
                elif t_res.get("needs_clarification") and not clarification_text:
                    clarification_text = str(t_res.get("clarification_question") or t_res.get("error") or "").strip()
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

            # Agar majburiy argument yetishmasa yoki hisob/jadval noaniq bo'lsa — taxmin qilmaymiz, darhol aniqlashtiramiz!
            if clarification_text:
                reply_text = f"❓ {clarification_text}"
                break

            contents.append({"role": "model", "parts": [{"text": f"[Asboblar chaqirildi: {', '.join(fc['name'] for fc in fcalls)}]"}]})
            contents.append({"role": "user", "parts": [{"text": json.dumps(tool_results_parts, ensure_ascii=False, default=str)}]})

        if not reply_text:
            reply_text = "So'rovingiz qayta ishlandi, lekin aniq javob shakllanmadi. Iltimos, qayta so'rab ko'ring."

        has_health = _needs_medical_disclaimer(clean_text, reply_text, used_tools, pending_action=proposed_action)
        if has_health and "103" not in reply_text:
            reply_text = reply_text + ai_tools.MEDICAL_DISCLAIMER

        ocr_receipt = extract_ocr_receipt_hint(reply_text, clean_text) if (image_bytes and proposed_action is None) else None

        if image_bytes:
            saved_user_content = f"🖼 [Rasm / Chek] {clean_text}".strip()
        elif audio_bytes:
            saved_user_content = clean_text if clean_text else "🎤 [Ovozli xabar]"
        else:
            saved_user_content = clean_text
        save_chat_message(sid, "user", saved_user_content)
        save_chat_message(sid, "model", reply_text)

        return {
            "ok": True,
            "reply_text": reply_text,
            "pending_action": proposed_action,
            "ocr_receipt": ocr_receipt,
            "has_health_context": has_health,
            "model_used": used_model,
            "latency_ms": round(total_latency, 1),
            "session_id": sid,
        }

    except ai_router.AIMissingKeyError as exc:
        return {
            "ok": False,
            "reply_text": f"⚠️ {exc}",
            "pending_action": None,
            "ocr_receipt": None,
            "has_health_context": False,
            "model_used": None,
            "latency_ms": 0.0,
            "session_id": sid,
        }
    except ai_router.AIUnavailableError as exc:
        return {
            "ok": False,
            "reply_text": f"⚠️ {exc}",
            "pending_action": None,
            "ocr_receipt": None,
            "has_health_context": False,
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
            "ocr_receipt": None,
            "has_health_context": False,
            "model_used": used_model,
            "latency_ms": round(total_latency, 1),
            "session_id": sid,
        }
