# AltronBek 3.1

## Yangilash tartibi (3.0.1 → 3.1, ma'lumotlarni saqlagan holda)
1. Telegramda botga kiring: **⚙️ Sozlamalar → 🗂 Ma'lumotlar → 📤 Zaxira nusxa** ni bosing va `.db` faylni saqlab oling.
2. GitHub repo'da **Add file → Upload files** ni oching.
3. Shu papkadagi **barcha fayllarni** (`bot.py`, `main.py`, `app.py`, `db.py`, `feat_*.py` va boshqalar — jami 29 ta yassi fayl) tanlab yuklang va **Commit changes** ni bosing.
4. Railway avtomatik qayta deploy qiladi (`python bot.py`).
5. Bot ishga tushganda:
   - Avval bazaning yangilanishdan oldingi avtomatik nusxasini (`altronbek.db.pre-v3.bak`) Telegramga yuboradi;
   - Keyin **"🟢 AltronBek 3.1 ishga tushdi."** xabarini yuboradi.
6. `/holat` buyrug'ini yuborib baza yo'li (`/data/altronbek.db`), sxema (`v3`) va barcha yozuvlar joyida ekanini tekshiring.

## 3.1 dagi yangiliklar
- **Tungi uyqu oqimi soddalashtirildi (`feat_sleep.py`, `feat_reminders.py`, `jobs.py`):**
  - 22:00 dan avval `/uyqu`, `/yotdim` yoki menyudan **«😴 Yotdim / Uyqu»** chaqirilsa, tungi uyqu xabari darhol faollashadi va 22:00 dagi APScheduler eslatmasi bekor qilinadi.
  - **«😴 Uxladim / Yotdim»** bosilganda yangi xabar yuborilmasdan o'sha xabarning o'zi tahrirlanib **«☀️ Uyg'ondim»** tugmasiga aylanadi.
  - Bomdodda alohida «Uyg'ondim» xabari yuborilmaydi — kechagi bitta xabardagi **«☀️ Uyg'ondim»** tugmasi bilan sikl yopiladi.
- **Ob-havo moduli va kiyinish tavsiyasi (`feat_weather.py`, `services.py`, `feat_summary.py`):**
  - Open-Meteo API (`elevation=582` Chirchiq uchun, CC BY 4.0 `"Open-Meteo.com"` yozuvi, 75 daqiqalik kesh, `timeout=5s`, `asyncio.to_thread`).
  - `apparent_temperature` bo'yicha 5 pog'onali kiyim tavsiyasi, shamol `>= 15 km/soat` qo'lqop ogohlantirishi, `07:00–21:30` oynasida yog'in `>= 25%` soyabon eslatmasi, harorat farqi `>= 8°C` bo'lganda tonggi/kunduzgi kiyim ajratilishi.
  - `06:30` va `21:45` kunlik xulosalarga hamda **«🌦 Ob-havo»** menyusiga ulandi.
- **Xarajatlar kalkulyatori (`feat_calculator.py`, `db.py` sxema `v3`, `feat_stats.py`):**
  - `sessions`, `expenses`, `expense_splits` jadvallari (`PRAGMA user_version = 3`).
  - **🛒 Bozorlik:** `Kartoshka 5kg 25 Men` va kg/litr/dona narxlar dinamikasi (+ Statistika → Bozorlik).
  - **🍽 Restoran / Choyxona:** umumiy + shaxsiy taomlar (`@Ism` yoki tugma orqali) + `% servis` mutanosib taqsimoti.
  - **🚗 Safar / Umumiy fond:** ko'p kishilik xarajatlarni umumlashtirish.
  - **⚡️ Tezkor bo'lish:** `450000 / 4` (menyudan ham, oddiy chatda ham ishlaydi).
  - **Minglik qoidasi** (`25 → 25 000`, `400..999` chegara sonlarda tasdiq so'rash) va **100 so'mgacha yaxlitlash** (qoldiq eng ko'p to'laganga, minimal tranzaksiyali qarz yopish).
