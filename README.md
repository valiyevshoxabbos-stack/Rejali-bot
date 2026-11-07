# AltronBek 3.5.1

## Yangilash tartibi (3.5 → 3.5.1, ma'lumotlarni 100% saqlagan holda)
1. Telegramda botga kiring: **⚙️ Sozlamalar → 🗂 Ma'lumotlar → 📤 Zaxira nusxa** ni bosing va `.db` faylni saqlab oling.
2. GitHub repo'da **Add file → Upload files** ni oching.
3. Shu papkadagi **barcha fayllarni** (`bot.py`, `main.py`, `app.py`, `db.py`, `feat_*.py` va boshqalar — jami 31 ta yassi fayl) tanlab yuklang va **Commit changes** ni bosing.
4. Railway avtomatik qayta deploy qiladi (`python bot.py`).
5. Bot ishga tushganda:
   - Avval bazaning yangilanishdan oldingi avtomatik nusxasini (`altronbek.db.pre-v5.bak`) Telegramga yuboradi;
   - Standart va tizimli moliya kategoriyalarini (`finance_categories`) xavfsiz to'ldiradi;
   - **"🟢 AltronBek 3.5.1 ishga tushdi."** xabarini yuboradi;
   - Ketidan **🔄 Moliyaviy holat tekshiruvi (Start-up sync)** oynasini (`[✅ To'g'ri]`, `[➕ Qo'shish]`, `[✏️ Tahrirlash]`) chiqaradi.
6. `/holat` buyrug'ini yuborib baza yo'li (`/data/altronbek.db`), sxema (`v5`) va barcha yozuvlar joyida ekanini tekshiring.

## 3.5.1 dagi yangiliklar va tuzatishlar
- **Tranzaksiyalar tarixi va sahifalash (`feat_finance.py`):**
  - Moliya menyusida **📜 Tarix** tugmasi: oxirgi tranzaksiyalarni (sana, summa, hisob, kategoriya, izoh) sahifada 8 tadan chiqaradi.
  - **⬅️ Oldingi / Keyingi ➡️** inline sahifalash (pagination) mavjud xabarni joyida tahrirlash (`edit_text`) orqali ishlaydi.
- **Chelenj boshlanish sanasi va «🧊 Muzlatish (Skip Day)» (`feat_challenges.py`, `db.py` sxema `v5`):**
  - Yangi chelenj yaratishda **🟢 Bugundan** yoki **⏳ Ertadan** varianti: «Ertadan» tanlansa `start_date` ertangi kun bo'ladi, bugungi `21:45` kechki xulosada hisoblanmaydi va uzilish/jarima yozilmaydi.
  - Har bir chelenjda ko'pi bilan **2 marta «🧊 Muzlatish (Skip Day)»** imkoniyati (`challenge_freezes`): muzlatilgan kun uzilish (`to'liq emas`) sifatida hisoblanmaydi.
- **Dinamik Kategoriyalar va Tizimli Integratsiya (`feat_finance.py`, `finance_categories`):**
  - Standart chiqim (`Oziq-ovqat/Bozorlik`, `Kafe/Tushlik`, `Yo'l/Transport`, `Ilm/Ta'lim`, `Sport/Sog'liq`, `Kiyim`, `Ehson`, `Boshqa`) va kirim (`Stipendiya`, `Ish haqi (Maktab)`, `Qo'shimcha ish`, `Oila`, `Boshqa`) kategoriyalari hamda `[➕ Yangi kategoriya]` / `[⚙️ Tahrirlash]` boshqaruvi.
  - **🏦 Kredit to'lovi** (chiqim): aktiv kreditlar ro'yxatini chiqaradi, tanlangan kreditning `remaining_debt` va `remaining_installments` qiymatini kamaytiradi va karta balansidan yechadi.
  - **🏠 Ijara/Kvartira** (chiqim): `500 000 so'm` oylik rejadan to'langan qismini chegiradi va hisob balansidan yechadi.
  - **🤝 Qarzni qaytarish** (chiqim): foydalanuvchi qarzdorliklarini (`I owe`) chiqarib, to'langan summani qarzdan va balansdan chegiradi.
  - **🤝 Qarz qaytishi** (kirim): foydalanuvchi haqlarini (`Owe me`) chiqarib, to'langan summani haqdan kamaytiradi va hisob balansiga qo'shadi.
- **1 daqiqalik Bekor qilish (Undo) va 90 kunlik Avto-Arxiv + `VACUUM`:**
  - Kiritilgan har bir tranzaksiya tasdiq xabari tagida **1 daqiqa davomida «↩️ Bekor qilish»** tugmasi turadi: bosilsa tranzaksiya bazadan o'chiriladi, hisob balansi va bog'langan kredit/kvartira/qarz holati to'liq qaytariladi.
  - APScheduler har 90 kunda `90` kundan eski `finance_transactions` va `challenge_logs` yozuvlarini JSON + CSV zaxira fayllari sifatida Telegramga yuborib, jadvaldan tozalaydi va `VACUUM;` orqali bazani fizik ixchamlashtiradi (kartalar va naqd pul balansi daxlsiz saqlanadi).
