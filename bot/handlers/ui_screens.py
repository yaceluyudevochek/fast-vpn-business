"""
Показ "экранов" пользовательской части бота: картинка + подпись + клавиатура.

Каждый смысловой блок бота (старт, покупка, тарифы, оплата, подписки,
информация, рефералка) может сопровождаться своей картинкой. Картинки —
опциональны: если файл для ключа не найден в assets/images, экран
отправляется обычным текстовым сообщением (без фото) — бот полностью
white-label и работает "из коробки" без какого-либо брендинга. Чтобы не
грузить файл заново на каждый показ, после первой отправки картинки её
Telegram file_id кэшируется в памяти процесса.
"""

import logging
import os
from typing import Optional, Union

from aiogram.types import (
    CallbackQuery,
    Message,
    FSInputFile,
    InputMediaPhoto,
    InlineKeyboardMarkup,
)

logger = logging.getLogger(__name__)

_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "images"
)

# Имена файлов — только заготовка. Положите свою картинку под этим именем
# в assets/images, либо ничего не кладите — тогда экран отправится текстом.
IMAGES = {
    "start": "start.png",
    "buy": "buy.png",
    "tariffs": "tariffs.png",
    "payment": "payment.png",
    "subs": "subscriptions.png",
    "info": "info.png",
    "referral": "referral.png",
}

_file_id_cache: dict[str, str] = {}


def _has_image(image_key: str) -> bool:
    if image_key in _file_id_cache:
        return True
    filename = IMAGES.get(image_key)
    return bool(filename) and os.path.isfile(os.path.join(_ASSETS_DIR, filename))


def _photo(image_key: str):
    if image_key in _file_id_cache:
        return _file_id_cache[image_key]
    return FSInputFile(os.path.join(_ASSETS_DIR, IMAGES[image_key]))


def _remember(image_key: str, message: Optional[Message]):
    if message is not None and message.photo:
        _file_id_cache[image_key] = message.photo[-1].file_id


async def show_screen(
    target: Union[CallbackQuery, Message],
    image_key: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    """
    Показать экран: если target — CallbackQuery, обновляет существующее сообщение
    (меняя картинку и подпись через edit_media, либо просто текст через edit_text,
    если картинки для этого ключа нет); если Message — отправляет новое сообщение
    (фото или текст).
    """
    has_image = _has_image(image_key)

    if isinstance(target, CallbackQuery):
        message = target.message

        if not has_image:
            try:
                if message.photo:
                    raise ValueError("switching from photo to text screen")
                result = await message.edit_text(
                    text, parse_mode="HTML", reply_markup=reply_markup
                )
                return result
            except Exception as e:
                logger.debug(f"edit_text failed, sending fresh text message: {e}")
                try:
                    await message.delete()
                except Exception:
                    pass
                return await message.answer(
                    text, parse_mode="HTML", reply_markup=reply_markup
                )

        photo = _photo(image_key)
        try:
            result = await message.edit_media(
                media=InputMediaPhoto(media=photo, caption=text, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
            _remember(image_key, result)
            return result
        except Exception as e:
            logger.debug(f"edit_media failed, sending fresh photo instead: {e}")
            try:
                await message.delete()
            except Exception:
                pass
            result = await message.answer_photo(
                _photo(image_key),
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            _remember(image_key, result)
            return result
    else:
        if not has_image:
            return await target.answer(
                text, parse_mode="HTML", reply_markup=reply_markup
            )

        photo = _photo(image_key)
        result = await target.answer_photo(
            photo, caption=text, parse_mode="HTML", reply_markup=reply_markup
        )
        _remember(image_key, result)
        return result
