"""
Foydalanuvchi bot orqali qo'shgan maxsus eslatmalarni saqlaydi.
Uch turi bor:
  - daily: har kuni takrorlanadi (vaqt)
  - weekly: haftaning bitta kunida takrorlanadi (kun raqami + vaqt)
  - once: faqat bitta aniq sanada bir marta ishlaydi

Fayl: custom_reminders.json
Format:
{
  "next_id": 5,
  "items": [
    {"id": 1, "type": "daily", "time": "22:00", "text": "Dori ichish"},
    {"id": 2, "type": "weekly", "weekday": 0, "time": "18:00", "text": "Kitob qaytarish"},
    {"id": 3, "type": "once", "date": "2026-10-05", "time": "09:00", "text": "Shifokorga borish"}
  ]
}
"""

import json
import os
from threading import Lock


class CustomReminders:
    def __init__(self, path: str):
        self.path = path
        self.lock = Lock()
        if not os.path.exists(self.path):
            self._save({"next_id": 1, "items": []})

    def _load(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_daily(self, time_str: str, text: str) -> int:
        with self.lock:
            data = self._load()
            rid = data["next_id"]
            data["items"].append({"id": rid, "type": "daily", "time": time_str, "text": text})
            data["next_id"] += 1
            self._save(data)
            return rid

    def add_weekly(self, weekday: int, time_str: str, text: str) -> int:
        with self.lock:
            data = self._load()
            rid = data["next_id"]
            data["items"].append(
                {"id": rid, "type": "weekly", "weekday": weekday, "time": time_str, "text": text}
            )
            data["next_id"] += 1
            self._save(data)
            return rid

    def add_once(self, date_str: str, time_str: str, text: str) -> int:
        with self.lock:
            data = self._load()
            rid = data["next_id"]
            data["items"].append(
                {"id": rid, "type": "once", "date": date_str, "time": time_str, "text": text}
            )
            data["next_id"] += 1
            self._save(data)
            return rid

    def get_all(self) -> list:
        with self.lock:
            return self._load()["items"]

    def get_for_daily_and_weekly(self, weekday: int) -> list:
        """Bugun ishga tushishi kerak bo'lgan doimiy (daily/weekly) eslatmalar."""
        items = self.get_all()
        result = []
        for item in items:
            if item["type"] == "daily":
                result.append(item)
            elif item["type"] == "weekly" and item["weekday"] == weekday:
                result.append(item)
        return result

    def get_for_date(self, date_str: str) -> list:
        """Bugungi sanaga to'g'ri keladigan bir martalik (once) eslatmalar."""
        items = self.get_all()
        return [item for item in items if item["type"] == "once" and item["date"] == date_str]

    def remove(self, reminder_id: int) -> bool:
        with self.lock:
            data = self._load()
            before = len(data["items"])
            data["items"] = [i for i in data["items"] if i["id"] != reminder_id]
            self._save(data)
            return len(data["items"]) < before

    def remove_once_after_fire(self, reminder_id: int):
        """once turidagi eslatma ishlagandan keyin ro'yxatdan olib tashlanadi."""
        self.remove(reminder_id)
