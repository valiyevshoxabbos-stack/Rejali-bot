# RejaliBot 2.0

## Fayllar

- `bot.py` — asosiy bot (aiogram 3, barcha buyruqlar va tugmalar)
- `db.py` — SQLite sxema va ulanish
- `services.py` — biznes mantiq (kunlik jadval, eslatma holatlari, maktab/universitet)
- `scoring.py` — ball hisoblash va haftalik/oylik statistika
- `qazo.py` — qazo aniqlash va kodlar
- `schedule_data.py` — standart haftalik reja, maktab va universitet jadvali

## O'rnatish (1.0 dagi kabi)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # to'ldiring: BOT_TOKEN, CHAT_ID, va h.k.
python3 bot.py
```

## MUHIM: Railway Volume

1.0 dagi kabi oddiy fayl saqlash endi yetarli emas — SQLite fayli ham xuddi
shu muammoga duch keladi: Railway qayta deploy qilganda o'chib ketishi mumkin.

**Railway'da albatta Volume ulang:**
1. Loyiha sahifasida "+ New" → "Volume"
2. Mount path: `/data`
3. `.env` dagi `DB_PATH` ni `/data/rejabot.db` qilib qoldiring (allaqachon shunday)

Volume ulanmasa, bot ishlayveradi, lekin har deploy'da barcha ball, qazo va
statistika tarixi yo'qoladi.

## Yangi buyruqlar (1.0 ga nisbatan)

- `/qoshish` — endi Title, Note, chek-pointlar, ustuvorlik va ball so'raydi
- `/vaqt <nom> <HH:MM> [doimiy]` — standart eslatma vaqtini o'zgartirish
- `/sinf <kun> <sinf1> <sinf2> ...` — maktab sinflar tartibini o'zgartirish
- `/ball <nom> <son>` — standart eslatma ballini o'zgartirish
- `/ballqoshish <ID> <son>` — maxsus eslatma ballini o'zgartirish
- `/qazolar` — ochiq qazolar ro'yxati
- `/bomdod_21_09` kabi kodlar — qazoni chiqarish
- `/izoh <matn>` — kunlik izoh

## Ball tizimi qisqacha

- Ball yig'iladi, kamaymaydi. Haftalik maqsad yo'q — faqat haftalar solishtiriladi.
- Har bir eslatmaning ball qiymati **qo'lda** kiritiladi (standart eslatmalarda
  `schedule_data.py` da boshlang'ich qiymat bor, `/ball` bilan o'zgartiriladi).
- Kech bo'lsa ham "Bajardim" bosilsa, to'liq ball beriladi.
- Kechiktirilib (boshqa kunga surilib) keyin bajarilsa — yarim ball.
- Bajarilmasa — ball yo'q. Qazo va chek-pointlar ball bermaydi/olmaydi.

## Qazo qoidasi

Har bir namoz uchun qazo muddati — **keyingi namoz vaqti kirgandan 15 daqiqa
o'tgach** (bomdod uchun ham, ya'ni peshin kirib 15 daqiqadan keyin). Xufton
uchun keyingi — ertangi bomdod.

## Test qilingan narsalar

Botni ishga tushirishdan oldin quyidagilar avtomatik sinovdan o'tkazilgan:
owner-only middleware, global error handler, standart+maxsus eslatmalar
ro'yxati, bir kunlik va doimiy vaqt o'zgartirish, chek-pointlar, kechiktirish
(tez tugmalar va qo'lda sana), qazo aniqlash va chiqarish, ball hisoblash
(oddiy, kechiktirilgan, qo'lda o'zgartirilgan), maktab/universitet eslatmalari.

## Hali qilinmagan narsalar (keyingi safar)

- Iqtiboslar (kayfiyat uchun) — manba tayyor bo'lgach qo'shiladi
- 1.0 dan 2.0 ga o'tish (eski `custom_reminders.json` ma'lumotlarini
  ko'chirish) — agar kerak bo'lsa alohida so'rang
