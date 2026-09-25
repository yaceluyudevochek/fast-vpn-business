import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
import aiosqlite

from aiogram import Bot
from database import db
from handlers.ui_emoji import E

logger = logging.getLogger(__name__)


class SubscriptionReminder:
    def __init__(self, bot: Bot, check_interval: int = 3600):
        self.bot = bot
        self.check_interval = check_interval
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        # Храним информацию об отправленных уведомлениях
        self._sent_notifications: Dict[str, Dict] = {}

    async def start(self):
        if self.is_running:
            return

        self.is_running = True
        self._task = asyncio.create_task(self._check_subscriptions_loop())
        logger.info("🚀 Subscription reminder service started")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 Subscription reminder service stopped")

    async def _check_subscriptions_loop(self):
        while self.is_running:
            try:
                await self._check_all_subscriptions()
            except Exception as e:
                logger.error(f"Error in subscription check loop: {e}", exc_info=True)
            await asyncio.sleep(self.check_interval)

    async def _check_all_subscriptions(self):
        """Проверка всех подписок"""
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("""
                SELECT s.id, s.telegram_id, s.subscription_expiry, s.status,
                       tt.name as tariff_name
                FROM subscriptions s
                JOIN tariff_types tt ON s.tariff_type_id = tt.id
                WHERE s.status != 'DISABLED'
            """) as cursor:
                subscriptions = await cursor.fetchall()

        for sub in subscriptions:
            try:
                await self._check_subscription(dict(sub))
            except Exception as e:
                logger.error(f"Error checking subscription {sub['id']}: {e}")
            await asyncio.sleep(0.05)

    async def _check_subscription(self, subscription: dict):
        """Проверка конкретной подписки"""
        sub_id = subscription['id']
        telegram_id = subscription['telegram_id']
        expiry_str = subscription['subscription_expiry']
        current_status = subscription['status']
        tariff_name = subscription['tariff_name']

        if not expiry_str:
            return

        try:
            # Парсим дату окончания
            expiry_date = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
            now = datetime.now(expiry_date.tzinfo)

            time_left = expiry_date - now
            hours_left = time_left.total_seconds() / 3600
            is_active = hours_left > 0

            # Ключ для отслеживания уведомлений по этой подписке
            notif_key = f"{telegram_id}_{sub_id}"

            # ======================================================
            # ЛОГИКА ДЛЯ АКТИВНЫХ ПОДПИСОК (напоминания)
            # ======================================================
            if is_active:
                # Уведомление за 24 часа (один раз)
                if 23 <= hours_left <= 25:
                    if not self._was_notification_sent(notif_key, '24h'):
                        await self._send_24h_reminder(telegram_id, expiry_date, tariff_name)
                        self._mark_notification_sent(notif_key, '24h')

                # Уведомление за 1 час (один раз)
                elif 0.9 <= hours_left <= 1.1:
                    if not self._was_notification_sent(notif_key, '1h'):
                        await self._send_1h_reminder(telegram_id, expiry_date, tariff_name)
                        self._mark_notification_sent(notif_key, '1h')

                # Уведомление за 10 минут (один раз)
                elif 0.15 <= hours_left <= 0.18:
                    if not self._was_notification_sent(notif_key, '10min'):
                        await self._send_10min_reminder(telegram_id, expiry_date, tariff_name)
                        self._mark_notification_sent(notif_key, '10min')

            # ======================================================
            # ЛОГИКА ДЛЯ ИСТЕКШИХ ПОДПИСОК (ТОЛЬКО ОДИН РАЗ)
            # ======================================================
            else:
                # Подписка истекла - отправляем уведомление только один раз.
                # Статус EXPIRED в БД персистентен и выставляется в тот же момент,
                # что и отправка уведомления, поэтому он сам по себе надёжный
                # признак "уже уведомляли" - в отличие от self._sent_notifications,
                # которое живёт только в памяти процесса и обнуляется при рестарте бота.
                if current_status != 'EXPIRED':
                    await self._send_expired_reminder(telegram_id, expiry_date, tariff_name)
                    self._mark_notification_sent(notif_key, 'expired')

                    # Обновляем статус в БД
                    await db.update_subscription_status(sub_id, 'EXPIRED')
                    logger.info(f"📝 Статус подписки {sub_id} изменен на EXPIRED")

            # Очищаем старые записи (раз в день)
            if now.hour == 3 and now.minute < 10:  # В 3 часа ночи
                self._cleanup_old_notifications()

        except Exception as e:
            logger.error(f"Error processing subscription {sub_id}: {e}")

    def _was_notification_sent(self, key: str, notif_type: str) -> bool:
        """Проверяем, отправляли ли уже такое уведомление"""
        if key not in self._sent_notifications:
            return False
        if notif_type not in self._sent_notifications[key]:
            return False

        sent_data = self._sent_notifications[key][notif_type]
        sent_time = sent_data['time']

        # Для активных уведомлений (24h, 1h, 10min) - не отправлять повторно
        if notif_type in ['24h', '1h', '10min']:
            # Если прошло больше 48 часов, можно отправить снова (на всякий случай)
            return (datetime.now() - sent_time).total_seconds() < 48 * 3600

        # Для уведомления об истечении - никогда не отправлять повторно
        elif notif_type == 'expired':
            return True  # Раз отправили - больше никогда

        return False

    def _mark_notification_sent(self, key: str, notif_type: str):
        """Отмечаем, что уведомление отправлено"""
        if key not in self._sent_notifications:
            self._sent_notifications[key] = {}

        self._sent_notifications[key][notif_type] = {
            'time': datetime.now(),
            'type': notif_type
        }

        logger.info(f"✅ Уведомление {notif_type} для {key} отмечено как отправленное")

    def _cleanup_old_notifications(self):
        """Очищаем старые записи (старше 30 дней)"""
        now = datetime.now()
        keys_to_remove = []

        for key, notifications in self._sent_notifications.items():
            types_to_remove = []
            for notif_type, data in notifications.items():
                # Удаляем записи старше 30 дней
                if (now - data['time']).days > 30:
                    types_to_remove.append(notif_type)

            for notif_type in types_to_remove:
                del notifications[notif_type]

            if not notifications:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._sent_notifications[key]

        if keys_to_remove or types_to_remove:
            logger.info(f"🧹 Очищено {len(keys_to_remove)} ключей и {len(types_to_remove)} типов уведомлений")

    # ======================================================
    # ОТПРАВКА УВЕДОМЛЕНИЙ
    # ======================================================

    async def _send_24h_reminder(self, telegram_id: int, expiry_date: datetime, tariff_name: str):
        """Уведомление за 24 часа"""
        try:
            msk_time = expiry_date + timedelta(hours=3)
            formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

            text = (
                "⚠️ <b>Внимание!</b>\n\n"
                f"Подписка <b>{tariff_name}</b> истекает <b>через 1 день</b>.\n"
                f"📅 Дата окончания: {formatted_date}\n\n"
                "Чтобы продолжить пользоваться VPN, пожалуйста, продлите подписку.\n\n"
                "💎 Нажмите /start и выберите 'Мои подписки' для продления."
            )

            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
            logger.info(f"✅ Sent 24h reminder to user {telegram_id} for {tariff_name}")
        except Exception as e:
            logger.error(f"❌ Failed to send 24h reminder: {e}")

    async def _send_1h_reminder(self, telegram_id: int, expiry_date: datetime, tariff_name: str):
        """Уведомление за 1 час"""
        try:
            msk_time = expiry_date + timedelta(hours=3)
            formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

            text = (
                "⏰ <b>Срочное уведомление!</b>\n\n"
                f"Подписка <b>{tariff_name}</b> истекает <b>через 1 час</b>!\n"
                f"📅 Дата окончания: {formatted_date}\n\n"
                "🚀 Продлите подписку сейчас, чтобы не потерять доступ к VPN."
            )

            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
            logger.info(f"✅ Sent 1h reminder to user {telegram_id} for {tariff_name}")
        except Exception as e:
            logger.error(f"❌ Failed to send 1h reminder: {e}")

    async def _send_10min_reminder(self, telegram_id: int, expiry_date: datetime, tariff_name: str):
        """Уведомление за 10 минут"""
        try:
            msk_time = expiry_date + timedelta(hours=3)
            formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

            text = (
                "🔥 <b>Последнее напоминание!</b>\n\n"
                f"Подписка <b>{tariff_name}</b> истекает <b>через 10 минут</b>!\n"
                f"📅 Дата окончания: {formatted_date}\n\n"
                "💎 Продлите подписку прямо сейчас, чтобы сохранить доступ к VPN."
            )

            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
            logger.info(f"✅ Sent 10min reminder to user {telegram_id} for {tariff_name}")
        except Exception as e:
            logger.error(f"❌ Failed to send 10min reminder: {e}")

    async def _send_expired_reminder(self, telegram_id: int, expiry_date: datetime, tariff_name: str):
        """Уведомление об истечении подписки (ТОЛЬКО ОДИН РАЗ)"""
        try:
            msk_time = expiry_date + timedelta(hours=3)
            formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

            text = (
                f"{E['cross']} <b>Подписка истекла</b>\n\n"
                f"Срок действия подписки <b>{tariff_name}</b> истек {formatted_date}.\n\n"
                f"{E['diamond']} Для восстановления доступа к VPN, пожалуйста, продлите подписку.\n\n"
                "👉 Нажмите /start и выберите 'Мои подписки'"
            )

            await self.bot.send_message(telegram_id, text, parse_mode="HTML")
            logger.info(f"✅ Sent expired notification to user {telegram_id} for {tariff_name}")
        except Exception as e:
            logger.error(f"❌ Failed to send expired notification: {e}")