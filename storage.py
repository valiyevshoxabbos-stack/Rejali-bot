"""
Oddiy JSON-fayl asosidagi saqlash: har kunning bajarilgan/bajarilmagan
vazifalarini yozib boradi.
"""

import json
import os
from datetime import date, timedelta
from threading import Lock


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.lock = Lock()
        if not os.path.exists(self.path):
            self._save({})

    def _load(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def mark(self, day: str, item_id: str, done: bool):
        with self.lock:
            data = self._load()
            data.setdefault(day, {})
            data[day][item_id] = done
            self._save(data)

    def get_last_days(self, n: int) -> dict:
        with self.lock:
            data = self._load()
        result = {}
        today = date.today()
        for i in range(n):
            d = (today - timedelta(days=i)).isoformat()
            if d in data:
                items = data[d]
                done = sum(1 for v in items.values() if v)
                total = len(items)
                result[d] = (done, total)
        return dict(sorted(result.items(), reverse=True))
