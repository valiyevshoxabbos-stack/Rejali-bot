"""
AltronBek 4.0.1: AI Suhbat moduli uchun izolyatsiyalangan ulanish nuqtasi.
Yassi tuzilmada to'g'ridan-to'g'ri `feat_ai_chat` dan eksport qiladi (`features/` papkasisiz ham ishlaydi).
"""

from feat_ai_chat import (  # noqa: F401
    AIChatState,
    entry,
    register_jobs,
    render_ai_settings,
    router,
)
