"""
Чёрно-белые анимированные эмодзи (набор "TG iOS & macOS Icons") для текстов
сообщений и для иконок на кнопках.

Custom emoji в ТЕКСТЕ сообщений/подписей видны всем пользователям Telegram
(не только Premium) — через <tg-emoji emoji-id="...">, ограничение Premium
касается только отправки таких эмодзи людьми, боты используют их свободно.

Custom emoji ИКОНКА НА КНОПКЕ (icon_custom_emoji_id, Bot API 9.4+) — работает,
только если у владельца бота есть Telegram Premium (или куплен доп. юзернейм
на Fragment). Раз это условие выполнено, дублируем unicode-эмодзи в тексте
кнопки иконкой ICON — на новых клиентах будет красивая анимированная иконка,
на старых (где поле ещё не поддерживается) отобразится хотя бы unicode-эмодзи
из текста, кнопка не останется пустой.
"""

# emoji_id и unicode-запасной символ для каждого смыслового значка (набор tgmacicons)
_ID = {
    "wave": "5258337316715373336",
    "sparkle": "5260726538302660868",
    "diamond": "5280962371207077415",
    "gift": "5773677501825945508",
    "shield": "5258476306152038031",
    "tag": "5296348778012361146",
    "money": "5258204546391351475",
    "link": "5260730055880876557",
    "check": "5260726538302660868",
    "cross": "5260342697075416641",
    "clock": "5258419835922030550",
    "warn": "5258474669769497337",
    "info": "5258503720928288433",
    "home": "5257963315258204021",
    "calendar": "5258105663359294787",
    "chart": "5258391025281408576",
    "bulb": "5258216851472654189",
    "people": "5258513401784573443",
    "repeat": "5258420634785947640",
    "doc": "5257965174979042426",
    "chat": "5258215846450305872",
    "bitcoin": "5258368777350816286",
    "wallet": "5769126056262898415",
}
_FALLBACK = {
    "wave": "🤙",
    "sparkle": "✅",
    "diamond": "💎",
    "gift": "🎁",
    "shield": "🔒",
    "tag": "🏷",
    "money": "💰",
    "link": "⛓",
    "check": "✅",
    "cross": "❌",
    "clock": "🕔",
    "warn": "❗️",
    "info": "ℹ️",
    "home": "🏘",
    "calendar": "🗓",
    "chart": "📈",
    "bulb": "💡",
    "people": "👥",
    "repeat": "🔄",
    "doc": "📝",
    "chat": "💬",
    "bitcoin": "🪙",
    "wallet": "👛",
}
_ID["drop"] = _ID["info"]
_FALLBACK["drop"] = _FALLBACK["info"]

# Анимированные эмодзи для текста/подписей: E["diamond"] -> "<tg-emoji ...>💎</tg-emoji>"
E = {key: f'<tg-emoji emoji-id="{_ID[key]}">{_FALLBACK[key]}</tg-emoji>' for key in _ID}

# Соответствие "смысл кнопки" -> ключ в наборе выше (None = своей иконки нет, только unicode)
_BTN_ICON_KEY = {
    "buy": "diamond",
    "trial": "gift",
    "subs": "shield",
    "referral": "people",
    "info": "info",
    "back": None,
    "confirm": "check",
    "cancel": "cross",
    "home": "home",
    "pay": "wallet",
    "crypto": "bitcoin",
    "check_payment": "repeat",
    "reset": "repeat",
    "link": "link",
    "calendar": "calendar",
    "warn": "warn",
    "doc": "doc",
    "chat": "chat",
    "tag": "tag",
    "money": "money",
    "diamond": "diamond",
    "shield": "shield",
    "people": "people",
    "check": "check",
    "cross": "cross",
    "clock": "clock",
}
_BTN_FALLBACK_OVERRIDE = {
    "back": "◂",
}

# Обычные unicode-эмодзи для текста кнопок (гарантированно видны всем)
# emoji_id для icon_custom_emoji_id кнопок (только там, где есть подходящая иконка)
BTN = {}
ICON = {}
for _key, _icon_key in _BTN_ICON_KEY.items():
    if _icon_key is None:
        BTN[_key] = _BTN_FALLBACK_OVERRIDE[_key]
    else:
        BTN[_key] = _FALLBACK[_icon_key]
        ICON[_key] = _ID[_icon_key]


def kb_button(icon_key, label, **kwargs):
    """
    Кнопка с иконкой по смысловому ключу (см. _BTN_ICON_KEY).
    Если для ключа есть custom emoji — иконка идёт через icon_custom_emoji_id,
    а в текст ничего не дублируется. Если своей иконки нет (icon_key не найден
    или None) — используется unicode-запасной символ прямо в тексте кнопки.
    """
    from aiogram.types import InlineKeyboardButton

    icon_id = ICON.get(icon_key)
    if icon_id:
        return InlineKeyboardButton(text=label, icon_custom_emoji_id=icon_id, **kwargs)

    fallback = BTN.get(icon_key, "")
    text = f"{fallback} {label}".strip() if fallback else label
    return InlineKeyboardButton(text=text, **kwargs)
