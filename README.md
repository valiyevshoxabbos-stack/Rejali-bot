# AltronBek 3.0

## GitHub'ga yuklash (kompyuterdan)
1. Zip'ni oching.
2. GitHub repo'da **Add file → Upload files**. Hamma fayllarni va **`features`** papkasini
   sudrab tashlang (papka tuzilmasi saqlanadi). Bir xil nomli fayllar (`bot.py`, `db.py`, `services.py`,
   `scoring.py`, `qazo.py`, `schedule_data.py`) yangisi bilan almashadi. **Commit changes**.
3. Railway o'zi qayta deploy qiladi. Start command `python bot.py` bo'lib qolaversa ham bo'ladi
   (`bot.py` endi `main.py` ni ishga tushiradi). Variables o'zgarmaydi.
4. Eski `storage.py` va `custom_reminders.py` (1.0 dan) endi kerak emas, o'chirsangiz bo'ladi.

Birinchi ishga tushishda bot "🟢 AltronBek 3.0 ishga tushdi" xabarini yuboradi. 2.0 dagi eski
xabarlarning tugmalari "oldingi versiyadan qolgan" deb rad etiladi (boshqa yozuvni buzmaslik uchun).
Eski 2.0 bazasi bo'lsa, `.v2.bak` nomi bilan chetga olinadi.

## Volume
Volume ulanmaguncha baza har deploy'da noldan boshlanadi. Ulagach (mount path `/data`)
Variables'ga `DB_PATH=/data/altronbek.db` qo'shing. Zaxira: Sozlamalar → Ma'lumotlar.

## Tuzilma
- `main.py` kirish nuqtasi; `features/` har funksiya alohida modul.
- Ixtiyoriy modullar (`features/weather.py`, `features/calculator.py`) fayl qo'shilsa avtomatik yuklanadi;
  yo'q bo'lsa menyu "keyingi versiyada" deb javob beradi.
- Baza versiyalangan (`PRAGMA user_version`): keyingi versiyalar ma'lumotni saqlab yangilanadi.

## 3.0 da bor
Menyu va sozlamalar, xabarlarni tozalash (🗑, avto-o'chirish, tiklash), eslatma rejimlari
(majburiy/ixtiyoriy/o'chirilgan), pauza, tezkor "➕ Bajardim", uyqu, tug'ilgan kunlar, vazn,
bo'limli statistika, zaxira va CSV eksport, `/holat`.
Keyingi versiyalarda: ob-havo (3.1), kalkulyator (3.2).

## Eslatma
`/vaqt zal 10:00 doimiy` faqat shu kun turi uchun (masalan juma kuni yozsangiz, juma uchun) doimiy o'zgartiradi.
Juma kuni fizika standart 15:30 ga surilgan (zal bilan to'qnashmasligi uchun).
