# AltronBek 3.6

## Yangilash tartibi (3.5.1 → 3.6, ma'lumotlarni 100% saqlagan holda)
1. Telegramda botga kiring: **⚙️ Sozlamalar → 🗂 Ma'lumotlar → 📤 Zaxira nusxa** ni bosing va `.db` faylni saqlab oling.
2. GitHub repo'da **Add file → Upload files** ni oching.
3. Shu papkadagi **barcha fayllarni** (`bot.py`, `main.py`, `app.py`, `db.py`, `feat_*.py` va boshqalar — jami 32 ta yassi fayl) tanlab yuklang va **Commit changes** ni bosing.
4. Railway avtomatik qayta deploy qiladi (`python bot.py`).
5. Bot ishga tushganda:
   - Avval bazaning yangilanishdan oldingi avtomatik nusxasini (`altronbek.db.pre-v6.bak`) Telegramga yuboradi;
   - **🚀 Startup Changelog** kartochkasini (`AltronBek v3.6 yangilanishi muvaffaqiyatli o'rnatildi!`) yuboradi;
   - **"🟢 AltronBek 3.6 ishga tushdi."** va **🔄 Moliyaviy holat tekshiruvi (Start-up sync)** xabarini chiqaradi.
6. `/holat` buyrug'ini yuborib baza yo'li (`/data/altronbek.db`), sxema (`v6`) va barcha yozuvlar joyida ekanini tekshiring.

## 3.6 dagi yangiliklar (0% Hardcode — Dinamik Smart Bozorlik)
- **🚀 Startup Changelog (`main.py`, `db.py` sxema `v6`):**
  - `PRAGMA user_version` yoki joriy release (`app.VERSION`) yangilanganda ishga tushish vaqtida yangilanishlar kartochkasini bir marta avtomatik yuboradi.
- **🛒 Smart Bozorlik va Ikki Xil Rejim (`feat_market.py`):**
  - **📝 Rejalashtirilgan ro'yxat (`planned`)**: uyda narxsiz mahsulotlar ro'yxatini tuzish (balansga daxl qilmaydi) va bozorga borganda bitta tugma bilan **⚡️ Ayni damdagi xarid** rejimiga o'tkazish.
  - **⚡️ Ayni damdagi xarid (`active`)**: narxlar va `#joy` teglari bilan xarid, `[✅]` belgilash, `[➕ Yangi narsa qo'shish]` va `[➖ O'chirish]` In-place boshqaruvi (`edit_message_text`).
- **🧠 Smart Parser va 🛡 Anti-Cheat (+35%):**
  - `Mahsulot Miqdor [kg|dona|litr|metr] [Narx] [#joy]` formati, Minglik qoidasi (`< 1000 -> * 1000`), `unit_price = total_price / quantity`, `#teg` bo'yicha `market_locations` ni dinamik yaratish.
  - Narx oldingi xariddan **+35% va undan ko'p** sakraganda `[✅ Narx to'g'ri]` yoki `[✏️ Qayta kiritish]` tasdig'i.
- **💰 Moliya integratsiyasi va 📈 Inflyatsiya monitoringi:**
  - **«🏁 Xaridni yakunlash»** bosilganda faqat olingan (`is_bought = 1`) mahsulotlar summasi tanlangan hisobdan yechilib, `finance_transactions` ga yagona yozuv (`Oziq-ovqat / Bozorlik`, `Bozorlik (#joy): ...`) bo'lib tushadi va `market_logs` da har mahsulotning `price_diff_percent` dinamikasi saqlanadi.
- **📍 Savdo joylari tahlili (`/narx <mahsulot>`):**
  - Mahsulotning bazadagi barcha `#teg` savdo joylari bo'yicha narxlarini solishtirib, eng arzon joyni ajratib ko'rsatadi.
