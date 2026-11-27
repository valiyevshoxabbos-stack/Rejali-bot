"""
AltronBek 4.0.1: AI Suhbat moduli uchun izolyatsiyalangan ulanish nuqtasi.
`features/ai_chat.py` dan router va kirish nuqtalarini eksport qiladi.
"""

from features.ai_chat import (  # noqa: F401
    AIChatState,
    entry,
    register_jobs,
    render_ai_settings,
    router,
)
