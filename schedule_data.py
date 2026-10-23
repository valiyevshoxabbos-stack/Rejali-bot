"""
Standart haftalik reja (shablon). Bot birinchi ishga tushganda bazaga yuklaydi;
keyin vaqt va ballar botning o'zida (/vaqt, /ball, Sozlamalar) o'zgartiriladi.

slug - qisqa, bo'sh joysiz id. priority: 1 oddiy, 2 muhim, 3 juda muhim.
Haftalik kunlar: 0 = Dushanba ... 6 = Yakshanba.
"""

DAILY = [
    {"slug": "yotish", "start": "22:00", "title": "Yotish vaqti",
     "note": "Ertaga bomdodga turish uchun muhim", "priority": 3, "scored": True, "points": 15},
]

_FIZIKA = {"slug": "fizika", "start": "07:20", "end": "08:40",
           "title": "Fizika: nazariy qism va tayyorlanish", "priority": 2, "scored": True, "points": 10}
_ZAL_AM = {"slug": "zal", "start": "09:00", "end": "11:00", "title": "Zal mashg'uloti",
           "priority": 2, "scored": True, "points": 10}
_DUSH = {"slug": "dush", "start": "11:00", "title": "Dush, kiyinish", "priority": 1, "scored": False}
_UNI = {"slug": "universitet", "start": "13:40", "title": "Universitet darslari boshlanadi",
        "priority": 2, "scored": False}
_BASSEYN = {"slug": "basseyn", "start": "19:00", "end": "21:00", "title": "Basseyn",
            "priority": 2, "scored": True, "points": 10}

WEEKLY = {
    0: [dict(_FIZIKA), dict(_ZAL_AM), dict(_DUSH), dict(_UNI)],                    # Dushanba
    1: [dict(_FIZIKA), dict(_UNI), dict(_BASSEYN)],                                # Seshanba
    2: [dict(_FIZIKA), dict(_ZAL_AM), dict(_DUSH), dict(_UNI)],                    # Chorshanba
    3: [dict(_FIZIKA), dict(_UNI), dict(_BASSEYN)],                                # Payshanba
    4: [  # Juma. Fizika 17:20-18:40 zal (18:00) bilan to'qnashgani uchun 15:30 ga surilgan.
        {"slug": "ish", "start": "07:00", "end": "14:45", "title": "Maktabda ish",
         "priority": 2, "scored": False},
        {"slug": "ish_tugash", "start": "14:45", "title": "Ish tugadi",
         "note": "Endi o'qish/tayyorgarlik vaqti", "priority": 1, "scored": False},
        {**_FIZIKA, "start": "15:30", "end": "16:50"},
        {"slug": "zal", "start": "18:00", "end": "20:00", "title": "Zal mashg'uloti",
         "priority": 2, "scored": True, "points": 10},
    ],
    5: [  # Shanba
        {"slug": "ish", "start": "07:00", "end": "14:45", "title": "Maktabda ish",
         "priority": 2, "scored": False},
        {"slug": "ish_tugash", "start": "14:45", "title": "Ish tugadi", "priority": 1, "scored": False},
        {**_FIZIKA, "start": "17:20", "end": "18:40"},
        dict(_BASSEYN),
    ],
    6: [],  # Yakshanba: dam olish
}

PRAYER_NAMES = ["Bomdod", "Peshin", "Asr", "Shom", "Xufton"]

# Maktab: I-smena qo'ng'iroq vaqtlari (dars raqami -> (kirish, chiqish))
SCHOOL_BELLS = {
    1: ("08:00", "08:45"), 2: ("08:55", "09:40"), 3: ("09:50", "10:35"),
    4: ("10:50", "11:35"), 5: ("11:45", "12:30"), 6: ("12:40", "13:25"), 7: ("13:30", "14:15"),
}

# Maktab sinflari tartibi (bot orqali /sinf bilan o'zgartiriladi)
SCHOOL_CLASSES = {
    4: ["7b", "8b", "11b", "9b", "10b"],   # Juma
    5: ["7b", "8b", "10b", "11b", "9b"],   # Shanba
}

# Universitet jadvali (23/3 guruh), (boshlanish, tugash, fan, xona)
UNIVERSITY = {
    0: [("13:40", "15:00", "Astrofizika (m)", "1/405"),
        ("15:10", "16:30", "Yadro va elementar zarralar fizikasi (m)", "1/405"),
        ("16:40", "18:00", "Astrofizika (a)", "1/404")],
    1: [("13:40", "15:00", "Lazer fizikasi (m)", "1/405"),
        ("15:10", "16:30", "Yadro va elementar zarralar fizikasi (a)", "1/404"),
        ("16:40", "18:00", "Fizikadan masalalar yechish metodikasi (s)", "1/404")],
    2: [("13:40", "15:00", "Fizika o'qitish metodikasi II (m)", "1/405"),
        ("15:10", "16:30", "Fizikadan masalalar yechish metodikasi (a)", "1/404"),
        ("16:40", "18:00", "SCIENCE fanini o'qitish metodikasi (s)", "1/404")],
    3: [("13:40", "15:00", "SCIENCE fanini o'qitish metodikasi (m)", "1/405"),
        ("15:10", "16:30", "Lazer fizikasi (s)", "1/404"),
        ("16:40", "18:00", "Fizika o'qitish metodikasi (l)", "1/107")],
}
