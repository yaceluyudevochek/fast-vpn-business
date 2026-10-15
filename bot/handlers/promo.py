"""Пользовательская часть промокодов: ввод кода на экране плана, снятие кода."""
import logging
import time
from collections import defaultdict, deque

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from database import db as database
from database import promo_db
from handlers.ui_emoji import E, kb_button
from handlers.ui_screens import show_screen
from services.promo import check_promo, normalize_code

router = Router()
logger = logging.getLogger(__name__)

# Защита от подбора кодов: не больше N неудачных попыток за окно
MAX_FAILED_ATTEMPTS = 5
ATTEMPTS_WINDOW_SECONDS = 600
_failed_attempts = defaultdict(deque)


class PromoInput(StatesGroup):
    code = State()


def _rate_limited(user_id: int) -> bool:
    attempts = _failed_attempts[user_id]
    now = time.monotonic()
    while attempts and now - attempts[0] > ATTEMPTS_WINDOW_SECONDS:
        attempts.popleft()
    return len(attempts) >= MAX_FAILED_ATTEMPTS


def _parse(data: str):
    """promo_<action>_{plan_id}_{subscription_id}"""
    parts = data.split("_")
    return int(parts[2]), int(parts[3])


async def render_plan_screen(target, user_id: int, plan_id: int, subscription_id: int, state: FSMContext):
    """Показать экран плана (покупка или продление) с учётом сохранённого промокода"""
    from handlers.handlers import render_renew_plan, render_select_plan

    if subscription_id:
        return await render_renew_plan(target, user_id, plan_id, subscription_id, state)
    return await render_select_plan(target, user_id, plan_id, state)


@router.callback_query(F.data.startswith("promo_enter_"))
async def promo_enter(callback: CallbackQuery, state: FSMContext):
    try:
        plan_id, subscription_id = _parse(callback.data)
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    await state.set_state(PromoInput.code)
    await state.update_data(promo_plan_id=plan_id, promo_sub_id=subscription_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("back", "Отмена", callback_data=f"promo_cancel_{plan_id}_{subscription_id}")]
    ])
    await show_screen(
        callback, "payment",
        f"{E['tag']} <b>Промокод</b>\n\nОтправь промокод одним сообщением.",
        keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("promo_cancel_"))
async def promo_cancel(callback: CallbackQuery, state: FSMContext):
    try:
        plan_id, subscription_id = _parse(callback.data)
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return
    await state.set_state(None)
    error = await render_plan_screen(callback, callback.from_user.id, plan_id, subscription_id, state)
    await callback.answer(error or None, show_alert=bool(error))


@router.callback_query(F.data.startswith("promo_remove_"))
async def promo_remove(callback: CallbackQuery, state: FSMContext):
    try:
        plan_id, subscription_id = _parse(callback.data)
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return
    await state.update_data(promo_code=None)
    error = await render_plan_screen(callback, callback.from_user.id, plan_id, subscription_id, state)
    await callback.answer(error or "Промокод убран", show_alert=bool(error))


@router.message(PromoInput.code, F.text)
async def promo_code_input(message: Message, state: FSMContext):
    data = await state.get_data()
    plan_id, subscription_id = data.get("promo_plan_id"), data.get("promo_sub_id", 0)
    user_id = message.from_user.id

    plan = await database.get_tariff_plan(plan_id) if plan_id else None
    if not plan:
        await state.set_state(None)
        await message.answer("❌ План не найден, выбери тариф заново через /start")
        return

    cancel_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("back", "Отмена", callback_data=f"promo_cancel_{plan_id}_{subscription_id}")]
    ])

    if _rate_limited(user_id):
        await message.answer(
            f"{E['warn']} Слишком много неверных попыток. Попробуй ещё раз через несколько минут.",
            reply_markup=cancel_keyboard, parse_mode="HTML"
        )
        return

    code = normalize_code(message.text)
    check = await check_promo(code, user_id, plan)
    if not check.ok:
        _failed_attempts[user_id].append(time.monotonic())
        await message.answer(
            f"{E['cross']} {check.message}.\nПроверь код и отправь ещё раз.",
            reply_markup=cancel_keyboard, parse_mode="HTML"
        )
        return

    await state.set_state(None)
    await state.update_data(promo_code=check.promo["code"])
    await promo_db.record_event(check.promo["id"], user_id, "applied")

    try:
        await message.delete()  # не оставляем код в переписке
    except Exception:
        pass

    error = await render_plan_screen(message, user_id, plan_id, subscription_id, state)
    if error:
        await message.answer(error)
