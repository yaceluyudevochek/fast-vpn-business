from aiogram import Router, F
from aiogram.filters import Command
from aiogram.filters import CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import logging
from datetime import datetime, timedelta

from services.panel_api import PanelAPI
from config.config import PANEL_API_URL, PANEL_API_KEY
from database import db as database
from handlers.payment import router as payment_router
from handlers.ui_screens import show_screen
from handlers.ui_emoji import E, BTN, kb_button

router = Router()
logger = logging.getLogger(__name__)
panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)


# ==========================================================
# ГЛАВНОЕ МЕНЮ
# ==========================================================

async def render_main_menu(target, user_id: int, first_name: str):
    user_db = await database.get_user(user_id)

    if user_db and user_db["role"] == "banned":
        await show_screen(
            target,
            "start",
            f"{E['cross']} Ты заблокирован и не можешь пользоваться ботом."
        )
        return

    subscriptions = await database.get_user_subscriptions(user_id)
    active_subs = [s for s in subscriptions if s["status"] == "ACTIVE"]

    if active_subs:
        status_line = f"{E['shield']} У тебя <b>{len(active_subs)}</b> активных подписок."
    else:
        status_line = f"{E['drop']} Пока нет активных подписок — активируй бесплатный триал или оформи подписку."

    text = (
        f"{E['wave']} <b>Привет, {first_name}!</b>\n\n"
        f"{status_line}\n\n"
        f"Выбери действие ниже {E['sparkle']}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("buy", "Купить/продлить", callback_data="show_tariffs", style="primary")],
        [kb_button("subs", "Мои подписки", callback_data="my_subscriptions", style="success")],
        [
            kb_button("referral", "Пригласить друга", callback_data="get_link"),
            kb_button("trial", "Триал", callback_data="activate_trial")
        ],
        [kb_button("info", "Информация", callback_data="info_menu")]
    ])

    await show_screen(target, "start", text, keyboard)


# ==========================================================
# МЕНЮ ИНФОРМАЦИИ
# ==========================================================

from config.config import AGREEMENT_URL, PRIVACY_URL, SUPPORT_URL


@router.callback_query(F.data == "info_menu")
async def show_info_menu(callback: CallbackQuery):
    """Показать меню информации"""

    text = (
        f"{E['info']} <b>Информация</b>\n\n"
        "Используя сервис, ты подтверждаешь, что ознакомлен "
        "с настоящим соглашением и принимаешь его условия в полном объёме.\n\n"
        f"Выбери раздел ниже {E['bulb']}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("doc", "Пользовательское соглашение", url=AGREEMENT_URL)],
        [kb_button("subs", "Политика конфиденциальности", url=PRIVACY_URL)],
        [kb_button("chat", "Техническая поддержка", url=SUPPORT_URL)],
        [kb_button("back", "Назад в меню", callback_data="back_to_main")]
    ])

    await show_screen(callback, "info", text, keyboard)
    await callback.answer()


# ==========================================================
# /start
# ==========================================================

@router.message(Command("start"))
async def start_cmd(message: Message, command: CommandObject):
    user = message.from_user
    referred_by = None

    if command.args and command.args.startswith("ref_"):
        ref_code = command.args[4:]
        referrer = await database.get_user_by_ref_code(ref_code)
        if referrer and referrer["telegram_id"] != user.id:
            referred_by = referrer["telegram_id"]
            logger.info(f"User {user.id} referred by {referred_by}")

    await database.add_user(user.id, user.username, referred_by)

    await render_main_menu(message, user.id, user.first_name)


# ==========================================================
# ПОКАЗ ТАРИФОВ
# ==========================================================
@router.callback_query(F.data == "show_tariffs")
async def show_tariff_types(callback: CallbackQuery):
    tariff_types = await database.get_all_tariff_types()

    if not tariff_types:
        await callback.answer("❌ Тарифы временно недоступны", show_alert=True)
        return

    keyboard = []
    for tt in tariff_types:
        button_text = f"{tt['name']} — {tt['description'] or 'Лимит: ' + str(tt['hwid_limit']) + ' устройств'}"
        keyboard.append([kb_button("tag", button_text, callback_data=f"select_type_{tt['id']}")])

    keyboard.append([kb_button("back", "Назад", callback_data="back_to_main")])

    text = (
        f"{E['tag']} <b>Выбери тип подписки</b>\n\n"
        f"Каждый тип имеет свои тарифные планы {E['sparkle']}"
    )

    await show_screen(callback, "buy", text, InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


# ==========================================================
# ВЫБОР ПЛАНА ДЛЯ ТИПА ТАРИФА
# ==========================================================

@router.callback_query(F.data.startswith("select_type_"))
async def show_plans_for_type(callback: CallbackQuery):
    type_id = int(callback.data.split("_")[2])

    tariff_type = await database.get_tariff_type(type_id)
    if not tariff_type:
        await callback.answer("❌ Тип тарифа не найден", show_alert=True)
        return

    plans = await database.get_tariff_plans(type_id)
    if not plans:
        await callback.answer("❌ Для этого тарифа нет доступных планов", show_alert=True)
        return

    keyboard = []
    for plan in plans:
        button_text = f"{plan['days']} дней — {plan['price']} руб."
        keyboard.append([kb_button("calendar", button_text, callback_data=f"select_plan_{plan['id']}")])

    keyboard.append([kb_button("back", "Назад к типам", callback_data="show_tariffs")])

    text = (
        f"{E['diamond']} <b>{tariff_type['name']}</b>\n\n"
        f"{E['shield']} Лимит устройств: <b>{tariff_type['hwid_limit']}</b>\n"
        f"{tariff_type['description'] or ''}\n\n"
        f"Выбери срок подписки {E['calendar']}"
    )

    await show_screen(callback, "tariffs", text, InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


# ==========================================================
# ВЫБОР ПЛАНА (проверка существующей подписки)
# ==========================================================

@router.callback_query(F.data.startswith("select_plan_"))
async def select_plan(callback: CallbackQuery):
    """Выбор плана - перенаправляем на оплату"""
    plan_id = int(callback.data.split("_")[2])

    # Получаем информацию о плане для отображения
    plan = await database.get_tariff_plan(plan_id)
    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    # Определяем, лимитный это тариф или безлимитный
    is_unlimited = (plan['traffic_limit_bytes'] == 0)

    # Проверяем, есть ли уже активная подписка этого типа
    user_id = callback.from_user.id
    subscriptions = await database.get_user_subscriptions(user_id)
    existing_sub = None
    for sub in subscriptions:
        if sub['tariff_type_id'] == plan['tariff_type_id'] and sub['status'] == 'ACTIVE':
            existing_sub = sub
            break

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("pay", "Оплатить", callback_data=f"pay_plan_{plan_id}")],
        [kb_button("back", "Назад к планам", callback_data=f"select_type_{plan['tariff_type_id']}")]
    ])

    traffic_text = "безлимит" if is_unlimited else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

    text_parts = [
        f"{E['diamond']} <b>{plan['tariff_name']}</b>\n\n",
        f"{E['calendar']} Срок: <b>{plan['days']} дней</b>\n",
        f"{E['money']} Цена: <b>{plan['price']} руб.</b>\n",
        f"{E['chart']} Лимит трафика: <b>{traffic_text}</b>\n",
        f"{E['shield']} Лимит устройств: <b>{plan['hwid_limit']}</b>\n\n"
    ]

    text = "".join(text_parts)

    # Добавляем предупреждение для лимитных тарифов при наличии активной подписки
    if not is_unlimited and existing_sub:
        text += (
            f"{E['warn']} <b>Важно!</b> При покупке этого тарифа:\n"
            "• Весь текущий остаток дней и гигабайтов будет сброшен\n"
            "• Ты получишь новый срок и новый лимит трафика\n"
            "• Счётчик трафика будет обнулён\n\n"
        )

    text += f"Для оформления нажми «Оплатить» {E['sparkle']}"

    await show_screen(callback, "payment", text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("renew_type_"))
async def renew_select_plan(callback: CallbackQuery):
    """Показать доступные планы для продления конкретной подписки"""
    try:
        # Формат: renew_type_{tariff_type_id}_{subscription_id}
        parts = callback.data.split("_")
        tariff_type_id = int(parts[2])
        subscription_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    # Получаем информацию о типе тарифа
    tariff_type = await database.get_tariff_type(tariff_type_id)
    if not tariff_type:
        await callback.answer("❌ Тип тарифа не найден", show_alert=True)
        return

    # Получаем подписку для отображения текущего статуса
    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    # Получаем актуальную информацию из API для форматирования даты
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

    # Форматируем дату окончания
    expiry_date_str = "неизвестно"
    if info and info['formatted_expiry']:
        expiry_date_str = info['formatted_expiry']
    elif subscription['subscription_expiry']:
        try:
            date_obj = datetime.fromisoformat(subscription['subscription_expiry'].replace('Z', '+00:00'))
            msk_time = date_obj + timedelta(hours=3)
            expiry_date_str = msk_time.strftime("%d.%m.%Y %H:%M МСК")
        except:
            expiry_date_str = subscription['subscription_expiry'][:10]

    # Получаем все активные планы для этого типа тарифа
    plans = await database.get_tariff_plans(tariff_type_id)
    if not plans:
        await callback.answer("❌ Нет доступных планов для продления", show_alert=True)
        return

    # Формируем клавиатуру с планами
    keyboard = []
    for plan in plans:
        traffic_text = "безлимит" if plan[
                                         'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

        # Определяем, лимитный это тариф или нет
        is_unlimited = (plan['traffic_limit_bytes'] == 0)

        # Формируем текст кнопки
        button_text = f"{plan['days']} дней — {plan['price']} руб. ({traffic_text})"

        # Добавляем предупреждение для лимитных тарифов
        if not is_unlimited and subscription['status'] == 'ACTIVE':
            button_text += f" {BTN['warn']}"

        keyboard.append([kb_button("calendar", button_text, callback_data=f"renew_plan_{plan['id']}_{subscription_id}")])

    # Кнопка возврата к подписке
    keyboard.append([kb_button("back", "Назад к подписке", callback_data=f"view_sub_{subscription_id}")])

    # Определяем статус для отображения
    status_emoji = E['check'] if info and info['is_active'] else E['cross']
    status_text = "Активна" if info and info['is_active'] else "Неактивна"

    # Формируем текст с предупреждением для лимитных тарифов
    warning_text = ""
    if subscription['status'] == 'ACTIVE':
        # Проверяем, есть ли среди планов лимитные
        has_limited = any(plan['traffic_limit_bytes'] > 0 for plan in plans)
        if has_limited:
            warning_text = (
                f"\n{E['warn']} <i>Тарифы с пометкой {BTN['warn']} имеют лимит трафика.\n"
                "При их выборе текущий остаток дней и гигабайтов будет сброшен!</i>\n"
            )

    text = (
        f"{E['diamond']} <b>Продление подписки: {tariff_type['name']}</b>\n\n"
        f"{status_emoji} Статус: <b>{status_text}</b>\n"
        f"{E['calendar']} Действует до: <b>{expiry_date_str}</b>\n"
        f"{E['shield']} Лимит устройств: <b>{tariff_type['hwid_limit']}</b>\n"
        f"{warning_text}"
        f"\nВыбери срок продления:"
    )

    await show_screen(callback, "tariffs", text, InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


# ==========================================================
# ВЫБОР ТАРИФА (устаревший, не используется активными кнопками)
# ==========================================================

@router.callback_query(F.data.startswith("select_tariff_"))
async def select_tariff(callback: CallbackQuery):
    tariff_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    tariff = await database.get_tariff(tariff_id)
    if not tariff:
        await callback.answer("❌ Тариф не найден", show_alert=True)
        return

    # Проверяем, есть ли уже подписка на этот тариф
    existing_sub = await database.get_user_subscription_by_tariff(user_id, tariff_id)

    # ИСПРАВЛЕНО: правильное форматирование
    text_parts = [
        f"📦 <b>Тариф: {tariff['name']}</b>\n\n",
        f"📅 Срок: {tariff['days']} дней\n",
        f"💰 Цена: {tariff['price']} руб.\n",
        f"📱 Лимит устройств: {tariff['hwid_limit']}\n\n"
    ]

    if existing_sub:
        text_parts.append("У вас уже есть подписка на этот тариф.\n")
        text_parts.append("Хотите продлить её?")
        action = "renew"
    else:
        text_parts.append("Хотите приобрести эту подписку?")
        action = "buy"

    text = "".join(text_parts)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"{action}_tariff_{tariff_id}"
        )],
        [InlineKeyboardButton(text="« Назад к тарифам", callback_data="show_tariffs")]
    ])

    await callback.message.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ==========================================================
# АКТИВАЦИЯ ТРИАЛА (с созданием подписки для реферера)
# ==========================================================

@router.callback_query(F.data == "activate_trial")
async def activate_trial(callback: CallbackQuery):
    user = callback.from_user

    # Проверяем, использовал ли пользователь триал
    subscriptions = await database.get_user_subscriptions(user.id)
    trial_used = any(s.get("trial_used") == 1 for s in subscriptions)

    if trial_used:
        await callback.answer(
            "Вы уже использовали пробный период!",
            show_alert=True
        )
        return

    # Берем тариф с ID=1 (базовый)
    tariff_type = await database.get_tariff_type(1)
    if not tariff_type:
        await callback.answer("❌ Триал временно недоступен", show_alert=True)
        return

    # Берем первый план для этого тарифа
    plans = await database.get_tariff_plans(1)
    if not plans:
        await callback.answer("❌ Триал временно недоступен", show_alert=True)
        return

    plan = plans[0]

    await show_screen(callback, "buy", f"{E['clock']} Активирую пробный период...")

    # Проверяем, есть ли уже базовая подписка (tg{user_id}_1)
    base_username = f"tg{user.id}_1"
    existing_sub = None

    # Ищем среди подписок пользователя базовую (с username = tg{user_id}_1)
    for sub in subscriptions:
        if sub['username_in_panel'] == base_username:
            existing_sub = sub
            break

    if existing_sub:
        # Если базовая подписка уже есть - просто продлеваем на 3 дня
        logger.info(f"Found existing base subscription for user {user.id}, extending by 3 days")

        new_expiry = await panel_api.extend_subscription_by_username(
            base_username,
            3  # Добавляем 3 дня к существующей подписке
        )

        if new_expiry:
            # Обновляем дату в БД
            await database.update_subscription_expiry(existing_sub['id'], new_expiry)

            # Отмечаем, что триал использован (но подписка та же)
            await database.mark_subscription_trial_used(existing_sub['id'])

            # Форматируем дату
            date_obj = datetime.fromisoformat(new_expiry.replace('Z', '+00:00'))
            msk_time = date_obj + timedelta(hours=3)
            formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

            # Получаем ссылку
            user_data = await panel_api.get_user_by_username(base_username)
            link = user_data.get("subscriptionUrl")

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [kb_button("subs", "Мои подписки", callback_data="my_subscriptions")],
                [kb_button("home", "В меню", callback_data="back_to_main")]
            ])

            text = (
                f"{E['check']} <b>Пробный период активирован!</b>\n\n"
                f"{E['calendar']} К текущей подписке добавлено <b>+3 дня</b>\n"
                f"{E['calendar']} Новая дата окончания: <b>{formatted_date}</b>\n"
                f"{E['link']} Ссылка для подключения:\n"
                f"{link}"
            )

            await show_screen(callback, "buy", text, keyboard)
        else:
            await show_screen(callback, "buy", f"{E['cross']} Ошибка при активации пробного периода.")

        # РЕФЕРАЛЬНАЯ НАГРАДА (с созданием подписки, если нужно)
        user_db = await database.get_user(user.id)
        if user_db and user_db["referred_by"]:
            referrer_id = user_db["referred_by"]
            await database.increment_referral_activated(referrer_id)

            # Проверяем, есть ли у реферера базовая подписка
            referrer_subs = await database.get_user_subscriptions(referrer_id)
            referrer_base = None
            for sub in referrer_subs:
                if sub['username_in_panel'] == f"tg{referrer_id}_1":
                    referrer_base = sub
                    break

            if referrer_base:
                # Если есть - продлеваем
                new_expiry = await panel_api.extend_subscription_by_username(
                    referrer_base['username_in_panel'],
                    7
                )
                if new_expiry:
                    await database.update_subscription_expiry(referrer_base['id'], new_expiry)
            else:
                # Если нет - создаем новую подписку на 7 дней
                logger.info(f"Creating base subscription for referrer {referrer_id} with 7 days")

                # Формируем username для реферера
                referrer_username = f"tg{referrer_id}_1"
                referrer_tag = f"U{referrer_id}_T1"
                if len(referrer_tag) > 16:
                    referrer_tag = referrer_tag[:16]

                # Создаем пользователя в панели
                new_referrer_user = await panel_api.create_user(
                    username=referrer_username,
                    telegram_id=referrer_id,
                    squad_id=tariff_type['squad_id'],
                    days=7,
                    hwid_limit=tariff_type['hwid_limit'],
                    tag=referrer_tag
                )

                if new_referrer_user:
                    short_uuid = new_referrer_user.get("shortUuid")
                    expiry = new_referrer_user.get("expireAt")

                    # Сохраняем подписку в БД (без отметки trial)
                    await database.create_subscription(
                        referrer_id,
                        1,
                        plan['id'],
                        short_uuid,
                        expiry,
                        referrer_username,
                        trial=False
                    )

            # Уведомляем реферера
            try:
                await callback.bot.send_message(
                    referrer_id,
                    f"🎉 <b>У вас новый реферал!</b>\n\n"
                    f"Пользователь {user.first_name} активировал пробный период.\n"
                    f"{E['sparkle']} Вам начислено +7 дней подписки!",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to notify referrer {referrer_id}: {e}")

    else:
        # Если базовой подписки нет - создаем новую
        logger.info(f"No base subscription for user {user.id}, creating new trial")

        # Формируем username
        username_in_panel = base_username
        tag = f"U{user.id}_T1"
        if len(tag) > 16:
            tag = tag[:16]

        # Создаем пользователя в панели на 3 дня
        new_user = await panel_api.create_user(
            username=username_in_panel,
            telegram_id=user.id,
            squad_id=tariff_type['squad_id'],
            days=3,
            hwid_limit=tariff_type['hwid_limit'],
            tag=tag
        )

        if new_user:
            short_uuid = new_user.get("shortUuid")
            expiry = new_user.get("expireAt")

            # Сохраняем подписку
            subscription_id = await database.create_subscription(
                user.id,
                1,
                plan['id'],
                short_uuid,
                expiry,
                username_in_panel,
                trial=True
            )

            # РЕФЕРАЛЬНАЯ НАГРАДА (с созданием подписки, если нужно)
            user_db = await database.get_user(user.id)
            if user_db and user_db["referred_by"]:
                referrer_id = user_db["referred_by"]
                await database.increment_referral_activated(referrer_id)

                # Проверяем, есть ли у реферера базовая подписка
                referrer_subs = await database.get_user_subscriptions(referrer_id)
                referrer_base = None
                for sub in referrer_subs:
                    if sub['username_in_panel'] == f"tg{referrer_id}_1":
                        referrer_base = sub
                        break

                if referrer_base:
                    # Если есть - продлеваем
                    new_expiry = await panel_api.extend_subscription_by_username(
                        referrer_base['username_in_panel'],
                        7
                    )
                    if new_expiry:
                        await database.update_subscription_expiry(referrer_base['id'], new_expiry)
                else:
                    # Если нет - создаем новую подписку на 7 дней
                    logger.info(f"Creating base subscription for referrer {referrer_id} with 7 days")

                    # Формируем username для реферера
                    referrer_username = f"tg{referrer_id}_1"
                    referrer_tag = f"U{referrer_id}_T1"
                    if len(referrer_tag) > 16:
                        referrer_tag = referrer_tag[:16]

                    # Создаем пользователя в панели
                    new_referrer_user = await panel_api.create_user(
                        username=referrer_username,
                        telegram_id=referrer_id,
                        squad_id=tariff_type['squad_id'],
                        days=7,
                        hwid_limit=tariff_type['hwid_limit'],
                        tag=referrer_tag
                    )

                    if new_referrer_user:
                        referrer_short_uuid = new_referrer_user.get("shortUuid")
                        referrer_expiry = new_referrer_user.get("expireAt")

                        # Сохраняем подписку в БД (без отметки trial)
                        await database.create_subscription(
                            referrer_id,
                            1,
                            plan['id'],
                            referrer_short_uuid,
                            referrer_expiry,
                            referrer_username,
                            trial=False
                        )

                # Уведомляем реферера
                try:
                    await callback.bot.send_message(
                        referrer_id,
                        f"🎉 <b>У вас новый реферал!</b>\n\n"
                        f"Пользователь {user.first_name} активировал пробный период.\n"
                        f"{E['sparkle']} Вам начислено +7 дней подписки!",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify referrer {referrer_id}: {e}")

            link = new_user.get("subscriptionUrl")

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [kb_button("subs", "Мои подписки", callback_data="my_subscriptions")],
                [kb_button("home", "В меню", callback_data="back_to_main")]
            ])

            text = (
                f"{E['check']} <b>Пробный период активирован!</b>\n\n"
                f"{E['calendar']} Срок: <b>3 дня</b>\n"
                f"{E['link']} Ссылка для подключения:\n"
                f"{link}"
            )

            await show_screen(callback, "buy", text, keyboard)
        else:
            await show_screen(callback, "buy", f"{E['cross']} Ошибка при активации пробного периода.")


# ==========================================================
# МОИ ПОДПИСКИ
# ==========================================================

@router.callback_query(F.data == "my_subscriptions")
async def my_subscriptions(callback: CallbackQuery):
    user_id = callback.from_user.id

    subscriptions = await database.get_user_subscriptions(user_id)

    if not subscriptions:
        text = (
            f"{E['drop']} У тебя пока нет подписок.\n\n"
            f"Активируй триал или оформи подписку {E['gift']}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [kb_button("back", "Назад", callback_data="back_to_main")]
        ])
        await show_screen(callback, "subs", text, keyboard)
        await callback.answer()
        return

    keyboard = []
    for sub in subscriptions:
        # Получаем актуальную информацию из API
        info = await panel_api.format_subscription_info_by_short_uuid(sub['short_uuid'])
        status_icon = "check" if info and info['is_active'] else "cross"

        keyboard.append([kb_button(status_icon, sub['tariff_name'], callback_data=f"view_sub_{sub['id']}")])

    keyboard.append([kb_button("back", "Назад", callback_data="back_to_main")])

    text = (
        f"{E['shield']} <b>Твои подписки</b>\n\n"
        f"Выбери подписку для управления {E['sparkle']}"
    )

    await show_screen(callback, "subs", text, InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


# ==========================================================
# ПРОСМОТР КОНКРЕТНОЙ ПОДПИСКИ
# ==========================================================

@router.callback_query(F.data.startswith("view_sub_"))
async def view_subscription(callback: CallbackQuery):
    subscription_id = int(callback.data.split("_")[2])

    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    # Получаем актуальную информацию из API
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

    # Получаем ссылку на подписку
    user_data = await panel_api.get_user_by_username(subscription['username_in_panel'])
    link = None
    if user_data:
        link = user_data.get("subscriptionUrl")

    if info and info['is_active']:
        status_text = f"{E['check']} Активна до: <b>{info['formatted_expiry']}</b>"
    else:
        status_text = f"{E['cross']} Подписка неактивна или истекла"

    # Получаем количество устройств
    devices_count = await panel_api.get_user_hwid_devices_count_by_username(
        subscription['username_in_panel']
    )

    # Получаем лимит трафика из плана
    plan = await database.get_tariff_plan(subscription['tariff_plan_id'])
    traffic_text = "безлимит" if plan and plan[
        'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB" if plan else "неизвестно"

    text_parts = [
        f"{E['diamond']} <b>Подписка: {subscription['tariff_name']}</b>\n\n",
        f"{status_text}\n",
        f"{E['shield']} Устройств: <b>{devices_count} / {subscription['tariff_hwid_limit']}</b>\n",
        f"{E['chart']} Лимит трафика: <b>{traffic_text}</b>\n"
    ]

    text = "".join(text_parts)

    if link:
        text += (
            f"\n{E['link']} <b>Ссылка для подключения:</b>\n"
            f"{link}\n\n"
            f"{E['bulb']} <i>Не открылась автоматически? Скопируй ссылку и вставь "
            "вручную в приложение Happ, v2Ray или v2Box.</i>"
        )

    keyboard = []

    if info and info['is_active']:
        keyboard.append([kb_button("reset", "Сбросить лимит устройств", callback_data=f"reset_devices_{subscription_id}")])

    keyboard.append([kb_button(
        "buy", "Продлить подписку",
        callback_data=f"renew_type_{subscription['tariff_type_id']}_{subscription_id}"
    )])

    keyboard.append([kb_button("back", "Назад к списку", callback_data="my_subscriptions")])

    await show_screen(callback, "subs", text, InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()


# ==========================================================
# ВЫБОР ПЛАНА ДЛЯ ПРОДЛЕНИЯ ПОДПИСКИ
# ==========================================================

@router.callback_query(F.data.startswith("renew_plan_"))
async def renew_plan(callback: CallbackQuery):
    """Обработка выбора плана для продления"""
    try:
        # Формат: renew_plan_{plan_id}_{subscription_id}
        parts = callback.data.split("_")
        plan_id = int(parts[2])
        subscription_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    # Получаем информацию о плане
    plan = await database.get_tariff_plan(plan_id)
    if not plan:
        await callback.answer("❌ План не найден", show_alert=True)
        return

    # Получаем информацию о подписке
    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    # Проверяем, принадлежит ли подписка пользователю
    if subscription['telegram_id'] != callback.from_user.id:
        await callback.answer("❌ Это не ваша подписка", show_alert=True)
        return

    # Получаем актуальную информацию из API для отображения даты
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

    # Форматируем дату окончания
    expiry_date_str = "неизвестно"
    if info and info['formatted_expiry']:
        expiry_date_str = info['formatted_expiry']
    elif subscription['subscription_expiry']:
        try:
            date_obj = datetime.fromisoformat(subscription['subscription_expiry'].replace('Z', '+00:00'))
            msk_time = date_obj + timedelta(hours=3)
            expiry_date_str = msk_time.strftime("%d.%m.%Y %H:%M МСК")
        except:
            expiry_date_str = subscription['subscription_expiry'][:10]

    # Определяем, лимитный это тариф или безлимитный
    is_unlimited = (plan['traffic_limit_bytes'] == 0)

    traffic_gb = int(plan['traffic_limit_bytes'] / (1024 ** 3)) if not is_unlimited else 0

    # Формируем текст с предупреждением для лимитных тарифов
    text_parts = [
        f"{E['diamond']} <b>Продление подписки</b>\n\n",
        f"{E['diamond']} Тариф: <b>{plan['tariff_name']}</b>\n",
        f"{E['calendar']} Срок: <b>{plan['days']} дней</b>\n",
        f"{E['money']} Цена: <b>{plan['price']} руб.</b>\n",
        f"{E['chart']} Лимит трафика: <b>{'безлимит' if is_unlimited else f'{traffic_gb} GB'}</b>\n\n"
    ]

    text = "".join(text_parts)

    if not is_unlimited and subscription['status'] == 'ACTIVE':
        text += (
            f"{E['warn']} <b>Внимание!</b> При продлении на этот тариф:\n"
            "• Весь текущий остаток дней будет сброшен\n"
            "• Ты получишь новый срок и новый лимит трафика\n"
            "• Счётчик трафика будет обнулён\n\n"
        )

    text += "Продолжить?"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("confirm", "Подтвердить", callback_data=f"pay_plan_{plan_id}")],
        [kb_button("back", "Назад к выбору планов", callback_data=f"renew_type_{plan['tariff_type_id']}_{subscription_id}")]
    ])

    await show_screen(callback, "payment", text, keyboard)
    await callback.answer()


# ==========================================================
# СБРОС УСТРОЙСТВ ДЛЯ КОНКРЕТНОЙ ПОДПИСКИ
# ==========================================================

@router.callback_query(F.data.startswith("reset_devices_"))
async def reset_devices(callback: CallbackQuery):
    subscription_id = int(callback.data.split("_")[2])

    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    # Проверяем, можно ли сбросить
    can_reset, message = await database.can_reset_devices(subscription_id)

    if not can_reset:
        await callback.answer(message, show_alert=True)
        return

    # Запрашиваем подтверждение
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            kb_button("confirm", "Да, сбросить", callback_data=f"confirm_reset_{subscription_id}"),
            kb_button("cancel", "Отмена", callback_data=f"view_sub_{subscription_id}")
        ]
    ])

    text = (
        f"{E['warn']} <b>Подтверди сброс устройств</b>\n\n"
        f"Подписка: <b>{subscription['tariff_name']}</b>\n\n"
        "Ты уверен, что хочешь сбросить список подключённых устройств?\n"
        "Это действие отключит все текущие устройства и позволит подключиться с новых.\n\n"
        f"{E['clock']} Сброс доступен <b>1 раз в сутки</b>."
    )

    await show_screen(callback, "subs", text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_reset_"))
async def confirm_reset_devices(callback: CallbackQuery):
    subscription_id = int(callback.data.split("_")[2])

    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    # Еще раз проверяем
    can_reset, message = await database.can_reset_devices(subscription_id)
    if not can_reset:
        await callback.answer(message, show_alert=True)
        return

    await show_screen(callback, "subs", f"{E['clock']} Выполняю сброс устройств...")

    # Выполняем сброс
    success = await panel_api.reset_user_hwid_devices_by_username(
        subscription['username_in_panel']
    )

    if success:
        # Записываем время сброса
        await database.record_device_reset(subscription_id)
        reset_info = await database.get_last_reset_info(subscription_id)

        text = (
            f"{E['check']} <b>Устройства успешно сброшены!</b>\n\n"
            f"{E['diamond']} Подписка: <b>{subscription['tariff_name']}</b>\n"
            f"{E['chart']} Всего выполнено сбросов: <b>{reset_info['reset_count']}</b>"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [kb_button("back", "Назад к подписке", callback_data=f"view_sub_{subscription_id}")]
        ])

        await show_screen(callback, "subs", text, keyboard)
    else:
        await show_screen(
            callback,
            "subs",
            f"{E['cross']} Ошибка при сбросе устройств.",
            InlineKeyboardMarkup(inline_keyboard=[
                [kb_button("back", "Назад", callback_data=f"view_sub_{subscription_id}")]
            ])
        )
    await callback.answer()


# ==========================================================
# РЕФЕРАЛЬНАЯ ССЫЛКА
# ==========================================================

@router.callback_query(F.data == "get_link")
async def get_link(callback: CallbackQuery):
    user = callback.from_user
    user_db = await database.get_user(user.id)

    if not user_db or not user_db["ref_code"]:
        await callback.answer("❌ Ошибка получения реферального кода", show_alert=True)
        return

    bot_username = (await callback.bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_db['ref_code']}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("back", "Назад", callback_data="back_to_main")]
    ])

    stats_text = (
        f"{E['link']} <b>Твоя реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"{E['gift']} <b>Бонус:</b> за каждого друга, активировавшего триал,\n"
        f"ты получаешь <b>+7 дней</b> к подписке!\n\n"
        f"{E['people']} Активировали триал: <b>{user_db['referrals_activated']}</b> чел."
    )

    await show_screen(callback, "referral", stats_text, keyboard)
    await callback.answer()


# ==========================================================
# КОМАНДА /ref
# ==========================================================

@router.message(Command("ref"))
async def ref_command(message: Message):
    user = message.from_user
    user_db = await database.get_user(user.id)

    if not user_db or not user_db["ref_code"]:
        await message.answer("❌ Ошибка получения реферального кода")
        return

    bot_username = (await message.bot.me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_db['ref_code']}"

    text = (
        f"{E['link']} <b>Твоя реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"{E['gift']} Активировали триал: <b>{user_db['referrals_activated']}</b> чел."
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [kb_button("home", "В меню", callback_data="back_to_main")]
    ])

    await show_screen(message, "referral", text, keyboard)


# ==========================================================
# НАЗАД В ГЛАВНОЕ МЕНЮ
# ==========================================================

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await render_main_menu(
        callback,
        callback.from_user.id,
        callback.from_user.first_name
    )
    await callback.answer()
