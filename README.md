# AltronBek 3.0.1

## Yangilash tartibi (3.0 → 3.0.1, ma'lumotlarni saqlagan holda)
1. Telegramda botga kiring: **⚙️ Sozlamalar → 🗂 Ma'lumotlar → 📤 Zaxira nusxa** ni bosing va `.db` faylni saqlab oling.
2. GitHub repo'da **Add file → Upload files** ni oching.
3. Shu papkadagi **barcha fayllarni** (`bot.py`, `main.py`, `app.py`, `db.py`, `feat_*.py` va boshqalar — hammasi yassi, papkasiz!) tanlab yuklang va **Commit changes** ni bosing.
   *(Repo'da eski `features/` papkasi qolsa ham zarari yo'q, yangi `main.py` faqat `feat_*.py` modullarini o'qiydi).*
4. Railway avtomatik qayta deploy qiladi (`python bot.py`).
5. Bot ishga tushganda:
   - Avval bazaning yangilanishdan oldingi avtomatik nusxasini (`altronbek.db.pre-v2.bak`) yuboradi;
   - Keyin **"🟢 AltronBek 3.0.1 ishga tushdi."** xabarini yuboradi.
6. `/holat` buyrug'ini yuborib baza yo'li (`/data/altronbek.db`), sxema (`v2`) va yozuvlar soni (tug'ilgan kunlar va h.k.) joyida ekanini tekshiring.

## 3.0.1 dagi yangiliklar va tuzatishlar
- **Yassi tuzilma (`feat_*.py`):** GitHub'ga papkasiz bitta urinishda yuklash.
- **Laglar bartaraf etildi:** SQLite `WAL` rejimi + doimiy ulanish, tezkor `cb.answer()`, yengillashtirilgan qazo tekshiruvi.
- **Kunlik jurnal (`feat_journal.py`):** javob berilgan eslatma xabarlari o'chib, kun bo'yi bitta xabar yangilanib boradi.
- **Zaxiradan tiklash (`/tiklash`, `feat_restore.py`):** `.db` fayldan xavfsiz tekshiruv va avto-nusxa bilan tiklash.
- **Foydalanuvchi xabarini ham o'chirish:** bot javobi o'chganda siz yozgan buyruq ham tozalanadi (Sozlamalarda o'chirib-yoqish mumkin).
- **Uyqu:** Bomdodda uyg'onish qayta so'ralmaydi; `/uyqu_kiritish` orqali tungi uyquni qo'lda kiritish qo'shildi.
- **Tuzatishlar:** eslatma xabarida izoh (`note`) chiqadi; ko'p yillik qazo kodlari to'qnashmaydi; `/shahar` va `/sinf` bugungi jadvalni darhol yangilaydi; ixtiyoriy eslatmada "Boraman" bosilgach vaqtida eslatadi; `/izohlar` ro'yxati hamda kengaytirilgan CSV/matn eksporti qo'shildi.
