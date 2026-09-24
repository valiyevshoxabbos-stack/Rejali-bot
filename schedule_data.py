"""
Umarkhanning haftalik reja jadvali.
Har bir kun uchun: vazifalar ro'yxati (vaqt, nom, izoh).
Namoz vaqtlari alohida Aladhan API orqali olinadi (bot.py da), shuning
uchun bu yerda faqat namoz bilan bog'liq bo'lmagan ishlar bor.

Vaqtni o'zgartirish kerak bo'lsa — shu faylni tahrirlab, botni qayta ishga
tushirish kifoya.
"""

WEEKLY_PLAN = {
    # 0 = Dushanba, 1 = Seshanba, 2 = Chorshanba, 3 = Payshanba, 4 = Juma, 5 = Shanba, 6 = Yakshanba
    0: {  # Dushanba
        "prayers": True,
        "tasks": [
            {"id": "zal", "time": "09:00", "label": "Zal mashg'uloti", "note": "9:00-11:00"},
            {"id": "dush1", "time": "11:00", "label": "Dush, kiyinish"},
            {"id": "universitet", "time": "13:40", "label": "Universitet darslari boshlanadi"},
            {"id": "basseyn", "time": "19:00", "label": "Basseyn", "note": "19:00-21:00"},
            {"id": "yotish", "time": "22:15", "label": "Yotish vaqti!", "note": "Ertaga bomdodga turish uchun muhim"},
        ],
    },
    1: {  # Seshanba
        "prayers": True,
        "tasks": [
            {"id": "zal", "time": "09:00", "label": "Zal mashg'uloti", "note": "9:00-11:00"},
            {"id": "dush1", "time": "11:00", "label": "Dush, kiyinish"},
            {"id": "universitet", "time": "13:40", "label": "Universitet darslari boshlanadi"},
            {"id": "basseyn", "time": "19:00", "label": "Basseyn", "note": "19:00-21:00"},
            {"id": "yotish", "time": "22:15", "label": "Yotish vaqti!"},
        ],
    },
    2: {  # Chorshanba
        "prayers": True,
        "tasks": [
            {"id": "zal", "time": "09:00", "label": "Zal mashg'uloti", "note": "9:00-11:00"},
            {"id": "dush1", "time": "11:00", "label": "Dush, kiyinish"},
            {"id": "universitet", "time": "13:40", "label": "Universitet darslari boshlanadi"},
            {"id": "basseyn", "time": "19:00", "label": "Basseyn", "note": "19:00-21:00"},
            {"id": "yotish", "time": "22:15", "label": "Yotish vaqti!"},
        ],
    },
    3: {  # Payshanba
        "prayers": True,
        "tasks": [
            {"id": "zal", "time": "09:00", "label": "Zal mashg'uloti", "note": "9:00-11:00"},
            {"id": "dush1", "time": "11:00", "label": "Dush, kiyinish"},
            {"id": "universitet", "time": "13:40", "label": "Universitet darslari boshlanadi"},
            {"id": "basseyn", "time": "19:00", "label": "Basseyn", "note": "19:00-21:00"},
            {"id": "yotish", "time": "22:15", "label": "Yotish vaqti!"},
        ],
    },
    4: {  # Juma
        "prayers": True,
        "tasks": [
            {"id": "ish", "time": "07:00", "label": "Maktabga (ishga)"},
            {"id": "ish_tugash", "time": "14:45", "label": "Ish tugadi", "note": "Endi o'qish/tayyorgarlik vaqti"},
            {"id": "yotish", "time": "21:30", "label": "Yotish vaqti!", "note": "Erta yot, ertaga ham ish bor"},
        ],
    },
    5: {  # Shanba
        "prayers": True,
        "tasks": [
            {"id": "ish", "time": "07:00", "label": "Maktabga (ishga)"},
            {"id": "ish_tugash", "time": "14:45", "label": "Ish tugadi"},
            {"id": "yotish", "time": "22:00", "label": "Yotish vaqti!"},
        ],
    },
    6: {  # Yakshanba
        "prayers": True,
        "tasks": [],
    },
}
