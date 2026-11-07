# AltronBek 3.5

## Yangilash tartibi (3.1 → 3.5, ma'lumotlarni 100% saqlagan holda)
1. Telegramda botga kiring: **⚙️ Sozlamalar → 🗂 Ma'lumotlar → 📤 Zaxira nusxa** ni bosing va `.db` faylni saqlab oling.
2. GitHub repo'da **Add file → Upload files** ni oching.
3. Shu papkadagi **barcha fayllarni** (`bot.py`, `main.py`, `app.py`, `db.py`, `feat_*.py` va boshqalar — jami 31 ta yassi fayl) tanlab yuklang va **Commit changes** ni bosing.
4. Railway avtomatik qayta deploy qiladi (`python bot.py`).
5. Bot ishga tushganda:
   - Avval bazaning yangilanishdan oldingi avtomatik nusxasini (`altronbek.db.pre-v4.bak`) Telegramga yuboradi;
   - **"🟢 AltronBek 3.5 ishga tushdi."** xabarini yuboradi;
   - Ketidan **🔄 Moliyaviy holat tekshiruvi (Start-up sync)** oynasini (`[✅ To'g'ri]`, `[➕ Qo'shish]`, `[✏️ Tahrirlash]`) chiqaradi.
6. `/holat` buyrug'ini yuborib baza yo'li (`/data/altronbek.db`), sxema (`v4`) va barcha yozuvlar joyida ekanini tekshiring.

## 3.5 dagi yangiliklar (0% Hardcode — 100% Dinamik CRUD)
- **Universal Chelenj Dvigateli (`feat_challenges.py`, `scoring.py`, `db.py` sxema `v4`):**
  - `challenges`, `challenge_tasks`, `challenge_logs`, `challenge_bonuses` jadvallari.
  - Bot ichidan 1 vazifalik yoki ko'p vazifalik chelenj yaratish, tahrirlash, vazifa qo'shish va to'xtatish (FSM + inline).
  - 4 xil vazifa turi: `binary` (Ha/Yo'q), `numeric` (sonli maqsad), `weekly_quota` (haftada N kun), `schedule` (`mon,wed,fri`).
  - **Davomiylik qoidasi:** vazifa o'tkazib yuborilsa chelenj noldan boshlanmaydi va bekor bo'lmaydi — 30 kunlik hisob davom etadi, faqat o'sha kun «to'liq emas» deb qayd etiladi.
  - Kunlik barcha vazifalar to'liq yopilganda `scoring.py` orqali bonus ball qo'shiladi; `21:45` kechki xulosa va `/chelenjlar` menyusiga inline boshqaruv paneli ulandi.
- **Universal Moliya Tizimi (`feat_finance.py`, `db.py` sxema `v4`):**
  - `finance_accounts`, `finance_debts`, `finance_loans`, `finance_recurring`, `finance_transactions` jadvallari.
  - **Smart Lifecycle:** kredit to'langanda `remaining_debt` va `remaining_installments` kamayib, `<= 0` bo'lganda `status = 'paid'` bo'ladi va qayta eslatilmaydi; qarzlar `0` ga yetganda `status = 'settled'` bo'lib eslatmadan tushadi.
  - **Davriy to'lovlar (Partial Payment):** masalan `8–15` sanalar oynasida oy davomida qisman to'lab boriladi (`paid_amount_this_month`), `target_amount` ga yetgach shu oy uchun to'xtaydi va yangi oyda avtomatik yangilanadi.
  - **Xavfsiz eslatmalar va filtrlar:** kredit kartalar uchun har oyning `5, 7 va 8`-sanalarida (`8-sana 17:00 gacha`) qat'iy ogohlantirish; «Yaqin to'lovlar» blokida FAQAT `<= 3 kun` qolgan to'lovlar; Minglik qoidasi (`25 → 25 000`, `400..999` sonlarda tasdiq).
  - **Start-up sync** va `06:30` tonggi xulosada Moliya bloki (`Naqd`, `Kartalarda (jami)`, `Haqlar`, `Yaqin to'lovlar ≤3 kun`).
- **Qo'shimcha qulayliklar:**
  - `!` bilan boshlangan xabarlar tezkor qayd sifatida `notes` jadvaliga tushadi va `21:45` kechki xulosada eslatiladi.
  - `feat_cleanup.py` to'liq In-place UX (`edit_message_text`) rejimiga o'tkazildi.
  - `services.py` da `28, 29, 30, 31` kunlik oylar uchun xavfsiz sanalar arifmetikasi (`add_months`, `clamp_day`, `next_monthly_due_date`).
