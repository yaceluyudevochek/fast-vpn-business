"""Общие куски интерфейса промокодов: строка с ценой и кнопка на экранах плана и оплаты."""
from typing import Optional

from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton

from handlers.ui_emoji import E, kb_button
from services.promo import PromoCheck, check_promo


async def saved_promo_code(state: Optional[FSMContext]) -> Optional[str]:
    if state is None:
        return None
    return (await state.get_data()).get("promo_code")


async def promo_check_for_state(state: Optional[FSMContext], telegram_id: int, plan: dict) -> Optional[PromoCheck]:
    """Проверка сохранённого в диалоге промокода для конкретного плана (None — кода нет)"""
    code = await saved_promo_code(state)
    if not code:
        return None
    return await check_promo(code, telegram_id, plan)


def price_lines(plan: dict, check: Optional[PromoCheck], code: Optional[str]) -> str:
    """Строка(и) с ценой: обычная, со скидкой по промокоду или с причиной, почему код не подошёл"""
    price = plan["price"]
    if check is None:
        return f"{E['money']} Цена: <b>{price} руб.</b>\n"
    if check.ok:
        percent = check.promo["discount_percent"]
        return (
            f"{E['money']} Цена: <s>{check.original_price} руб.</s> → <b>{check.final_price} руб.</b>"
            f" (−{percent}%)\n"
            f"{E['tag']} Промокод: <b>{check.promo['code']}</b>\n"
        )
    return (
        f"{E['money']} Цена: <b>{price} руб.</b>\n"
        f"{E['warn']} Промокод {code}: {check.message}\n"
    )


def promo_row(plan_id: int, subscription_id: int, check: Optional[PromoCheck]) -> list:
    """Кнопка на экране плана: ввести промокод или убрать уже введённый.
    subscription_id = 0 — покупка, иначе продление этой подписки."""
    if check is None:
        return [kb_button("tag", "Ввести промокод", callback_data=f"promo_enter_{plan_id}_{subscription_id}")]
    return [kb_button("cross", "Убрать промокод", callback_data=f"promo_remove_{plan_id}_{subscription_id}")]
