from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import logging
from datetime import datetime

from database import db
from services.platega_api import PlategaAPI
from config.config import (
    PLATEGA_MERCHANT_ID,
    PLATEGA_SECRET_KEY,
    PLATEGA_API_URL,
    PLATEGA_SUCCESS_URL,
    PLATEGA_FAILED_URL
)
from handlers.ui_screens import show_screen
from handlers.ui_emoji import E, kb_button

logger = logging.getLogger(__name__)

# Инициализация API Platega
platega_api = PlategaAPI(PLATEGA_MERCHANT_ID, PLATEGA_SECRET_KEY, PLATEGA_API_URL)

router = Router()


# ==========================================================
# FSM ДЛЯ ВЫБОРА СПОСОБА ОПЛАТЫ
# ==========================================================

class PaymentMethod(StatesGroup):
    selecting_method = State()  # выбор способа оплаты
    confirming = State()  # подтверждение


# ==========================================================
# ВЫБОР ПЛАНА (модифицируем существующий хендлер)
# ==========================================================

@router.callback_query(F.data.startswith("select_plan_"))
async def select_plan(callback: CallbackQuery):
    """Выбор плана (модифицировано для оплаты)"""
    plan_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    plan = await db.get_tariff_plan(plan_id)
    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    # Проверяем, есть ли уже подписка на этот тип тарифа
    subscriptions = await db.get_user_subscriptions(user_id)
    existing_sub = None
    for sub in subscriptions:
        if sub['tariff_type_id'] == plan['tariff_type_id'] and sub['status'] == 'ACTIVE':
            existing_sub = sub
            break

    traffic_text = "безлимит" if plan[
                                     'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

    if existing_sub:
        # Если есть активная - предлагаем продлить
        text = (
            f"📦 <b>{plan['tariff_name']}</b>\n\n"
            f"У вас уже есть активная подписка на этот тариф.\n\n"
            f"📅 Текущий срок: {existing_sub['tariff_days']} дней\n"
            f"💰 Цена продления: {plan['price']} руб. за {plan['days']} дней\n"
            f"📊 Лимит трафика: {traffic_text}\n\n"
            f"Хотите продлить подписку?"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Продлить",
                callback_data=f"pay_plan_{plan_id}"  # Изменено с renew_plan_ на pay_plan_
            )],
            [InlineKeyboardButton(
                text="« Назад к планам",
                callback_data=f"select_type_{plan['tariff_type_id']}"
            )]
        ])
    else:
        # Если нет - предлагаем купить
        text = (
            f"📦 <b>{plan['tariff_name']}</b>\n\n"
            f"📅 Срок: {plan['days']} дней\n"
            f"💰 Цена: {plan['price']} руб.\n"
            f"📊 Лимит трафика: {traffic_text}\n"
            f"📱 Лимит устройств: {plan['hwid_limit']}\n\n"
            f"Хотите приобрести эту подписку?"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✅ Купить",
                callback_data=f"pay_plan_{plan_id}"  # Изменено с buy_plan_ на pay_plan_
            )],
            [InlineKeyboardButton(
                text="« Назад к планам",
                callback_data=f"select_type_{plan['tariff_type_id']}"
            )]
        ])

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ==========================================================
# ВЫБОР СПОСОБА ОПЛАТЫ
# ==========================================================


@router.callback_query(F.data.startswith("pay_plan_"))
async def choose_payment_method(callback: CallbackQuery, state: FSMContext):
    """Выбор способа оплаты"""
    plan_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    plan = await db.get_tariff_plan(plan_id)
    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    # Проверяем, есть ли уже активная подписка этого типа
    subscriptions = await db.get_user_subscriptions(user_id)
    existing_sub = None
    for sub in subscriptions:
        if sub['tariff_type_id'] == plan['tariff_type_id'] and sub['status'] == 'ACTIVE':
            existing_sub = sub
            break

    # Определяем, лимитный это тариф
    is_unlimited = (plan['traffic_limit_bytes'] == 0)

    # Сохраняем план в состоянии
    await state.update_data(plan_id=plan_id)
    await state.set_state(PaymentMethod.selecting_method)

    # Клавиатура со способами оплаты
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("pay", "СБП (QR-код)", callback_data="payment_method_2")],
        [kb_button("crypto", "Криптовалюта", callback_data="payment_method_13")],
        [kb_button("back", "Назад", callback_data=f"select_plan_{plan_id}")]
    ])

    traffic_text = "безлимит" if is_unlimited else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

    # Формируем текст с предупреждением
    text = f"{E['money']} <b>{plan['tariff_name']}</b>\n\n"
    text += f"Сумма к оплате: <b>{plan['price']} руб.</b>\n"
    text += f"{E['calendar']} Срок: <b>{plan['days']} дней</b>\n"
    text += f"{E['chart']} Трафик: <b>{traffic_text}</b>\n\n"

    # Добавляем предупреждение для лимитных тарифов
    if not is_unlimited and existing_sub:
        text += f"{E['warn']} <b>Внимание!</b> У тебя уже есть активная подписка этого типа.\n"
        text += "При покупке нового тарифа:\n"
        text += "• Текущий остаток дней и гигабайтов будет сброшен\n"
        text += "• Ты получишь новый срок и новый лимит трафика\n"
        text += "• Счётчик трафика будет обнулён\n\n"
    elif not is_unlimited:
        text += f"{E['info']} <b>Информация:</b> это тариф с ограничением по трафику.\n"
        text += "При исчерпании лимита доступ будет заблокирован до продления.\n\n"

    text += f"Выбери способ оплаты {E['sparkle']}"

    await show_screen(callback, "payment", text, keyboard)
    await callback.answer()


# ==========================================================
# ОБРАБОТКА ВЫБРАННОГО СПОСОБА ОПЛАТЫ
# ==========================================================

@router.callback_query(PaymentMethod.selecting_method, F.data.startswith("payment_method_"))
async def process_payment_method(callback: CallbackQuery, state: FSMContext):
    """Обработка выбранного способа оплаты"""

    payment_method = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    data = await state.get_data()
    plan_id = data.get('plan_id')

    if not plan_id:
        await callback.answer("❌ Ошибка: выберите план заново", show_alert=True)
        await state.clear()
        return

    plan = await db.get_tariff_plan(plan_id)
    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        await state.clear()
        return

    await callback.answer("⏳ Создаю платеж...")

    # ==========================================================
    # ОПРЕДЕЛЯЕМ, ЕСТЬ ЛИ ПОДПИСКА (АКТИВНАЯ ИЛИ ИСТЕКШАЯ)
    # ==========================================================
    subscriptions = await db.get_user_subscriptions(user_id)
    existing_sub = None
    for sub in subscriptions:
        if sub['tariff_type_id'] == plan['tariff_type_id']:  # Любая подписка этого типа
            existing_sub = sub
            break

    # Определяем действие
    if existing_sub:
        if existing_sub['status'] == 'ACTIVE':
            action = 'renew_active'  # Продление активной
            action_text = "продление активной подписки"
        else:
            action = 'renew_expired'  # Продление истекшей
            action_text = "восстановление истекшей подписки"
    else:
        action = 'new'  # Новая подписка
        action_text = "новая подписка"

    # Формируем описание для платежа
    traffic_text = "безлимит" if plan[
                                     'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"
    description = f"Оплата {plan['tariff_name']} на {plan['days']} дней ({traffic_text})"

    # ==========================================================
    # ФОРМИРУЕМ PAYLOAD С ИНФОРМАЦИЕЙ О ПОДПИСКЕ
    # ==========================================================
    # Формат: user_id_plan_id_action_timestamp_[subscription_id]
    payload_parts = [
        str(user_id),
        str(plan_id),
        action,
        str(int(datetime.now().timestamp()))
    ]

    # Если есть существующая подписка, добавляем её ID для точности
    if existing_sub:
        payload_parts.append(str(existing_sub['id']))

    payload = '_'.join(payload_parts)

    logger.info(f"📦 Создание платежа: action={action}, action_text={action_text}, payload={payload}")

    # ==========================================================
    # СОЗДАЕМ ПЛАТЕЖ В PLATEGA.IO
    # ==========================================================
    payment_data = await platega_api.create_payment(
        amount=plan['price'],
        description=description,
        payload=payload,
        payment_method=payment_method,
        return_url=PLATEGA_SUCCESS_URL,
        failed_url=PLATEGA_FAILED_URL
    )

    if payment_data and payment_data.get('transactionId'):
        transaction_id = payment_data['transactionId']
        redirect_url = payment_data.get('redirect')

        # Сохраняем транзакцию в БД
        await db.create_payment_transaction(
            transaction_id=transaction_id,
            telegram_id=user_id,
            plan_id=plan_id,
            amount=plan['price'],
            payment_method=payment_method
        )

        # Отправляем пользователю ссылку на оплату
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [kb_button("pay", "Перейти к оплате", url=redirect_url)],
            [kb_button("check_payment", "Проверить оплату", callback_data=f"check_payment_{transaction_id}")],
            [kb_button("back", "Отмена", callback_data="back_to_main")]
        ])

        expires_in = payment_data.get('expiresIn', '01:00:00')

        await show_screen(
            callback,
            "payment",
            f"{E['check']} <b>Счёт на оплату создан!</b>\n\n"
            f"{E['diamond']} Тариф: <b>{plan['tariff_name']}</b>\n"
            f"{E['money']} Сумма: <b>{plan['price']} руб.</b>\n"
            f"{E['calendar']} Срок: <b>{plan['days']} дней</b>\n"
            f"{E['chart']} Трафик: <b>{traffic_text}</b>\n"
            f"{E['clock']} Счёт действителен: <b>{expires_in}</b>\n\n"
            f"Нажми кнопку ниже для оплаты — подписка активируется автоматически {E['sparkle']}",
            keyboard
        )
    else:
        # Ошибка создания платежа
        logger.error(f"Failed to create payment: {payment_data}")

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [kb_button("back", "Назад к тарифам", callback_data="show_tariffs")]
        ])

        error_message = f"{E['cross']} <b>Ошибка при создании платежа</b>\n\n"

        if payment_data and payment_data.get('error') == 'timeout':
            error_message += "Платёжная система временно недоступна. Пожалуйста, попробуй позже."
        else:
            error_message += "Пожалуйста, попробуй позже или выбери другой способ оплаты."

        await show_screen(callback, "payment", error_message, keyboard)

    await state.clear()


# ==========================================================
# ПРОВЕРКА СТАТУСА ПЛАТЕЖА
# ==========================================================

@router.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: CallbackQuery):
    """Проверка статуса платежа"""

    transaction_id = callback.data.split("_")[2]

    # Получаем транзакцию из БД
    transaction = await db.get_transaction(transaction_id)

    if not transaction:
        await callback.answer("❌ Транзакция не найдена", show_alert=True)
        return

    if transaction['status'] == 'CONFIRMED':
        # Платеж уже подтвержден
        plan = await db.get_tariff_plan(transaction['plan_id'])

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [kb_button("subs", "Мои подписки", callback_data="my_subscriptions")],
            [kb_button("home", "В меню", callback_data="back_to_main")]
        ])

        await show_screen(
            callback,
            "payment",
            f"{E['check']} <b>Платёж подтверждён!</b>\n\n"
            f"Твоя подписка успешно активирована.\n"
            f"Перейди в раздел «Мои подписки», чтобы получить ссылку.",
            keyboard
        )

    elif transaction['status'] == 'PENDING':
        # Проверяем статус через API
        status_data = await platega_api.check_payment_status(transaction_id)

        if status_data and status_data.get('status'):
            api_status = status_data['status']

            if api_status == 'CONFIRMED':
                # Обновляем статус в БД
                await db.update_transaction_status(transaction_id, 'CONFIRMED')

                # Здесь можно вызвать handle_successful_payment, но лучше дождаться callback'а
                # Просто уведомляем пользователя

                await show_screen(
                    callback,
                    "payment",
                    f"{E['check']} <b>Платёж подтверждён!</b>\n\n"
                    f"Подписка будет активирована в ближайшее время.",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [kb_button("home", "В меню", callback_data="back_to_main")]
                    ])
                )
            elif api_status == 'CANCELED':
                await db.update_transaction_status(transaction_id, 'CANCELED')
                await callback.answer("❌ Платеж отменен", show_alert=True)
            else:
                # Все еще ждем
                await callback.answer("⏳ Платеж еще не оплачен", show_alert=True)
        else:
            await callback.answer("❌ Ошибка проверки статуса", show_alert=True)

    elif transaction['status'] == 'CANCELED':
        await callback.answer("❌ Платеж был отменен", show_alert=True)
    else:
        await callback.answer(f"ℹ️ Статус платежа: {transaction['status']}", show_alert=True)