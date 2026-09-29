"""
Umarkhanning standart haftalik rejasi (shablon).

Bot birinchi ishga tushganda bularni bazaga yuklaydi. Keyin vaqtlarni
botning o'zida /vaqt buyrug'i bilan o'zgartirish mumkin (bir kunga yoki
doimiy), shuning uchun bu fayl faqat boshlang'ich holat.

Maydonlar:
  slug      - qisqa, bo'sh joysiz, takrorlanmas id (kodlar va statistika uchun)
  start     - boshlanish vaqti HH:MM
  end       - tugash vaqti HH:MM (ixtiyoriy)
  title     - qisqa nom (ro'yxatda ko'rinadi)
  note      - qo'shimcha izoh
  priority  - 1 oddiy, 2 muhim, 3 juda muhim
  scored    - ballga ta'sir qiladimi (True/False)
  checkpoints - kichik tekshiruvlar ro'yxati (ixtiyoriy)

Haftalik kunlar: 0 = Dushanba ... 6 = Yakshanba.
"""

# Har kuni takrorlanadigan
DAILY = [
    {"slug": "yotish", "start": "22:00", "title": "Yotish vaqti",
     "note": "Ertaga bomdodga turish uchun muhim", "priority": 3, "scored": True, "points": 15},
]

# Haftaning aniq kunlari
WEEKLY = {
    0: [  # Dushanba
        {"slug": "fizika", "start": "07:20", "end": "08:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "zal", "start": "09:00", "end": "11:00", "title": "Zal mashg'uloti",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "dush", "start": "11:00", "title": "Dush, kiyinish", "priority": 1, "scored": False},
        {"slug": "universitet", "start": "13:40", "title": "Universitet darslari boshlanadi",
         "priority": 2, "scored": False},
    ],
    1: [  # Seshanba
        {"slug": "fizika", "start": "07:20", "end": "08:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "universitet", "start": "13:40", "title": "Universitet darslari boshlanadi",
         "priority": 2, "scored": False},
        {"slug": "basseyn", "start": "19:00", "end": "21:00", "title": "Basseyn",
         "priority": 2, "scored": True, "points": 10},
    ],
    2: [  # Chorshanba
        {"slug": "fizika", "start": "07:20", "end": "08:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "zal", "start": "09:00", "end": "11:00", "title": "Zal mashg'uloti",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "dush", "start": "11:00", "title": "Dush, kiyinish", "priority": 1, "scored": False},
        {"slug": "universitet", "start": "13:40", "title": "Universitet darslari boshlanadi",
         "priority": 2, "scored": False},
    ],
    3: [  # Payshanba
        {"slug": "fizika", "start": "07:20", "end": "08:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "universitet", "start": "13:40", "title": "Universitet darslari boshlanadi",
         "priority": 2, "scored": False},
        {"slug": "basseyn", "start": "19:00", "end": "21:00", "title": "Basseyn",
         "priority": 2, "scored": True, "points": 10},
    ],
    4: [  # Juma
        {"slug": "ish", "start": "07:00", "end": "14:45", "title": "Maktabda ish",
         "priority": 2, "scored": False},
        {"slug": "ish_tugash", "start": "14:45", "title": "Ish tugadi",
         "note": "Endi o'qish/tayyorgarlik vaqti", "priority": 1, "scored": False},
        # DIQQAT: fizika (17:20-18:40) va zal (18:00-20:00) ustma-ust tushadi (40 daqiqa).
        {"slug": "fizika", "start": "17:20", "end": "18:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "zal", "start": "18:00", "end": "20:00", "title": "Zal mashg'uloti",
         "priority": 2, "scored": True, "points": 10},
    ],
    5: [  # Shanba
        {"slug": "ish", "start": "07:00", "end": "14:45", "title": "Maktabda ish",
         "priority": 2, "scored": False},
        {"slug": "ish_tugash", "start": "14:45", "title": "Ish tugadi", "priority": 1, "scored": False},
        {"slug": "fizika", "start": "17:20", "end": "18:40", "title": "Fizika: nazariy qism va tayyorlanish",
         "priority": 2, "scored": True, "points": 10},
        {"slug": "basseyn", "start": "19:00", "end": "21:00", "title": "Basseyn",
         "priority": 2, "scored": True, "points": 10},
    ],
    6: [],  # Yakshanba: dam olish
}

# Namozlar Aladhan API dan olinadi. Bular qazo tizimida kuzatiladi va ballga ta'sir qiladi.
PRAYER_NAMES = ["Bomdod", "Peshin", "Asr", "Shom", "Xufton"]

# Maktab jadvali: I-smena qo'ng'iroq vaqtlari (dars raqami -> (kirish, chiqish))
SCHOOL_BELLS = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("09:50", "10:35"),
    4: ("10:50", "11:35"),
    5: ("11:45", "12:30"),
    6: ("12:40", "13:25"),
    7: ("13:30", "14:15"),
}

# Maktabdagi darslar: kun -> [dars raqami bo'yicha ketma-ket sinflar]
# Bot orqali o'zgartiriladi (sinflar tartibi ba'zan o'zgaradi).
SCHOOL_CLASSES = {
    4: ["7b", "8b", "11b", "9b", "10b"],   # Juma
    5: ["7b", "8b", "10b", "11b", "9b"],   # Shanba
}

# Universitet jadvali (23/3 guruh). Faqat Dushanba-Payshanba, juma-shanba amaliyot.
# (m) - ma'ruza, (a) - amaliy, (s) - seminar, (l) - laboratoriya
UNIVERSITY = {
    0: [  # Dushanba
        ("13:40", "15:00", "Astrofizika (m)", "1/405"),
        ("15:10", "16:30", "Yadro va elementar zarralar fizikasi (m)", "1/405"),
        ("16:40", "18:00", "Astrofizika (a)", "1/404"),
    ],
    1: [  # Seshanba
        ("13:40", "15:00", "Lazer fizikasi (m)", "1/405"),
        ("15:10", "16:30", "Yadro va elementar zarralar fizikasi (a)", "1/404"),
        ("16:40", "18:00", "Fizikadan masalalar yechish metodikasi (s)", "1/404"),
    ],
    2: [  # Chorshanba
        ("13:40", "15:00", "Fizika o'qitish metodikasi II (m)", "1/405"),
        ("15:10", "16:30", "Fizikadan masalalar yechish metodikasi (a)", "1/404"),
        ("16:40", "18:00", "SCIENCE fanini o'qitish metodikasi (s)", "1/404"),
    ],
    3: [  # Payshanba
        ("13:40", "15:00", "SCIENCE fanini o'qitish metodikasi (m)", "1/405"),
        ("15:10", "16:30", "Lazer fizikasi (s)", "1/404"),
        ("16:40", "18:00", "Fizika o'qitish metodikasi (l)", "1/107"),
    ],
}
