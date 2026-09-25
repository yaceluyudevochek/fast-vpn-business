import logging
from aiohttp import web
import hmac
import hashlib
from datetime import datetime, timedelta

from database import db
from services.panel_api import PanelAPI
from config.config import (
    PLATEGA_MERCHANT_ID,
    PLATEGA_SECRET_KEY,
    PANEL_API_URL,
    PANEL_API_KEY
)

logger = logging.getLogger(__name__)

# Инициализируем API панели (будет передан из main)
panel_api = None


def init_webhook(panel_api_instance):
    """Инициализация с экземпляром PanelAPI"""
    global panel_api
    panel_api = panel_api_instance


async def handle_platega_callback(request):
    """
    Обработчик callback'ов от Platega.io
    Документация: https://app.apidog.com/web/project/1095671/apis/api-22645075-run
    """
    try:
        # Получаем данные запроса
        data = await request.json()
        headers = request.headers

        logger.info(f"📩 Получен callback от Platega: {data}")

        # Проверяем заголовки аутентификации
        merchant_id = headers.get('X-MerchantId')
        secret = headers.get('X-Secret')

        # Базовая проверка (можно усилить)
        if merchant_id != PLATEGA_MERCHANT_ID or secret != PLATEGA_SECRET_KEY:
            logger.warning(f"❌ Неверные заголовки аутентификации: {merchant_id}")
            return web.Response(status=401, text="Unauthorized")

        # Извлекаем данные из callback'а
        transaction_id = data.get('id')
        amount = data.get('amount')
        currency = data.get('currency')
        status = data.get('status')  # CONFIRMED или CANCELED
        payment_method = data.get('paymentMethod')
        payload = data.get('payload')  # наши данные (telegram_id_plan_id_timestamp)

        if not all([transaction_id, status, payload]):
            logger.error(f"❌ Неполные данные в callback: {data}")
            return web.Response(status=400, text="Bad Request")

        logger.info(f"✅ Callback валиден: транзакция {transaction_id}, статус {status}")

        # Получаем транзакцию из БД
        transaction = await db.get_transaction(transaction_id)

        if not transaction:
            # Если транзакции нет в БД, возможно это тестовый callback
            # Пробуем найти по payload
            transaction = await db.get_transaction_by_payload(payload)

            if not transaction:
                logger.error(f"❌ Транзакция не найдена: {transaction_id}")
                return web.Response(status=404, text="Transaction not found")

        # Платежные шлюзы (в т.ч. Platega) могут прислать один и тот же callback
        # несколько раз (retry при задержке ответа, сетевые сбои и т.п.).
        # Запоминаем, была ли транзакция уже подтверждена ДО этого запроса,
        # чтобы не активировать/продлевать подписку повторно по дублю.
        already_confirmed = transaction.get('status') == 'CONFIRMED'

        # Обновляем статус в БД
        await db.update_transaction_status(transaction_id, status)

        # Если статус CONFIRMED - активируем подписку (но не повторно для дублей)
        if status == "CONFIRMED":
            if already_confirmed:
                logger.warning(
                    f"⚠️ Дублирующийся callback для уже подтверждённой транзакции "
                    f"{transaction_id}, повторная активация пропущена"
                )
            else:
                await handle_successful_payment(transaction)
        elif status == "CANCELED":
            await handle_failed_payment(transaction)
        elif status == "CHARGEBACKED":
            await handle_chargeback_payment(transaction)

        # Отвечаем Platega.io, что callback принят
        return web.Response(status=200, text="OK")

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке callback: {e}", exc_info=True)
        return web.Response(status=500, text="Internal Server Error")


# В payment_webhook.py

async def handle_successful_payment(transaction):
    """Обработка успешного платежа"""
    logger.info(f"💰 Успешный платеж для транзакции {transaction['transaction_id']}")

    try:
        # Парсим payload
        payload = transaction['payload']
        payload_parts = payload.split('_')
        user_id = int(payload_parts[0])
        plan_id = int(payload_parts[1])
        action = payload_parts[2]  # 'new', 'renew_active', 'renew_expired'

        # Получаем информацию о плане
        plan = await db.get_tariff_plan(plan_id)
        if not plan:
            logger.error(f"❌ План {plan_id} не найден")
            return

        # Определяем, лимитный это тариф или безлимитный
        is_unlimited = (plan['traffic_limit_bytes'] == 0)

        # Ищем существующую подписку
        existing_sub = None
        if len(payload_parts) >= 5:
            try:
                subscription_id = int(payload_parts[4])
                existing_sub = await db.get_subscription(subscription_id)
            except (ValueError, IndexError):
                pass

        if not existing_sub:
            subscriptions = await db.get_user_subscriptions(user_id)
            for sub in subscriptions:
                if sub['tariff_type_id'] == plan['tariff_type_id']:
                    existing_sub = sub
                    break

        if existing_sub:
            # ======================================================
            # ПРОДЛЕНИЕ СУЩЕСТВУЮЩЕЙ ПОДПИСКИ
            # ======================================================

            if is_unlimited:
                # ------------------------------------------------------
                # БЕЗЛИМИТНЫЙ ТАРИФ: добавляем дни к существующей дате
                # ------------------------------------------------------
                logger.info(f"🔄 Продление безлимитного тарифа для пользователя {user_id}")

                new_expiry = await panel_api.extend_subscription_by_username(
                    existing_sub['username_in_panel'],
                    plan['days'],
                    plan['traffic_limit_bytes']  # 0 - безлимит
                )

                if new_expiry:
                    await db.update_subscription_expiry(existing_sub['id'], new_expiry)

                    # Если подписка была неактивна, активируем
                    if existing_sub['status'] != 'ACTIVE':
                        await db.update_subscription_status(existing_sub['id'], 'ACTIVE')
                        await panel_api.update_user_status_by_username(
                            existing_sub['username_in_panel'],
                            "ACTIVE"
                        )

                    await notify_user_success(
                        user_id, plan, new_expiry,
                        is_renewal=True,
                        was_expired=(existing_sub['status'] != 'ACTIVE'),
                        is_unlimited=True
                    )
                else:
                    logger.error(f"❌ Ошибка продления безлимитной подписки для {user_id}")

            else:
                # ------------------------------------------------------
                # ЛИМИТНЫЙ ТАРИФ: устанавливаем ровно plan['days'] дней от текущего момента
                # ------------------------------------------------------
                logger.info(f"🔄 Продление лимитного тарифа для пользователя {user_id} (установка новой даты)")

                from datetime import datetime, timezone, timedelta

                # Вычисляем новую дату: текущий момент + plan['days'] дней
                now = datetime.now(timezone.utc)
                new_expire_dt = now + timedelta(days=plan['days'])
                new_expire_str = new_expire_dt.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

                # Проверяем, что пользователь существует в панели
                user_data = await panel_api.get_user_by_username(existing_sub['username_in_panel'])
                if not user_data:
                    logger.error(f"❌ Не удалось получить данные пользователя из панели")
                    return

                # Обновляем пользователя в панели: устанавливаем новую дату и лимит
                update_payload = {
                    "username": existing_sub['username_in_panel'],
                    "expireAt": new_expire_str,  # Новая дата, НЕ сумма с остатком!
                    "trafficLimitBytes": plan['traffic_limit_bytes'],
                    "status": "ACTIVE"
                }

                logger.info(f"📤 Отправка PATCH запроса для пользователя {user_id}: {update_payload}")

                response = await panel_api._request("PATCH", "users", json=update_payload)

                if response and not response.get("error"):
                    # УСПЕШНО: теперь сбрасываем счетчик трафика
                    logger.info(f"🔄 Сбрасываем счетчик трафика для пользователя {user_id}")
                    reset_success = await panel_api.reset_user_traffic(existing_sub['username_in_panel'])

                    if reset_success:
                        logger.info(f"✅ Подписка обновлена: новая дата {new_expire_str}")

                        # Обновляем БД
                        await db.update_subscription_expiry(existing_sub['id'], new_expire_str)
                        await db.update_subscription_status(existing_sub['id'], 'ACTIVE')

                        # Отправляем уведомление об успехе
                        await notify_user_success(
                            user_id, plan, new_expire_str,
                            is_renewal=True,
                            was_expired=(existing_sub['status'] != 'ACTIVE'),
                            is_unlimited=False,
                            reset_traffic=True
                        )
                    else:
                        logger.error(f"❌ Ошибка сброса трафика для {user_id}")
                        # Даже если сброс не удался, подписка должна работать с новым лимитом
                        await db.update_subscription_expiry(existing_sub['id'], new_expire_str)
                        await db.update_subscription_status(existing_sub['id'], 'ACTIVE')

                        await notify_user_success(
                            user_id, plan, new_expire_str,
                            is_renewal=True,
                            was_expired=(existing_sub['status'] != 'ACTIVE'),
                            is_unlimited=False,
                            reset_traffic=False,
                            reset_error=True
                        )
                        await notify_admin_error(
                            user_id,
                            f"Не удалось сбросить счетчик трафика для {existing_sub['username_in_panel']}"
                        )
                else:
                    logger.error(f"❌ Ошибка обновления подписки для {user_id}: {response}")

        else:
            # ======================================================
            # НОВАЯ ПОДПИСКА (код остается без изменений)
            # ======================================================
            logger.info(f"🆕 Создание новой подписки для пользователя {user_id}")

            # Формируем username для панели
            base_username = f"tg{user_id}_{plan['tariff_type_id']}"
            username_in_panel = base_username
            tag = f"U{user_id}_T{plan['tariff_type_id']}"
            if len(tag) > 16:
                tag = tag[:16]

            # Проверяем, не занят ли username
            existing = await panel_api.get_user_by_username(username_in_panel)
            if existing:
                # Если пользователь существует в панели, но у нас нет подписки в БД
                logger.warning(f"⚠️ Пользователь {username_in_panel} уже есть в панели, но нет в БД")

                # Активируем и продлеваем
                await panel_api.update_user_status_by_username(username_in_panel, "ACTIVE")

                if is_unlimited:
                    new_expiry = await panel_api.extend_subscription_by_username(
                        username_in_panel,
                        plan['days'],
                        plan['traffic_limit_bytes']
                    )
                else:
                    # Для лимитного тарифа устанавливаем новую дату
                    from datetime import datetime, timezone, timedelta
                    now = datetime.now(timezone.utc)
                    new_expire_dt = now + timedelta(days=plan['days'])
                    new_expiry = new_expire_dt.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

                    # Обновляем пользователя
                    update_payload = {
                        "username": username_in_panel,
                        "expireAt": new_expiry,
                        "trafficLimitBytes": plan['traffic_limit_bytes'],
                        "status": "ACTIVE"
                    }
                    await panel_api._request("PATCH", "users", json=update_payload)

                    # Сбрасываем трафик
                    await panel_api.reset_user_traffic(username_in_panel)

                # Создаем запись в БД
                short_uuid = existing.get('shortUuid')
                subscription_id = await db.create_subscription(
                    user_id,
                    plan['tariff_type_id'],
                    plan_id,
                    short_uuid,
                    new_expiry,
                    username_in_panel,
                    trial=False
                )
            else:
                # Создаем нового пользователя в панели
                new_user = await panel_api.create_user(
                    username=username_in_panel,
                    telegram_id=user_id,
                    squad_id=plan['squad_id'],
                    days=plan['days'],
                    hwid_limit=plan['hwid_limit'],
                    tag=tag,
                    traffic_limit_bytes=plan['traffic_limit_bytes']
                )

                if new_user:
                    short_uuid = new_user.get("shortUuid")
                    expiry = new_user.get("expireAt")

                    # Для лимитных тарифов убеждаемся, что счетчик с нуля
                    if not is_unlimited:
                        await panel_api.reset_user_traffic(username_in_panel)

                    # Сохраняем подписку в БД
                    subscription_id = await db.create_subscription(
                        user_id,
                        plan['tariff_type_id'],
                        plan_id,
                        short_uuid,
                        expiry,
                        username_in_panel,
                        trial=False
                    )
                else:
                    logger.error(f"❌ Ошибка создания пользователя в панели для {user_id}")
                    return

            # Отправляем уведомление
            await notify_user_success(
                user_id,
                plan,
                new_expiry if 'new_expiry' in locals() else expiry,
                is_renewal=False,
                was_expired=False,
                is_unlimited=is_unlimited
            )

    except Exception as e:
        logger.error(f"❌ Ошибка при активации подписки: {e}", exc_info=True)


async def handle_failed_payment(transaction):
    """Обработка неуспешного платежа"""
    logger.info(f"❌ Неуспешный платеж для транзакции {transaction['transaction_id']}")

    telegram_id = transaction['telegram_id']

    # Отправляем уведомление пользователю
    try:
        from aiogram import Bot
        from config.config import TOKEN

        bot = Bot(token=TOKEN)

        text = (
            "❌ <b>Платеж не прошел</b>\n\n"
            "К сожалению, ваш платеж не был завершен.\n"
            "Пожалуйста, попробуйте еще раз или выберите другой способ оплаты."
        )

        await bot.send_message(telegram_id, text, parse_mode="HTML")
        await bot.session.close()

    except Exception as e:
        logger.error(f"❌ Ошибка при уведомлении о неуспешном платеже: {e}")


async def handle_chargeback_payment(transaction):
    """Обработка возврата средств (chargeback)"""
    logger.warning(f"⚠️ Chargeback для транзакции {transaction['transaction_id']}")

    telegram_id = transaction['telegram_id']

    # Здесь можно добавить логику блокировки подписки при chargeback
    # Например, найти подписку пользователя и отключить её

    # Отправляем уведомление администратору
    try:
        from aiogram import Bot
        from config.config import TOKEN, ADMIN_ID

        bot = Bot(token=TOKEN)

        text = (
            "⚠️ <b>CHARGEBACK</b>\n\n"
            f"Пользователь ID: <code>{telegram_id}</code>\n"
            f"Транзакция: {transaction['transaction_id']}\n"
            f"Сумма: {transaction['amount']} {transaction['currency']}\n\n"
            "Требуется ручная проверка!"
        )

        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        await bot.session.close()

    except Exception as e:
        logger.error(f"❌ Ошибка при уведомлении о chargeback: {e}")


# В payment_webhook.py - функция notify_user_success

async def notify_user_success(telegram_id: int, plan: dict, expiry: str,
                            is_renewal: bool, was_expired: bool = False,
                            is_unlimited: bool = False, reset_traffic: bool = False,
                            reset_error: bool = False):
    """Отправить пользователю уведомление об успешной активации"""
    try:
        from aiogram import Bot
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        from config.config import TOKEN
        from datetime import datetime, timedelta

        bot = Bot(token=TOKEN)

        # Форматируем дату
        date_obj = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
        msk_time = date_obj + timedelta(hours=3)
        formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

        traffic_text = "безлимитный" if is_unlimited else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

        if is_renewal:
            if is_unlimited:
                text = (
                    f"✅ <b>Подписка успешно продлена!</b>\n\n"
                    f"📦 Тариф: {plan['tariff_name']}\n"
                    f"➕ Добавлено дней: {plan['days']}\n"
                    f"📊 Лимит трафика: {traffic_text}\n"
                    f"📅 Новая дата: {formatted_date}\n\n"
                    f"💡 <i>Ссылка для подключения осталась прежней.\n"
                    f"Если нужно скопировать её заново, зайдите в 'Мои подписки'</i>"
                )
            else:
                if reset_traffic:
                    text = (
                        f"✅ <b>Подписка продлена!</b>\n\n"
                        f"📦 Тариф: {plan['tariff_name']}\n"
                        f"📅 Новый срок: {plan['days']} дней (до {formatted_date})\n"
                        f"📊 Лимит трафика: {traffic_text}\n\n"
                        f"🔄 Счетчик трафика был сброшен. У вас снова {traffic_text} для использования!\n\n"
                        f"💡 <i>Ссылка для подключения осталась прежней</i>"
                    )
                elif reset_error:
                    text = (
                        f"✅ <b>Подписка продлена!</b>\n\n"
                        f"📦 Тариф: {plan['tariff_name']}\n"
                        f"📅 Новый срок: {plan['days']} дней (до {formatted_date})\n"
                        f"📊 Лимит трафика: {traffic_text}\n\n"
                        f"⚠️ <b>Важно:</b> Произошла ошибка при сбросе счетчика трафика.\n"
                        f"Пожалуйста, обратитесь в поддержку, если у вас возникли проблемы с доступом."
                    )
                else:
                    text = (
                        f"✅ <b>Подписка восстановлена!</b>\n\n"
                        f"📦 Тариф: {plan['tariff_name']}\n"
                        f"📅 Новый срок: {plan['days']} дней (до {formatted_date})\n"
                        f"📊 Лимит трафика: {traffic_text}"
                    )
        else:
            text = (
                f"✅ <b>Подписка успешно активирована!</b>\n\n"
                f"📦 Тариф: {plan['tariff_name']}\n"
                f"📅 Срок: {plan['days']} дней\n"
                f"📊 Лимит трафика: {traffic_text}\n\n"
                f"🔗 Ссылка для подключения будет доступна в разделе 'Мои подписки'\n\n"
                f"💡 <i>Если ссылка не открывается автоматически:\n"
                f"• Скопируйте её\n"
                f"• Откройте Hiddify, v2Ray или другой клиент\n"
                f"• Вставьте ссылку вручную</i>"
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Мои подписки", callback_data="my_subscriptions")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_main")]
        ])

        await bot.send_message(telegram_id, text, reply_markup=keyboard, parse_mode="HTML")
        await bot.session.close()

    except Exception as e:
        logger.error(f"❌ Ошибка при отправке уведомления: {e}")



#на случай если все гига хуево
async def notify_admin_error(user_id: int, error_message: str):
    """
    Отправить уведомление администратору об ошибке
    """
    try:
        from aiogram import Bot
        from config.config import TOKEN, ADMIN_ID

        bot = Bot(token=TOKEN)

        text = (
            f"⚠️ <b>Ошибка при обработке платежа</b>\n\n"
            f"👤 Пользователь ID: <code>{user_id}</code>\n"
            f"❌ Ошибка: {error_message}\n\n"
            f"🔧 Требуется ручная проверка!"
        )

        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
        await bot.session.close()

        logger.info(f"📨 Уведомление об ошибке отправлено администратору")

    except Exception as e:
        logger.error(f"❌ Ошибка при отправке уведомления админу: {e}")