# Reja eslatuvchi bot

Umarkhanning kunlik rejasi va namoz vaqtlarini eslatib turadigan, "bajardim/bajarmadim"
tugmasi bilan statistika yuritadigan Telegram bot.

## 1-qadam: chat_id topish

1. Telegram'da `@userinfobot` ni oching va `/start` bosing — u sizga chat_id'ingizni beradi.
2. Yoki botingizga bitta xabar yozing, so'ng brauzerda oching:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   va javobdagi `"chat":{"id": ...}` qatoridagi raqamni oling.

## 2-qadam: sozlash

```bash
cp .env.example .env
```

`.env` faylni oching va:
- `BOT_TOKEN` — BotFather'dan olgan tokeningiz
- `CHAT_ID` — yuqorida topgan raqam
- `CITY` — hozircha `Chirchiq` (xohlasangiz keyin `/shahar` buyrug'i bilan o'zgartirasiz)

## 3-qadam: ishga tushirish

### A) Oddiy serverda (VPS, Render, Railway va h.k.)

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 bot.py
```

Doim ishlab tursin desangiz, `systemd` service yoki `screen`/`tmux` ichida
ishga tushiring, yoki Railway/Render kabi xizmatlarga joylang (ular bepul
tarifda ham doimiy ishlaydi).

### B) Termux'da (telefonda)

```bash
pkg install python
pip install -r requirements.txt
python bot.py
```

Termux'da fon rejimida ishlashi uchun:
- Android sozlamalarida Termux uchun "Battery optimization"ni o'chiring
- `termux-wake-lock` buyrug'ini ishga tushiring (ekran o'chsa ham jarayon to'xtamasin uchun)

## Buyruqlar

- `/start` — botni tanishtirish
- `/bugun` — bugungi rejani ko'rish
- `/namoz` — bugungi namoz vaqtlari
- `/statistika` — oxirgi 7 kunlik bajarish foizi
- `/shahar <nom>` — namoz vaqtlari hisoblanadigan shaharni o'zgartirish
- `/qoshish` — yangi eslatma qo'shish (bosqichma-bosqich so'raydi):
  - **Har kuni** — masalan har kuni soat 22:00 da
  - **Haftalik** — masalan har dushanba soat 18:00 da
  - **Aniq sana** — masalan faqat 2026-10-05 kuni soat 09:00 da (bir marta ishlaydi, keyin avtomatik o'chadi)
- `/royxat` — qo'shilgan barcha maxsus eslatmalar ro'yxati (ID raqamlari bilan)
- `/ochirish <ID>` — masalan `/ochirish 3` — o'sha eslatmani butunlay o'chiradi
- `/bekor` — `/qoshish` jarayonini bekor qilish

Maxsus eslatmalar `custom_reminders.json` faylida saqlanadi va `schedule_data.py`
dagi asosiy haftalik reja bilan bir qatorda, mustaqil ishlaydi — birini
o'zgartirish ikkinchisiga ta'sir qilmaydi.

## Rejani o'zgartirish

`schedule_data.py` faylini oching, kerakli kun/vaqt/nomni tahrirlang, botni
qayta ishga tushiring (`Ctrl+C` bilan to'xtatib, qayta `python3 bot.py`).

## Eslatma

- Bot har kuni soat 00:05 da o'sha kun uchun barcha eslatmalarni tuzib qo'yadi.
- Bot ishga tushganda ham darhol shu kungi qolgan eslatmalarni tuzadi (masalan
  kompyuter/server qayta yoqilgan bo'lsa).
- Statistika `data.json` faylida saqlanadi — uni o'chirmang, aks holda tarix yo'qoladi.
