"""
AltronBek 4.0.1: Smart Model Router (Auto-Fallback, Cooldown, Latency Learning, Paid/Fixed rejim).

To'liq izolyatsiya qilingan:
- GEMINI_API_KEY bo'lmasa, 429/503/404/timeout bo'lsa bot qulamaydi.
- Barcha xatolar `system_error_logs` va `ai_model_stats` jadvallariga yoziladi.
- Testlarda mock Gemini client osongina ulanadi (real tarmoq va sleep ishlatilmaydi).
"""

import asyncio
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable

import app
import db

LIGHT_MODELS: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

HEAVY_MODELS: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
]

ALL_MODELS: list[str] = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
]

COOLDOWN_TRANSIENT_SEC = 90        # 429 / 503 / timeout -> 90 soniya
COOLDOWN_NOT_FOUND_HOURS = 24      # 404 -> 24 soat
DEFAULT_TIMEOUT_SEC = 25.0


class AIError(Exception):
    """Barcha AI qatlami xatolari uchun baza klass."""


class AIMissingKeyError(AIError):
    """GEMINI_API_KEY o'rnatilmagan."""


class AIUnavailableError(AIError):
    """Barcha modellar band/limitda yoki tarmoq ishlamayapti."""


_custom_client_factory: Callable[[str], Any] | None = None


def set_client_factory(factory: Callable[[str], Any] | None):
    """Testlar uchun mock Gemini client factory o'rnatish."""
    global _custom_client_factory
    _custom_client_factory = factory


def reset_client_factory():
    global _custom_client_factory
    _custom_client_factory = None


def _build_client(api_key: str) -> Any:
    if _custom_client_factory is not None:
        return _custom_client_factory(api_key)
    try:
        from google import genai
    except ImportError as exc:
        raise AIUnavailableError("`google-genai` kutubxonasi o'rnatilmagan.") from exc
    return genai.Client(api_key=api_key)


def classify_error(exc: Exception) -> tuple[str, timedelta]:
    """
    Gemini xatosini tasniflaydi va cooldown muddatini qaytaradi.
    Returns: (code_label, cooldown_delta)
    """
    msg = str(exc).lower()
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 404 or "404" in msg or "not found" in msg or "does not exist" in msg:
        return "404", timedelta(hours=COOLDOWN_NOT_FOUND_HOURS)
    if code == 429 or "429" in msg or "resource_exhausted" in msg or "quota" in msg or "rate limit" in msg:
        return "429", timedelta(seconds=COOLDOWN_TRANSIENT_SEC)
    if code == 503 or "503" in msg or "unavailable" in msg or "overloaded" in msg:
        return "503", timedelta(seconds=COOLDOWN_TRANSIENT_SEC)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in msg or "timed out" in msg:
        return "timeout", timedelta(seconds=COOLDOWN_TRANSIENT_SEC)
    return "error", timedelta(seconds=COOLDOWN_TRANSIENT_SEC)


def is_api_key_configured() -> bool:
    return bool(app.get_gemini_api_key())


def get_routing_config() -> dict:
    return {
        "routing_mode": db.get_ai_setting("routing_mode", "auto") or "auto",
        "preferred_light_model": db.get_ai_setting("preferred_light_model", LIGHT_MODELS[0]) or LIGHT_MODELS[0],
        "preferred_heavy_model": db.get_ai_setting("preferred_heavy_model", HEAVY_MODELS[0]) or HEAVY_MODELS[0],
    }


def set_routing_mode(mode: str):
    m = "fixed" if mode == "fixed" else "auto"
    db.set_ai_setting("routing_mode", m)


def set_preferred_model(tier: str, model_name: str):
    key = "preferred_heavy_model" if tier == "heavy" else "preferred_light_model"
    db.set_ai_setting(key, model_name.strip())


def _ensure_stat_row(model_name: str, tier: str):
    db.ex(
        "INSERT OR IGNORE INTO ai_model_stats (model_name, tier, success_count, error_count, avg_latency_ms) "
        "VALUES (?, ?, 0, 0, 0)",
        (model_name, tier),
    )


def record_success(model_name: str, tier: str, latency_ms: float):
    _ensure_stat_row(model_name, tier)
    row = db.q1("SELECT success_count, avg_latency_ms FROM ai_model_stats WHERE model_name = ?", (model_name,))
    prev_cnt = int(row["success_count"] or 0) if row else 0
    prev_avg = float(row["avg_latency_ms"] or 0.0) if row else 0.0
    new_cnt = prev_cnt + 1
    new_avg = ((prev_avg * prev_cnt) + float(latency_ms)) / new_cnt
    db.ex(
        "UPDATE ai_model_stats SET tier = ?, success_count = ?, avg_latency_ms = ?, "
        "cooldown_until = NULL, last_used_at = ? WHERE model_name = ?",
        (tier, new_cnt, round(new_avg, 1), app.now().isoformat(), model_name),
    )


def record_error(model_name: str, tier: str, error_text: str, cooldown_delta: timedelta):
    _ensure_stat_row(model_name, tier)
    until_iso = (app.now() + cooldown_delta).isoformat()
    db.ex(
        "UPDATE ai_model_stats SET tier = ?, error_count = error_count + 1, "
        "cooldown_until = ?, last_error = ?, last_used_at = ? WHERE model_name = ?",
        (tier, until_iso, str(error_text)[:300], app.now().isoformat(), model_name),
    )


def get_model_stat(model_name: str) -> dict | None:
    return db.q1("SELECT * FROM ai_model_stats WHERE model_name = ?", (model_name,))


def get_all_model_stats() -> list[dict]:
    try:
        return db.q("SELECT * FROM ai_model_stats ORDER BY success_count DESC, avg_latency_ms ASC, model_name ASC")
    except Exception:
        return []


def clear_all_cooldowns():
    try:
        db.ex("UPDATE ai_model_stats SET cooldown_until = NULL")
    except Exception:
        pass


def _is_in_cooldown(stat: dict | None, now_dt: datetime) -> bool:
    if not stat or not stat.get("cooldown_until"):
        return False
    try:
        until_dt = datetime.fromisoformat(str(stat["cooldown_until"]))
        if until_dt.tzinfo is None:
            until_dt = until_dt.replace(tzinfo=app.TZ)
        return until_dt > now_dt
    except Exception:
        return False


def get_candidate_models(tier: str = "light") -> list[str]:
    """
    Berilgan tier (`light` yoki `heavy`) uchun foydalanish mumkin bo'lgan modellar ro'yxatini
    ustuvorlik tartibida qaytaradi (cooldown'da turganlar chiqarib tashlanadi).
    """
    t = "heavy" if tier == "heavy" else "light"
    base_chain = list(HEAVY_MODELS if t == "heavy" else LIGHT_MODELS)
    cfg = get_routing_config()
    preferred = cfg["preferred_heavy_model"] if t == "heavy" else cfg["preferred_light_model"]
    if preferred and preferred not in base_chain:
        base_chain.insert(0, preferred)

    now_dt = app.now()
    stats_map = {r["model_name"]: r for r in get_all_model_stats()}

    available = [m for m in base_chain if not _is_in_cooldown(stats_map.get(m), now_dt)]
    if not available:
        return []

    if cfg["routing_mode"] == "fixed":
        ordered = []
        if preferred in available:
            ordered.append(preferred)
        ordered.extend(m for m in available if m != preferred)
        return ordered

    # Auto rejim: eng yuqori muvaffaqiyat va eng past kechikishga (latency) ega modelni 1-o'ringa qo'yadi
    def _sort_key(model_name: str):
        st = stats_map.get(model_name)
        idx = base_chain.index(model_name)
        if not st or (int(st["success_count"] or 0) == 0 and int(st["error_count"] or 0) == 0):
            # Hali sinab ko'rilmagan modellar o'z standart ketma-ketligini saqlaydi
            return (1, 0.0, 999999.0, idx)
        succ = int(st["success_count"] or 0)
        err = int(st["error_count"] or 0)
        total = succ + err
        rate = succ / total if total > 0 else 0.0
        lat = float(st["avg_latency_ms"] or 0.0) if succ > 0 else 999999.0
        if succ > 0:
            # Muvaffaqiyatli modellar eng yuqori rate va eng past latency bo'yicha birinchi o'ringa chiqadi
            return (0, -round(rate, 3), lat, idx)
        # Faqat xato bergan modellar eng oxirga tushadi
        return (2, 0.0, lat, idx)

    return sorted(available, key=_sort_key)


class SmartModelRouter:
    """
    Gemini API ga so'rov yuboruvchi aqlli router:
    - Auto-fallback zanjiri (`light` / `heavy`)
    - Cooldown (429/503/timeout -> 90s, 404 -> 24h)
    - `ai_model_stats` statistikasi va latency learning
    - Xatolar `system_error_logs` ga yoziladi, asosiy bot hech qachon qulamaydi.
    """

    def __init__(self, client: Any | None = None, timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self._injected_client = client
        self.timeout_sec = timeout_sec

    def _get_client(self) -> Any:
        if self._injected_client is not None:
            return self._injected_client
        api_key = app.get_gemini_api_key()
        if not api_key:
            db.log_error("AIMissingKeyError", "GEMINI_API_KEY sozlanmagan", "", "features.ai_router")
            raise AIMissingKeyError(
                "AI moduli uchun <code>GEMINI_API_KEY</code> ulanmagan. "
                "Botning qolgan barcha funksiyalari odatdagidek ishlamoqda."
            )
        return _build_client(api_key)

    async def generate_content(
        self,
        contents: Any,
        tier: str = "light",
        config: Any = None,
    ) -> tuple[Any, str, float]:
        """
        Zanjirdagi mavjud modellarni navbat bilan sinab Gemini'dan javob oladi.
        Returns: (response, used_model_name, latency_ms)
        """
        client = self._get_client()
        candidates = get_candidate_models(tier)
        if not candidates:
            err_msg = f"Barcha {tier} modellar vaqtinchalik cooldown holatida."
            db.log_error("AIUnavailableError", err_msg, "", "features.ai_router.generate_content")
            raise AIUnavailableError(
                "Hozir barcha AI modellar vaqtinchalik dam olish (cooldown) holatida. "
                "1–2 daqiqadan so'ng qayta urinib ko'ring."
            )

        last_exc: Exception | None = None
        last_code = "error"

        for model_name in candidates:
            t0 = time.perf_counter()
            try:
                kwargs: dict[str, Any] = {"model": model_name, "contents": contents}
                if config is not None:
                    kwargs["config"] = config

                gen_fn = client.models.generate_content
                if asyncio.iscoroutinefunction(gen_fn):
                    resp = await asyncio.wait_for(gen_fn(**kwargs), timeout=self.timeout_sec)
                else:
                    resp = await asyncio.wait_for(
                        asyncio.to_thread(gen_fn, **kwargs),
                        timeout=self.timeout_sec,
                    )
                latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                record_success(model_name, tier, latency_ms)
                return resp, model_name, latency_ms

            except Exception as exc:
                last_exc = exc
                code_lbl, cd_delta = classify_error(exc)
                last_code = code_lbl
                record_error(model_name, tier, f"[{code_lbl}] {exc}", cd_delta)
                db.log_error(
                    f"AIModelError_{code_lbl}",
                    f"Model {model_name} xato berdi: {exc}",
                    traceback.format_exc(),
                    f"features.ai_router:{tier}",
                )
                app.log.warning(f"AI router fallback: {model_name} ({code_lbl}) -> keyingi modelga o'tilmoqda")
                continue

        final_msg = f"Barcha AI modellar javob bermadi (oxirgi xato: {last_code}: {last_exc})"
        db.log_error("AIAllModelsFailed", final_msg, "", f"features.ai_router:{tier}")
        raise AIUnavailableError(
            "AI xizmati hozir band yoki kunlik limitga yetdi. "
            "Birozdan so'ng qayta urinib ko'ring — botning qolgan barcha menyulari odatdagidek ishlaydi."
        ) from last_exc


def format_ai_diagnostics_block() -> str:
    """/diagnostika va AI Sozlamalari uchun ixcham AI holati bloki."""
    key_ok = is_api_key_configured()
    cfg = get_routing_config()
    mode_lbl = "🔄 Avto-rejim (Smart Fallback)" if cfg["routing_mode"] == "auto" else "⭐ Tanlangan model (Paid/Fixed)"
    lines = [
        "\n🤖 <b>Gemini AI Core Holati:</b>",
        f"• 🔑 <b>API kalit:</b> {'✅ Ulangan' if key_ok else '❌ Ulanmagan (izolyatsiya rejimi)'}",
        f"• 🧭 <b>Routing rejimi:</b> {mode_lbl}",
        f"• ⚡ <b>Light / 🧠 Heavy:</b> <code>{app.esc(cfg['preferred_light_model'])}</code> / <code>{app.esc(cfg['preferred_heavy_model'])}</code>",
    ]
    stats = get_all_model_stats()
    if stats:
        lines.append("• 📊 <b>Modellar statistikasi:</b>")
        now_dt = app.now()
        for st in stats[:6]:
            cd_tag = " ⏳(cooldown)" if _is_in_cooldown(st, now_dt) else ""
            lines.append(
                f"   - <code>{app.esc(st['model_name'])}</code>: "
                f"✅{st['success_count']} / ❌{st['error_count']} / {int(round(st['avg_latency_ms'] or 0))} ms{cd_tag}"
            )
    else:
        lines.append("• 📊 <b>Modellar statistikasi:</b> hali so'rov yuborilmagan")
    return "\n".join(lines)
