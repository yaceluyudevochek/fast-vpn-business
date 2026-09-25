from datetime import datetime, timedelta

import aiosqlite
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import logging

from database import db as database
from database.db import get_all_users, DB_PATH
from services.panel_api import PanelAPI
from config.config import PANEL_API_URL, PANEL_API_KEY, ADMIN_ID

router = Router()
panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)
logger = logging.getLogger(__name__)


# ======================================================
# FSM СОСТОЯНИЯ
# ======================================================

class AddDays(StatesGroup):
    user_id = State()
    subscription_id = State()
    days = State()


class AddAdmin(StatesGroup):
    telegram_id = State()


class BanUser(StatesGroup):
    telegram_id = State()


class UnbanUser(StatesGroup):
    telegram_id = State()


class Mailing(StatesGroup):
    content = State()
    confirm = State()


# FSM для создания типа тарифа
class CreateTariffType(StatesGroup):
    name = State()
    squad_id = State()
    hwid_limit = State()
    description = State()


# FSM для создания плана
class CreateTariffPlan(StatesGroup):
    type_id = State()
    days = State()
    price = State()
    traffic_limit = State()  # новый шаг


# FSM для редактирования
class EditTariffField(StatesGroup):
    entity_type = State()  # 'type' или 'plan'
    entity_id = State()
    field = State()
    value = State()

# ======================================================
# FSM ДЛЯ УМЕНЬШЕНИЯ ДНЕЙ
# ======================================================

class ReduceDays(StatesGroup):
    user_id = State()
    subscription_id = State()
    days = State()
    confirm = State()


# ======================================================
# АДМИН-МЕНЮ
# ======================================================

@router.message(Command("admin"))
async def admin_menu(message: Message):
    if not await database.is_admin(message.from_user.id):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Управление тарифами", callback_data="admin_tariffs")],
        [InlineKeyboardButton(text="➕ Добавить дни подписки", callback_data="admin_add_days")],
        [InlineKeyboardButton(text="➖ Уменьшить дни подписки", callback_data="admin_reduce_days")],
        [InlineKeyboardButton(text="💾 Управление бэкапами", callback_data="admin_backup")],
        [InlineKeyboardButton(text="👑 Добавить администратора", callback_data="admin_add_admin")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="🚫 Забанить пользователя", callback_data="admin_ban_user")],
        [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban_user")],
        [InlineKeyboardButton(text="📋 Список администраторов", callback_data="admin_list")]
    ])

    await message.answer(
        "🛠 <b>Админ-панель</b>\nВыберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ======================================================
# УПРАВЛЕНИЕ ТАРИФАМИ (ГЛАВНОЕ МЕНЮ)
# ======================================================

@router.callback_query(F.data == "admin_tariffs")
async def admin_tariffs(callback: CallbackQuery):
    """Главное меню управления тарифами"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список типов тарифов", callback_data="admin_list_types")],
        [InlineKeyboardButton(text="➕ Создать новый тип", callback_data="admin_create_type")],
        [InlineKeyboardButton(text="📋 Список планов", callback_data="admin_list_plans")],
        [InlineKeyboardButton(text="« Назад в админ-меню", callback_data="admin_back")]
    ])

    await callback.message.edit_text(
        "📦 <b>Управление тарифами</b>\n\n"
        "Выберите действие:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ======================================================
# УПРАВЛЕНИЕ ТИПАМИ ТАРИФОВ (tariff_types)
# ======================================================

@router.callback_query(F.data == "admin_list_types")
async def admin_list_types(callback: CallbackQuery):
    """Список всех типов тарифов"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    types = await database.get_all_tariff_types(active_only=False)

    if not types:
        await callback.message.edit_text(
            "📋 <b>Типы тарифов</b>\n\n"
            "Список пуст",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="admin_tariffs")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    text = "📋 <b>Типы тарифов:</b>\n\n"
    keyboard = []

    for tt in types:
        status = "✅" if tt['is_active'] else "❌"
        text += f"{status} <b>{tt['name']}</b> (ID: {tt['id']})\n"
        text += f"   🆔 Squad: <code>{tt['squad_id']}</code>\n"
        text += f"   📱 Лимит: {tt['hwid_limit']}\n"
        if tt['description']:
            text += f"   📝 {tt['description']}\n"
        text += "\n"

        keyboard.append([InlineKeyboardButton(
            text=f"✏️ {tt['name']}",
            callback_data=f"admin_edit_type_{tt['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="« Назад", callback_data="admin_tariffs")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_type_"))
async def admin_edit_type(callback: CallbackQuery):
    """Редактирование конкретного типа тарифа"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        type_id = int(callback.data.split("_")[3])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    tariff_type = await database.get_tariff_type(type_id)
    if not tariff_type:
        await callback.answer("❌ Тип не найден", show_alert=True)
        return

    text = (
        f"✏️ <b>Редактирование типа тарифа</b>\n\n"
        f"ID: {tariff_type['id']}\n"
        f"Название: {tariff_type['name']}\n"
        f"Squad ID: {tariff_type['squad_id']}\n"
        f"Лимит устройств: {tariff_type['hwid_limit']}\n"
        f"Описание: {tariff_type['description'] or 'нет'}\n"
        f"Статус: {'✅ Активен' if tariff_type['is_active'] else '❌ Неактивен'}\n\n"
        f"Что хотите изменить?"
    )

    keyboard = [
        [InlineKeyboardButton(text="📝 Название", callback_data=f"edit_type_field_{type_id}_name")],
        [InlineKeyboardButton(text="🆔 Squad ID", callback_data=f"edit_type_field_{type_id}_squad_id")],
        [InlineKeyboardButton(text="📱 Лимит устройств", callback_data=f"edit_type_field_{type_id}_hwid_limit")],
        [InlineKeyboardButton(text="📄 Описание", callback_data=f"edit_type_field_{type_id}_description")],
        [InlineKeyboardButton(
            text="🔄 Переключить статус",
            callback_data=f"toggle_type_{type_id}"
        )],
        [InlineKeyboardButton(text="« Назад к списку", callback_data="admin_list_types")]
    ]

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_type_field_"))
async def edit_type_field(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования поля типа тарифа"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        parts = callback.data.split("_")
        # format: edit_type_field_{type_id}_{field}
        # Например: edit_type_field_1_name или edit_type_field_1_squad_id или edit_type_field_1_hwid_limit
        if len(parts) < 5:
            await callback.answer("❌ Неверный формат данных", show_alert=True)
            return

        type_id = int(parts[3])

        # Собираем поле обратно, так как оно может состоять из нескольких частей
        field_parts = parts[4:]
        field = "_".join(field_parts)

        logger.info(f"Editing type field: type_id={type_id}, field={field}")

    except (IndexError, ValueError) as e:
        logger.error(f"Error parsing callback data: {e}")
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    field_names = {
        "name": "название",
        "squad_id": "Squad ID",
        "hwid_limit": "лимит устройств",
        "description": "описание"
    }

    if field not in field_names:
        logger.error(f"Unknown field: {field}")
        await callback.answer(f"❌ Неизвестное поле: {field}", show_alert=True)
        return

    await state.set_state(EditTariffField.value)
    await state.update_data(
        entity_type='type',
        entity_id=type_id,
        field=field
    )

    # Отправляем новое сообщение вместо редактирования
    await callback.message.answer(
        f"Введите новое <b>{field_names[field]}</b> для типа тарифа:",
        parse_mode="HTML"
    )

    # Просто отвечаем на callback, не редактируя сообщение
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_type_"))
async def toggle_type_status(callback: CallbackQuery):
    """Переключение статуса типа тарифа"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        type_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    tariff_type = await database.get_tariff_type(type_id)
    if not tariff_type:
        await callback.answer("❌ Тип не найден", show_alert=True)
        return

    new_status = 0 if tariff_type['is_active'] else 1

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tariff_types SET is_active = ? WHERE id = ?",
            (new_status, type_id)
        )
        await db.commit()

    await callback.answer(f"Статус изменен на {'активен' if new_status else 'неактивен'}")

    # Получаем обновленные данные
    updated_type = await database.get_tariff_type(type_id)

    if updated_type:
        text = (
            f"✏️ <b>Редактирование типа тарифа</b>\n\n"
            f"ID: {updated_type['id']}\n"
            f"Название: {updated_type['name']}\n"
            f"Squad ID: {updated_type['squad_id']}\n"
            f"Лимит устройств: {updated_type['hwid_limit']}\n"
            f"Описание: {updated_type['description'] or 'нет'}\n"
            f"Статус: {'✅ Активен' if updated_type['is_active'] else '❌ Неактивен'}\n\n"
            f"Что хотите изменить?"
        )

        keyboard = [
            [InlineKeyboardButton(text="📝 Название", callback_data=f"edit_type_field_{type_id}_name")],
            [InlineKeyboardButton(text="🆔 Squad ID", callback_data=f"edit_type_field_{type_id}_squad_id")],
            [InlineKeyboardButton(text="📱 Лимит устройств", callback_data=f"edit_type_field_{type_id}_hwid_limit")],
            [InlineKeyboardButton(text="📄 Описание", callback_data=f"edit_type_field_{type_id}_description")],
            [InlineKeyboardButton(
                text="🔄 Переключить статус",
                callback_data=f"toggle_type_{type_id}"
            )],
            [InlineKeyboardButton(text="« Назад к списку", callback_data="admin_list_types")]
        ]

        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )

        # Удаляем старое сообщение
        try:
            await callback.message.delete()
        except:
            pass


# ======================================================
# УПРАВЛЕНИЕ ПЛАНАМИ (tariff_plans)
# ======================================================

@router.callback_query(F.data == "admin_list_plans")
async def admin_list_plans(callback: CallbackQuery):
    """Список всех планов с фильтром по типу"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    types = await database.get_all_tariff_types(active_only=False)

    if not types:
        await callback.message.edit_text(
            "📋 <b>Планы тарифов</b>\n\n"
            "Сначала создайте хотя бы один тип тарифа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="admin_tariffs")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    keyboard = []
    for tt in types:
        keyboard.append([InlineKeyboardButton(
            text=f"📊 {tt['name']}",
            callback_data=f"admin_plans_for_type_{tt['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="« Назад", callback_data="admin_tariffs")])

    await callback.message.edit_text(
        "📋 <b>Выберите тип тарифа для просмотра планов:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_plans_for_type_"))
async def admin_plans_for_type(callback: CallbackQuery):
    """Показать планы для конкретного типа"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        type_id = int(callback.data.split("_")[4])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    tariff_type = await database.get_tariff_type(type_id)
    if not tariff_type:
        await callback.answer("❌ Тип не найден", show_alert=True)
        return

    # Получаем ВСЕ планы, включая неактивные
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT *
            FROM tariff_plans
            WHERE tariff_type_id = ?
            ORDER BY days
        """, (type_id,)) as cursor:
            rows = await cursor.fetchall()
            plans = [dict(row) for row in rows]

    text = f"📋 <b>Планы для типа {tariff_type['name']}:</b>\n\n"

    if not plans:
        text += "Нет доступных планов."
    else:
        for plan in plans:
            status_emoji = "✅" if plan['is_active'] else "❌"
            traffic_text = "безлимит" if plan['traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024**3):.0f} GB"
            text += f"{status_emoji} <b>{plan['days']} дней</b> — {plan['price']} руб.\n"
            text += f"   📊 Трафик: {traffic_text}\n"
            text += f"   ID: {plan['id']}\n\n"

    keyboard = [
        [InlineKeyboardButton(
            text="➕ Добавить план",
            callback_data=f"admin_create_plan_for_{type_id}"
        )]
    ]

    # Добавляем кнопки для редактирования каждого плана
    for plan in plans:
        status = "✅" if plan['is_active'] else "❌"
        traffic_text = "безлимит" if plan['traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024**3):.0f}GB"
        keyboard.insert(0, [InlineKeyboardButton(
            text=f"{status} {plan['days']} дн. - {plan['price']} руб. ({traffic_text})",
            callback_data=f"admin_edit_plan_{plan['id']}"
        )])

    keyboard.append([InlineKeyboardButton(text="« Назад к типам", callback_data="admin_list_plans")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_edit_plan_"))
async def admin_edit_plan(callback: CallbackQuery):
    """Редактирование конкретного плана"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        plan_id = int(callback.data.split("_")[3])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tp.*, tt.name as type_name, tt.id as type_id
            FROM tariff_plans tp
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE tp.id = ?
        """, (plan_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.answer("❌ План не найден", show_alert=True)
                return
            plan = dict(row)

    traffic_text = "безлимит" if plan[
                                     'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

    text = (
        f"✏️ <b>Редактирование плана</b>\n\n"
        f"Тип: {plan['type_name']}\n"
        f"ID плана: {plan['id']}\n"
        f"Дней: {plan['days']}\n"
        f"Цена: {plan['price']} руб.\n"
        f"📊 Лимит трафика: {traffic_text}\n"
        f"Статус: {'✅ Активен' if plan['is_active'] else '❌ Неактивен'}\n\n"
        f"Что хотите изменить?"
    )

    keyboard = [
        [InlineKeyboardButton(text="📅 Количество дней", callback_data=f"edit_plan_field_{plan_id}_days")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_plan_field_{plan_id}_price")],
        [InlineKeyboardButton(text="📊 Лимит трафика", callback_data=f"edit_plan_field_{plan_id}_traffic_limit_bytes")],
        [InlineKeyboardButton(
            text="🔄 Переключить статус",
            callback_data=f"toggle_plan_{plan_id}"
        )],
        [InlineKeyboardButton(text="« Назад", callback_data=f"admin_plans_for_type_{plan['type_id']}")]
    ]

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_plan_field_"))
async def edit_plan_field(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования поля плана"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        parts = callback.data.split("_")
        # format: edit_plan_field_{plan_id}_{field}
        # Например: edit_plan_field_1_days или edit_plan_field_1_price или edit_plan_field_1_traffic_limit_bytes
        if len(parts) < 5:
            await callback.answer("❌ Неверный формат данных", show_alert=True)
            return

        plan_id = int(parts[3])

        # Собираем поле обратно, так как оно может состоять из нескольких частей
        field_parts = parts[4:]
        field = "_".join(field_parts)

        logger.info(f"Editing plan field: plan_id={plan_id}, field={field}")

    except (IndexError, ValueError) as e:
        logger.error(f"Error parsing callback data: {e}")
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    field_names = {
        "days": "количество дней",
        "price": "цену",
        "traffic_limit_bytes": "лимит трафика (в GB, или '-' для безлимита)"
    }

    if field not in field_names:
        logger.error(f"Unknown field: {field}")
        await callback.answer(f"❌ Неизвестное поле: {field}", show_alert=True)
        return

    await state.set_state(EditTariffField.value)
    await state.update_data(
        entity_type='plan',
        entity_id=plan_id,
        field=field
    )

    # Отправляем новое сообщение вместо редактирования
    await callback.message.answer(
        f"Введите новое <b>{field_names[field]}</b> для плана:",
        parse_mode="HTML"
    )

    await callback.answer()


@router.callback_query(F.data.startswith("toggle_plan_"))
async def toggle_plan_status(callback: CallbackQuery):
    """Переключение статуса плана"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        plan_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    # Получаем информацию о плане
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tp.*, tt.name as type_name, tt.id as type_id
            FROM tariff_plans tp
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE tp.id = ?
        """, (plan_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await callback.answer("❌ План не найден", show_alert=True)
                return
            plan = dict(row)

        # Переключаем статус
        new_status = 0 if plan['is_active'] else 1
        await db.execute(
            "UPDATE tariff_plans SET is_active = ? WHERE id = ?",
            (new_status, plan_id)
        )
        await db.commit()

    await callback.answer(f"Статус изменен на {'активен' if new_status else 'неактивен'}")

    # Получаем обновленные данные плана
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tp.*, tt.name as type_name, tt.id as type_id
            FROM tariff_plans tp
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE tp.id = ?
        """, (plan_id,)) as cursor:
            row = await cursor.fetchone()
            updated_plan = dict(row) if row else None

    if updated_plan:
        traffic_text = "безлимит" if updated_plan[
                                         'traffic_limit_bytes'] == 0 else f"{updated_plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

        text = (
            f"✏️ <b>Редактирование плана</b>\n\n"
            f"Тип: {updated_plan['type_name']}\n"
            f"ID плана: {updated_plan['id']}\n"
            f"Дней: {updated_plan['days']}\n"
            f"Цена: {updated_plan['price']} руб.\n"
            f"📊 Лимит трафика: {traffic_text}\n"
            f"Статус: {'✅ Активен' if updated_plan['is_active'] else '❌ Неактивен'}\n\n"
            f"Что хотите изменить?"
        )

        keyboard = [
            [InlineKeyboardButton(text="📅 Количество дней", callback_data=f"edit_plan_field_{plan_id}_days")],
            [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_plan_field_{plan_id}_price")],
            [InlineKeyboardButton(text="📊 Лимит трафика",
                                  callback_data=f"edit_plan_field_{plan_id}_traffic_limit_bytes")],
            [InlineKeyboardButton(
                text="🔄 Переключить статус",
                callback_data=f"toggle_plan_{plan_id}"
            )],
            [InlineKeyboardButton(text="« Назад", callback_data=f"admin_plans_for_type_{updated_plan['type_id']}")]
        ]

        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )

        # Удаляем старое сообщение
        try:
            await callback.message.delete()
        except:
            pass

# ======================================================
# ОБЩИЙ ОБРАБОТЧИК ДЛЯ РЕДАКТИРОВАНИЯ ПОЛЕЙ
# ======================================================

@router.message(EditTariffField.value)
async def process_field_edit(message: Message, state: FSMContext):
    """Обработка ввода нового значения для поля"""
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    data = await state.get_data()

    entity_type = data.get('entity_type')
    entity_id = data.get('entity_id')
    field = data.get('field')
    value = message.text.strip()

    if not all([entity_type, entity_id, field]):
        await message.answer("❌ Ошибка: данные не найдены. Начните заново.")
        await state.clear()
        return

    # Специальная обработка для traffic_limit_bytes
    if field == 'traffic_limit_bytes':
        if value == '-':
            value = 0  # безлимит
        else:
            if not value.isdigit() or int(value) <= 0:
                await message.answer("❌ Введите положительное число или '-' для безлимита!")
                return
            # Переводим GB в байты
            value = int(value) * 1024 * 1024 * 1024
    # Валидация для остальных числовых полей
    elif field in ['days', 'price', 'hwid_limit']:
        if not value.isdigit() or int(value) <= 0:
            await message.answer("❌ Введите положительное число!")
            return
        value = int(value)

    table = "tariff_types" if entity_type == 'type' else "tariff_plans"

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"UPDATE {table} SET {field} = ? WHERE id = ?", (value, entity_id))
            await db.commit()

        await message.answer("✅ Поле успешно обновлено!")

        # Отправляем новое сообщение с обновленным меню
        if entity_type == 'type':
            # Получаем обновленную информацию о типе
            tariff_type = await database.get_tariff_type(entity_id)

            if tariff_type:
                text = (
                    f"✏️ <b>Редактирование типа тарифа</b>\n\n"
                    f"ID: {tariff_type['id']}\n"
                    f"Название: {tariff_type['name']}\n"
                    f"Squad ID: {tariff_type['squad_id']}\n"
                    f"Лимит устройств: {tariff_type['hwid_limit']}\n"
                    f"Описание: {tariff_type['description'] or 'нет'}\n"
                    f"Статус: {'✅ Активен' if tariff_type['is_active'] else '❌ Неактивен'}\n\n"
                    f"Что хотите изменить?"
                )

                keyboard = [
                    [InlineKeyboardButton(text="📝 Название", callback_data=f"edit_type_field_{entity_id}_name")],
                    [InlineKeyboardButton(text="🆔 Squad ID", callback_data=f"edit_type_field_{entity_id}_squad_id")],
                    [InlineKeyboardButton(text="📱 Лимит устройств",
                                          callback_data=f"edit_type_field_{entity_id}_hwid_limit")],
                    [InlineKeyboardButton(text="📄 Описание", callback_data=f"edit_type_field_{entity_id}_description")],
                    [InlineKeyboardButton(
                        text="🔄 Переключить статус",
                        callback_data=f"toggle_type_{entity_id}"
                    )],
                    [InlineKeyboardButton(text="« Назад к списку", callback_data="admin_list_types")]
                ]

                await message.answer(
                    text,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                    parse_mode="HTML"
                )
        else:
            # Для планов - получаем обновленную информацию
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("""
                    SELECT tp.*, tt.name as type_name, tt.id as type_id
                    FROM tariff_plans tp
                    JOIN tariff_types tt ON tp.tariff_type_id = tt.id
                    WHERE tp.id = ?
                """, (entity_id,)) as cursor:
                    row = await cursor.fetchone()
                    plan = dict(row) if row else None

            if plan:
                traffic_text = "безлимит" if plan[
                                                 'traffic_limit_bytes'] == 0 else f"{plan['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

                text = (
                    f"✏️ <b>Редактирование плана</b>\n\n"
                    f"Тип: {plan['type_name']}\n"
                    f"ID плана: {plan['id']}\n"
                    f"Дней: {plan['days']}\n"
                    f"Цена: {plan['price']} руб.\n"
                    f"📊 Лимит трафика: {traffic_text}\n"
                    f"Статус: {'✅ Активен' if plan['is_active'] else '❌ Неактивен'}\n\n"
                    f"Что хотите изменить?"
                )

                keyboard = [
                    [InlineKeyboardButton(text="📅 Количество дней", callback_data=f"edit_plan_field_{entity_id}_days")],
                    [InlineKeyboardButton(text="💰 Цена", callback_data=f"edit_plan_field_{entity_id}_price")],
                    [InlineKeyboardButton(text="📊 Лимит трафика",
                                          callback_data=f"edit_plan_field_{entity_id}_traffic_limit_bytes")],
                    [InlineKeyboardButton(
                        text="🔄 Переключить статус",
                        callback_data=f"toggle_plan_{entity_id}"
                    )],
                    [InlineKeyboardButton(text="« Назад", callback_data=f"admin_plans_for_type_{plan['type_id']}")]
                ]

                await message.answer(
                    text,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
                    parse_mode="HTML"
                )

    except Exception as e:
        logger.error(f"Error updating {table}: {e}")
        await message.answer(f"❌ Ошибка при обновлении: {str(e)}")

    await state.clear()


# ======================================================
# СОЗДАНИЕ ТИПА ТАРИФА
# ======================================================

@router.callback_query(F.data == "admin_create_type")
async def create_type_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания нового типа тарифа"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(CreateTariffType.name)
    await callback.message.edit_text(
        "📝 <b>Создание нового типа тарифа</b>\n\n"
        "Введите название типа (например, Default, Premium, Business):",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(CreateTariffType.name)
async def create_type_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(CreateTariffType.squad_id)
    await message.answer(
        "Введите <b>Squad ID</b> из Remnawave:",
        parse_mode="HTML"
    )


@router.message(CreateTariffType.squad_id)
async def create_type_squad(message: Message, state: FSMContext):
    await state.update_data(squad_id=message.text)
    await state.set_state(CreateTariffType.hwid_limit)
    await message.answer(
        "Введите <b>лимит устройств</b> (по умолчанию 3):\n"
        "Или отправьте 0 для значения по умолчанию",
        parse_mode="HTML"
    )


@router.message(CreateTariffType.hwid_limit)
async def create_type_hwid(message: Message, state: FSMContext):
    hwid_limit = 3
    if message.text.isdigit() and int(message.text) > 0:
        hwid_limit = int(message.text)

    await state.update_data(hwid_limit=hwid_limit)
    await state.set_state(CreateTariffType.description)
    await message.answer(
        "Введите <b>описание</b> типа тарифа (или отправьте '-' чтобы пропустить):",
        parse_mode="HTML"
    )


@router.message(CreateTariffType.description)
async def create_type_description(message: Message, state: FSMContext):
    description = None if message.text == '-' else message.text
    data = await state.get_data()

    # Создаем тип в БД
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tariff_types (name, squad_id, hwid_limit, description)
            VALUES (?, ?, ?, ?)
        """, (data['name'], data['squad_id'], data['hwid_limit'], description))
        await db.commit()

        # Получаем ID созданного типа
        async with db.execute("SELECT last_insert_rowid()") as cursor:
            type_id = (await cursor.fetchone())[0]

    await message.answer(
        f"✅ <b>Тип тарифа успешно создан!</b>\n\n"
        f"ID: {type_id}\n"
        f"Название: {data['name']}\n"
        f"Squad ID: {data['squad_id']}\n"
        f"Лимит устройств: {data['hwid_limit']}\n"
        f"Описание: {description or 'нет'}",
        parse_mode="HTML"
    )
    await state.clear()


# ======================================================
# СОЗДАНИЕ ПЛАНА
# ======================================================

@router.callback_query(F.data.startswith("admin_create_plan_for_"))
async def create_plan_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания нового плана"""
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        type_id = int(callback.data.split("_")[4])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка в данных", show_alert=True)
        return

    tariff_type = await database.get_tariff_type(type_id)
    if not tariff_type:
        await callback.answer("❌ Тип не найден", show_alert=True)
        return

    await state.update_data(type_id=type_id)
    await state.set_state(CreateTariffPlan.days)

    await callback.message.edit_text(
        f"📝 <b>Создание плана для типа {tariff_type['name']}</b>\n\n"
        f"Введите количество дней:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(CreateTariffPlan.days)
async def create_plan_days(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Введите положительное число!")
        return

    await state.update_data(days=int(message.text))
    await state.set_state(CreateTariffPlan.price)
    await message.answer(
        "Введите цену в рублях:",
        parse_mode="HTML"
    )


@router.message(CreateTariffPlan.price)
async def create_plan_price(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Введите положительное число!")
        return

    await state.update_data(price=int(message.text))
    await state.set_state(CreateTariffPlan.traffic_limit)
    await message.answer(
        "📊 Введите <b>лимит трафика</b> в GB (или отправьте <code>-</code> для безлимита):\n\n"
        "Например:\n"
        "• <code>100</code> - 100 GB\n"
        "• <code>-</code> - безлимит",
        parse_mode="HTML"
    )


@router.message(CreateTariffPlan.traffic_limit)
async def create_plan_traffic(message: Message, state: FSMContext):
    data = await state.get_data()

    # Определяем лимит трафика
    traffic_limit_bytes = 0  # по умолчанию безлимит

    if message.text.strip() != '-':
        if not message.text.isdigit() or int(message.text) <= 0:
            await message.answer("❌ Введите положительное число или '-' для безлимита!")
            return
        # Переводим GB в байты
        traffic_limit_bytes = int(message.text) * 1024 * 1024 * 1024

    # Создаем план в БД
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tariff_plans (tariff_type_id, days, price, traffic_limit_bytes)
            VALUES (?, ?, ?, ?)
        """, (data['type_id'], data['days'], data['price'], traffic_limit_bytes))
        await db.commit()

        async with db.execute("SELECT last_insert_rowid()") as cursor:
            plan_id = (await cursor.fetchone())[0]

    traffic_text = "безлимит" if traffic_limit_bytes == 0 else f"{message.text} GB"

    await message.answer(
        f"✅ <b>План успешно создан!</b>\n\n"
        f"ID: {plan_id}\n"
        f"Дней: {data['days']}\n"
        f"Цена: {data['price']} руб.\n"
        f"📊 Лимит трафика: {traffic_text}",
        parse_mode="HTML"
    )
    await state.clear()


# ======================================================
# ДОБАВЛЕНИЕ ДНЕЙ
# ======================================================

@router.callback_query(F.data == "admin_add_days")
async def start_add_days(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(AddDays.user_id)
    await callback.message.answer(
        "1️⃣ Введите <b>Telegram ID</b> пользователя:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddDays.user_id)
async def process_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите корректный Telegram ID!")
        return

    user_id = int(message.text)
    user = await database.get_user(user_id)

    if not user:
        await message.answer("❌ Пользователь не найден в базе данных")
        await state.clear()
        return

    # Получаем все подписки пользователя
    subscriptions = await database.get_user_subscriptions(user_id)

    if not subscriptions:
        await message.answer("❌ У пользователя нет подписок")
        await state.clear()
        return

    await state.update_data(user_id=user_id)

    # Предлагаем выбрать подписку
    keyboard = []
    for sub in subscriptions:
        keyboard.append([InlineKeyboardButton(
            text=f"{sub['tariff_name']} (до {sub['subscription_expiry'][:10]})",
            callback_data=f"admin_add_days_sub_{sub['id']}"
        )])

    await message.answer(
        f"👤 Найден пользователь ID: {user_id}\n\n"
        f"2️⃣ Выберите подписку для продления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.set_state(AddDays.subscription_id)


@router.callback_query(F.data.startswith("admin_add_days_sub_"))
async def process_subscription_select(callback: CallbackQuery, state: FSMContext):
    try:
        subscription_id = int(callback.data.split("_")[4])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка получения ID подписки", show_alert=True)
        return

    await state.update_data(subscription_id=subscription_id)
    await state.set_state(AddDays.days)

    await callback.message.edit_text(
        "3️⃣ Введите <b>количество дней</b> для добавления:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddDays.days)
async def process_add_days(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Введите положительное число!")
        return

    days = int(message.text)
    data = await state.get_data()

    if 'subscription_id' not in data:
        await message.answer("❌ Ошибка: не выбрана подписка")
        await state.clear()
        return

    subscription_id = data['subscription_id']

    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await message.answer("❌ Подписка не найдена")
        await state.clear()
        return

    status_msg = await message.answer("⏳ Добавляю дни...")

    # Получаем план подписки для лимита трафика
    plan = await database.get_tariff_plan(subscription['tariff_plan_id'])
    traffic_limit = plan['traffic_limit_bytes'] if plan else 0

    # Продлеваем в панели с обновлением лимита трафика
    new_expiry = await panel_api.extend_subscription_by_username(
        subscription['username_in_panel'],
        days,
        traffic_limit  # передаем лимит трафика
    )

    if new_expiry:
        # Обновляем в БД
        await database.update_subscription_expiry(subscription_id, new_expiry)

        # Форматируем дату
        from datetime import datetime, timedelta
        date_obj = datetime.fromisoformat(new_expiry.replace('Z', '+00:00'))
        msk_time = date_obj + timedelta(hours=3)
        formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

        # Уведомляем пользователя
        try:
            traffic_text = "безлимитный" if traffic_limit == 0 else f"{traffic_limit / (1024**3):.0f} GB"
            await message.bot.send_message(
                subscription['telegram_id'],
                f"🎉 <b>Вам добавлены дни подписки!</b>\n\n"
                f"📦 Тариф: {subscription['tariff_name']}\n"
                f"➕ +{days} дней\n"
                f"📊 Лимит трафика: {traffic_text}\n"
                f"📅 Новая дата: {formatted_date}",
                parse_mode="HTML"
            )
            notify_status = "✅ Уведомление отправлено"
        except Exception as e:
            notify_status = f"❌ Ошибка уведомления: {e}"

        await status_msg.edit_text(
            f"✅ <b>Дни успешно добавлены!</b>\n\n"
            f"👤 Пользователь ID: {subscription['telegram_id']}\n"
            f"📦 Подписка: {subscription['tariff_name']}\n"
            f"➕ Добавлено дней: {days}\n"
            f"📊 Лимит трафика: {'безлимит' if traffic_limit == 0 else f'{traffic_limit / (1024**3):.0f} GB'}\n"
            f"📅 Новая дата: {formatted_date}\n"
            f"{notify_status}",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            "❌ Ошибка при добавлении дней.\n"
            f"Username в панели: {subscription['username_in_panel']}",
            parse_mode="HTML"
        )

    await state.clear()


# ======================================================
# ДОБАВЛЕНИЕ АДМИНИСТРАТОРА
# ======================================================

@router.callback_query(F.data == "admin_add_admin")
async def start_add_admin(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(AddAdmin.telegram_id)
    await callback.message.answer(
        "👑 Введите <b>Telegram ID</b> пользователя, которого хотите сделать администратором:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AddAdmin.telegram_id)
async def process_add_admin(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if not message.text.isdigit():
        await message.answer("❌ Введите корректный Telegram ID (только цифры)!")
        return

    tg_id = int(message.text)

    user = await database.get_user(tg_id)
    if not user:
        await message.answer(
            "❌ Пользователь с таким ID не найден в базе данных.\n"
            "Сначала пользователь должен запустить бота (/start)."
        )
        await state.clear()
        return

    if await database.is_admin(tg_id):
        await message.answer("❌ Этот пользователь уже является администратором!")
        await state.clear()
        return

    await database.set_user_role(tg_id, "admin")

    try:
        user_info = await message.bot.get_chat(tg_id)
        username = user_info.username or "нет username"
        full_name = user_info.full_name or "нет имени"
    except:
        username = user[1] or "нет username"
        full_name = "неизвестно"

    try:
        await message.bot.send_message(
            tg_id,
            f"👑 <b>Поздравляем!</b>\n\n"
            f"Вам назначены права администратора в боте.\n"
            f"Теперь вам доступна команда /admin для управления сервисом.",
            parse_mode="HTML"
        )
        notify_status = "✅ Уведомление отправлено"
    except Exception as e:
        notify_status = f"❌ Не удалось отправить уведомление: {str(e)}"

    await message.answer(
        f"✅ <b>Администратор успешно назначен!</b>\n\n"
        f"👤 ID: {tg_id}\n"
        f"📝 Username: @{username}\n"
        f"📋 Имя: {full_name}\n"
        f"{notify_status}",
        parse_mode="HTML"
    )

    await state.clear()


# ======================================================
# СПИСОК АДМИНИСТРАТОРОВ
# ======================================================

@router.callback_query(F.data == "admin_list")
async def admin_list(callback: CallbackQuery):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    admins = await database.get_all_admins()

    if not admins:
        await callback.message.edit_text(
            "📋 <b>Список администраторов:</b>\n\n"
            "❌ Администраторы не найдены",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад в админ-меню", callback_data="admin_back")]
            ])
        )
        await callback.answer()
        return

    admin_list_text = "📋 <b>Список администраторов:</b>\n\n"

    for admin in admins:
        admin_id = admin[0]
        username = admin[1] or "нет username"

        try:
            user_info = await callback.bot.get_chat(admin_id)
            full_name = user_info.full_name or "нет имени"
        except:
            full_name = "неизвестно"

        is_main = "⭐️ " if admin_id == ADMIN_ID else ""

        admin_list_text += f"{is_main}👤 ID: <code>{admin_id}</code>\n"
        admin_list_text += f"   Имя: {full_name}\n"
        admin_list_text += f"   Username: @{username}\n\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в админ-меню", callback_data="admin_back")]
    ])

    await callback.message.edit_text(
        admin_list_text,
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


# ======================================================
# РАССЫЛКА
# ======================================================

@router.callback_query(F.data == "admin_mailing")
async def start_mailing(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(Mailing.content)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить рассылку", callback_data="cancel_mailing")]
    ])

    await callback.message.edit_text(
        "📢 <b>Создание рассылки</b>\n\n"
        "Отправьте сообщение для рассылки.\n\n"
        "Поддерживаются:\n"
        "• Текст с форматированием\n"
        "• Фотография (1 шт)\n"
        "• Видео\n"
        "• Документы\n\n"
        "Сообщение будет доставлено ВСЕМ пользователям бота.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_mailing")
async def cancel_mailing(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # Создаем искусственный callback для возврата в админ-меню
    fake_callback = types.CallbackQuery(
        id="0",
        from_user=callback.from_user,
        chat_instance="0",
        message=callback.message,
        data="admin_back"
    )
    await back_to_admin(fake_callback)
    await callback.answer("✅ Рассылка отменена")


@router.message(Mailing.content)
async def process_mailing_content(message: Message, state: FSMContext, bot):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if message.media_group_id:
        await message.answer(
            "❌ Альбомы не поддерживаются. Отправьте одно фото или видео."
        )
        return

    content_data = {
        'text': message.html_text if message.html_text else None,
        'caption': message.html_text if message.caption else None,
        'content_type': message.content_type,
        'message_id': message.message_id,
        'from_chat_id': message.chat.id
    }

    if message.photo:
        photo = message.photo[-1]
        content_data['file_id'] = photo.file_id
        content_data['file_unique_id'] = photo.file_unique_id
        content_data['content_type'] = 'photo'

    elif message.video:
        content_data['file_id'] = message.video.file_id
        content_data['file_unique_id'] = message.video.file_unique_id
        content_data['content_type'] = 'video'

    elif message.document:
        content_data['file_id'] = message.document.file_id
        content_data['file_unique_id'] = message.document.file_unique_id
        content_data['content_type'] = 'document'

    elif message.animation:
        content_data['file_id'] = message.animation.file_id
        content_data['file_unique_id'] = message.animation.file_unique_id
        content_data['content_type'] = 'animation'

    elif message.voice:
        content_data['file_id'] = message.voice.file_id
        content_data['file_unique_id'] = message.voice.file_unique_id
        content_data['content_type'] = 'voice'

    elif message.video_note:
        content_data['file_id'] = message.video_note.file_id
        content_data['file_unique_id'] = message.video_note.file_unique_id
        content_data['content_type'] = 'video_note'

    elif message.text:
        content_data['content_type'] = 'text'

    else:
        await message.answer("❌ Неподдерживаемый тип сообщения")
        return

    await state.update_data(content=content_data)
    await show_mailing_preview(message, state)


async def show_mailing_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    content = data['content']

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_mailing"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_mailing")
        ],
        [InlineKeyboardButton(text="📝 Отправить заново", callback_data="restart_mailing")]
    ])

    preview_text = (
        "📢 <b>Предпросмотр рассылки</b>\n\n"
        "Так сообщение увидят пользователи:\n"
        "━━━━━━━━━━━━━━━━"
    )

    await message.answer(preview_text, parse_mode="HTML")

    try:
        if content['content_type'] == 'text':
            await message.answer(
                content['text'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'photo':
            await message.answer_photo(
                content['file_id'],
                caption=content['caption'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'video':
            await message.answer_video(
                content['file_id'],
                caption=content['caption'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'document':
            await message.answer_document(
                content['file_id'],
                caption=content['caption'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'animation':
            await message.answer_animation(
                content['file_id'],
                caption=content['caption'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'voice':
            await message.answer_voice(
                content['file_id'],
                caption=content['caption'],
                parse_mode="HTML"
            )
        elif content['content_type'] == 'video_note':
            await message.answer_video_note(
                content['file_id']
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка при создании превью: {str(e)}")

    await message.answer(
        "Подтвердите отправку рассылки всем пользователям:",
        reply_markup=keyboard
    )


@router.callback_query(F.data == "restart_mailing")
async def restart_mailing(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Mailing.content)
    await callback.message.edit_text(
        "📢 Отправьте новое сообщение для рассылки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_mailing")]
        ])
    )
    await callback.answer()


@router.callback_query(F.data == "confirm_mailing")
async def confirm_mailing(callback: CallbackQuery, state: FSMContext, bot):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    data = await state.get_data()
    content = data['content']

    users = await get_all_users()

    total_users = len(users)
    if total_users == 0:
        await callback.message.edit_text("❌ Нет пользователей для рассылки")
        await state.clear()
        return

    status_message = await callback.message.edit_text(
        f"📢 Начинаю рассылку...\n"
        f"Всего пользователей: {total_users}\n"
        f"Отправлено: 0/{total_users}"
    )

    sent = 0
    failed = 0
    blocked = 0

    for user in users:
        user_id = user[0]

        try:
            if content['content_type'] == 'text':
                await bot.send_message(
                    user_id,
                    content['text'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'photo':
                await bot.send_photo(
                    user_id,
                    content['file_id'],
                    caption=content['caption'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'video':
                await bot.send_video(
                    user_id,
                    content['file_id'],
                    caption=content['caption'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'document':
                await bot.send_document(
                    user_id,
                    content['file_id'],
                    caption=content['caption'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'animation':
                await bot.send_animation(
                    user_id,
                    content['file_id'],
                    caption=content['caption'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'voice':
                await bot.send_voice(
                    user_id,
                    content['file_id'],
                    caption=content['caption'],
                    parse_mode="HTML"
                )
            elif content['content_type'] == 'video_note':
                await bot.send_video_note(
                    user_id,
                    content['file_id']
                )

            sent += 1

        except Exception as e:
            if "bot was blocked by the user" in str(e):
                blocked += 1
            else:
                failed += 1
                logger.error(f"Failed to send to {user_id}: {e}")

        if (sent + failed + blocked) % 10 == 0:
            try:
                await status_message.edit_text(
                    f"📢 Рассылка в процессе...\n"
                    f"Всего: {total_users}\n"
                    f"✅ Отправлено: {sent}\n"
                    f"❌ Ошибок: {failed}\n"
                    f"🚫 Заблокировали бота: {blocked}"
                )
            except:
                pass

        await asyncio.sleep(0.05)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад в админ-меню", callback_data="admin_back")]
    ])

    await status_message.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"📊 Статистика:\n"
        f"• Всего пользователей: {total_users}\n"
        f"• ✅ Успешно отправлено: {sent}\n"
        f"• ❌ Ошибок отправки: {failed}\n"
        f"• 🚫 Заблокировали бота: {blocked}\n\n"
        f"Рассылка выполнена за {sent + failed + blocked} сообщений.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

    await state.clear()


# ======================================================
# БАН
# ======================================================

@router.callback_query(F.data == "admin_ban_user")
async def start_ban(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(BanUser.telegram_id)
    await callback.message.answer(
        "🚫 Введите Telegram ID пользователя для бана:\n"
        "(Пользователь будет забанен, а ВСЕ его подписки отключены)"
    )
    await callback.answer()


@router.message(BanUser.telegram_id)
async def process_ban(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if not message.text.isdigit():
        await message.answer("❌ Введите корректный Telegram ID.")
        return

    tg_id = int(message.text)

    if tg_id == message.from_user.id:
        await message.answer("❌ Вы не можете забанить самого себя!")
        await state.clear()
        return

    from config.config import ADMIN_ID
    if tg_id == ADMIN_ID:
        await message.answer("❌ Вы не можете забанить главного администратора!")
        await state.clear()
        return

    user = await database.get_user(tg_id)
    if not user:
        await message.answer("❌ Пользователь не найден в базе.")
        await state.clear()
        return

    subscriptions = await database.get_user_subscriptions(tg_id)

    if not subscriptions:
        await message.answer("❌ У пользователя нет подписок.")
    else:
        status_msg = await message.answer(f"⏳ Отключаю подписки пользователя {tg_id}...")

        success_count = 0
        fail_count = 0

        for sub in subscriptions:
            try:
                result = await panel_api.update_user_status_by_username(
                    sub['username_in_panel'],
                    "DISABLED"
                )
                if result:
                    await database.update_subscription_status(sub['id'], "DISABLED")
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.error(f"Failed to disable subscription {sub['id']}: {e}")
                fail_count += 1

        await status_msg.edit_text(
            f"🚫 Пользователь {tg_id} забанен.\n"
            f"📊 Отключено подписок: {success_count}\n"
            f"❌ Ошибок: {fail_count}"
        )

    await database.set_user_role(tg_id, "banned")
    await state.clear()


# ======================================================
# АНБАН
# ======================================================

@router.callback_query(F.data == "admin_unban_user")
async def start_unban(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(UnbanUser.telegram_id)
    await callback.message.answer(
        "✅ Введите Telegram ID для разбана:\n"
        "(Пользователь будет разбанен, а его подписки активированы)"
    )
    await callback.answer()


@router.message(UnbanUser.telegram_id)
async def process_unban(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if not message.text.isdigit():
        await message.answer("❌ Введите корректный Telegram ID.")
        return

    tg_id = int(message.text)

    user = await database.get_user(tg_id)
    if not user:
        await message.answer("❌ Пользователь не найден в базе.")
        await state.clear()
        return

    subscriptions = await database.get_user_subscriptions(tg_id)

    if subscriptions:
        status_msg = await message.answer(f"⏳ Активирую подписки пользователя {tg_id}...")

        success_count = 0
        fail_count = 0

        for sub in subscriptions:
            try:
                result = await panel_api.update_user_status_by_username(
                    sub['username_in_panel'],
                    "ACTIVE"
                )
                if result:
                    await database.update_subscription_status(sub['id'], "ACTIVE")
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.error(f"Failed to enable subscription {sub['id']}: {e}")
                fail_count += 1

        await status_msg.edit_text(
            f"✅ Пользователь {tg_id} разбанен.\n"
            f"📊 Активировано подписок: {success_count}\n"
            f"❌ Ошибок: {fail_count}"
        )
    else:
        await message.answer(f"✅ Пользователь {tg_id} разбанен. (нет активных подписок)")

    await database.set_user_role(tg_id, "user")
    await state.clear()


# ======================================================
# НАЗАД В АДМИН-МЕНЮ
# ======================================================

@router.callback_query(F.data == "admin_back")
async def back_to_admin(callback: CallbackQuery):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Управление тарифами", callback_data="admin_tariffs")],
        [InlineKeyboardButton(text="➕ Добавить дни подписки", callback_data="admin_add_days")],
        [InlineKeyboardButton(text="➖ Уменьшить дни подписки", callback_data="admin_reduce_days")],
        [InlineKeyboardButton(text="💾 Управление бэкапами", callback_data="admin_backup")],
        [InlineKeyboardButton(text="👑 Добавить администратора", callback_data="admin_add_admin")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="🚫 Забанить пользователя", callback_data="admin_ban_user")],
        [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban_user")],
        [InlineKeyboardButton(text="📋 Список администраторов", callback_data="admin_list")]
    ])

    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>\nВыберите действие:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


# ======================================================
# УМЕНЬШЕНИЕ ДНЕЙ ПОДПИСКИ
# ======================================================

@router.callback_query(F.data == "admin_reduce_days")
async def start_reduce_days(callback: CallbackQuery, state: FSMContext):
    if not await database.is_admin(callback.from_user.id):
        await callback.answer("❌ У вас нет прав администратора", show_alert=True)
        return

    await state.set_state(ReduceDays.user_id)
    await callback.message.answer(
        "1️⃣ Введите <b>Telegram ID</b> пользователя, у которого нужно уменьшить дни:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(ReduceDays.user_id)
async def process_reduce_user_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите корректный Telegram ID!")
        return

    user_id = int(message.text)
    user = await database.get_user(user_id)

    if not user:
        await message.answer("❌ Пользователь не найден в базе данных")
        await state.clear()
        return

    # Получаем все подписки пользователя
    subscriptions = await database.get_subscriptions_by_telegram_id(user_id)

    if not subscriptions:
        await message.answer("❌ У пользователя нет подписок")
        await state.clear()
        return

    await state.update_data(user_id=user_id)

    # Формируем клавиатуру с подписками
    keyboard = []
    for sub in subscriptions:
        # Получаем актуальную информацию из API для отображения статуса
        info = await panel_api.format_subscription_info_by_short_uuid(sub['short_uuid'])
        status_emoji = "✅" if info and info['is_active'] else "❌"

        # Форматируем дату для отображения
        expiry_date = sub['subscription_expiry']
        if expiry_date:
            try:
                date_obj = datetime.fromisoformat(expiry_date.replace('Z', '+00:00'))
                msk_time = date_obj + timedelta(hours=3)
                formatted_date = msk_time.strftime("%d.%m.%Y")
            except:
                formatted_date = expiry_date[:10]
        else:
            formatted_date = "нет даты"

        traffic_text = "безлимит" if sub[
                                         'traffic_limit_bytes'] == 0 else f"{sub['traffic_limit_bytes'] / (1024 ** 3):.0f} GB"

        button_text = f"{status_emoji} {sub['tariff_name']} - до {formatted_date} ({traffic_text})"
        keyboard.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"reduce_days_sub_{sub['id']}"
        )])

    await message.answer(
        f"👤 Найден пользователь ID: {user_id}\n\n"
        f"2️⃣ Выберите подписку для уменьшения дней:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.set_state(ReduceDays.subscription_id)


@router.callback_query(F.data.startswith("reduce_days_sub_"), ReduceDays.subscription_id)
async def process_reduce_subscription_select(callback: CallbackQuery, state: FSMContext):
    try:
        subscription_id = int(callback.data.split("_")[3])
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка получения ID подписки", show_alert=True)
        return

    # Получаем информацию о подписке для отображения
    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.answer("❌ Подписка не найдена", show_alert=True)
        return

    await state.update_data(subscription_id=subscription_id)
    await state.set_state(ReduceDays.days)

    # Получаем актуальную информацию из API
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

    if info and info['is_active']:
        days_left = info['days_left']
        status_text = f"✅ Активна, осталось дней: <b>{days_left:.1f}</b>"
    else:
        status_text = "❌ Подписка неактивна или истекла"
        # Можно разрешить уменьшение даже для неактивной?

    await callback.message.edit_text(
        f"📦 <b>Подписка: {subscription['tariff_name']}</b>\n\n"
        f"{status_text}\n"
        f"📅 Текущая дата: {subscription['subscription_expiry'][:10]}\n\n"
        f"3️⃣ Введите <b>количество дней</b> для УМЕНЬШЕНИЯ:\n"
        f"<i>(например, 7 - уменьшит срок на 7 дней)</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(ReduceDays.days)
async def process_reduce_days(message: Message, state: FSMContext):
    if not await database.is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора")
        await state.clear()
        return

    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Введите положительное число!")
        return

    days = int(message.text)
    data = await state.get_data()

    if 'subscription_id' not in data:
        await message.answer("❌ Ошибка: не выбрана подписка")
        await state.clear()
        return

    subscription_id = data['subscription_id']
    subscription = await database.get_subscription(subscription_id)

    if not subscription:
        await message.answer("❌ Подписка не найдена")
        await state.clear()
        return

    # Получаем актуальную информацию для проверки
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

    if not info:
        await message.answer("❌ Не удалось получить информацию о подписке из панели")
        return

    # ПРОВЕРКА: нельзя уменьшить больше, чем осталось дней
    days_left = info.get('days_left', 0)

    if days_left <= 0:
        await message.answer(
            "❌ Подписка уже истекла. Нельзя уменьшить количество дней.\n\n"
            f"Текущий статус: {info.get('status', 'неизвестно')}",
            parse_mode="HTML"
        )
        return

    if days >= days_left:
        await message.answer(
            f"❌ Нельзя уменьшить на {days} дней, так как осталось только {days_left:.1f} дней.\n\n"
            f"Максимальное допустимое уменьшение: {int(days_left - 0.1)} дней\n"
            f"(нельзя сделать отрицательный срок)",
            parse_mode="HTML"
        )
        return

    # ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ - показываем подтверждение
    await state.update_data(days=days)
    await state.set_state(ReduceDays.confirm)

    current_expiry = info['formatted_expiry']
    new_expiry_date = datetime.fromisoformat(subscription['subscription_expiry'].replace('Z', '+00:00')) - timedelta(
        days=days)
    msk_new = new_expiry_date + timedelta(hours=3)
    formatted_new = msk_new.strftime("%d.%m.%Y %H:%M МСК")

    # Дополнительная информация для админа
    warning = ""
    if days > days_left / 2:
        warning = "\n⚠️ <i>Вы уменьшаете больше половины оставшегося срока!</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_reduce_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_reduce_no")
        ]
    ])

    await message.answer(
        f"📦 <b>Подтверждение уменьшения дней</b>\n\n"
        f"Пользователь ID: {subscription['telegram_id']}\n"
        f"Подписка: {subscription['tariff_name']}\n"
        f"Осталось дней: <b>{days_left:.1f}</b>\n"
        f"Уменьшить на: <b>{days}</b> дней\n"
        f"Останется: <b>{days_left - days:.1f}</b> дней\n\n"
        f"Текущая дата: {current_expiry}\n"
        f"Новая дата: {formatted_new}{warning}\n\n"
        f"<i>Действие необратимо!</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("confirm_reduce_"), ReduceDays.confirm)
async def confirm_reduce_days(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[2]  # "yes" или "no"

    if action == "no":
        await state.clear()
        # Возвращаемся в админ-меню
        fake_callback = types.CallbackQuery(
            id="0",
            from_user=callback.from_user,
            chat_instance="0",
            message=callback.message,
            data="admin_back"
        )
        from admin import back_to_admin
        await back_to_admin(fake_callback)
        await callback.answer("✅ Операция отменена")
        return

    # action == "yes"
    data = await state.get_data()
    subscription_id = data.get('subscription_id')
    days = data.get('days')

    if not subscription_id or not days:
        await callback.message.edit_text("❌ Ошибка: данные не найдены")
        await state.clear()
        return

    subscription = await database.get_subscription(subscription_id)
    if not subscription:
        await callback.message.edit_text("❌ Подписка не найдена")
        await state.clear()
        return

    # ПОВТОРНАЯ ПРОВЕРКА перед выполнением
    info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])
    if not info:
        await callback.message.edit_text("❌ Не удалось получить информацию о подписке")
        await state.clear()
        return

    days_left = info.get('days_left', 0)
    if days_left <= 0:
        await callback.message.edit_text("❌ Подписка уже истекла, уменьшение невозможно")
        await state.clear()
        return

    if days >= days_left:
        await callback.message.edit_text(
            f"❌ Нельзя уменьшить на {days} дней, так как осталось только {days_left:.1f} дней",
            parse_mode="HTML"
        )
        await state.clear()
        return

    status_msg = await callback.message.edit_text("⏳ Уменьшаю дни подписки...")

    # Уменьшаем в панели
    new_expiry = await panel_api.reduce_subscription_days_by_username(
        subscription['username_in_panel'],
        days
    )

    if new_expiry:
        # Обновляем в БД
        await database.update_subscription_expiry(subscription_id, new_expiry)

        # Проверяем, не истекла ли подписка после уменьшения
        updated_info = await panel_api.format_subscription_info_by_short_uuid(subscription['short_uuid'])

        # Форматируем даты
        date_obj = datetime.fromisoformat(new_expiry.replace('Z', '+00:00'))
        msk_time = date_obj + timedelta(hours=3)
        formatted_date = msk_time.strftime("%d.%m.%Y %H:%M МСК")

        # Уведомляем пользователя
        try:
            remaining_days = updated_info['days_left'] if updated_info else 0
            await callback.bot.send_message(
                subscription['telegram_id'],
                f"⚠️ <b>Изменение срока подписки</b>\n\n"
                f"📦 Тариф: {subscription['tariff_name']}\n"
                f"➖ Срок уменьшен на {days} дней\n"
                f"📅 Новая дата: {formatted_date}\n"
                f"⏳ Осталось дней: {remaining_days:.1f}\n\n"
                f"<i>По вопросам обращайтесь к администрации</i>",
                parse_mode="HTML"
            )
            notify_status = "✅ Уведомление отправлено"
        except Exception as e:
            notify_status = f"❌ Ошибка уведомления: {e}"

        # Проверяем статус после уменьшения
        status_text = "✅ Подписка активна"
        if updated_info and not updated_info['is_active']:
            status_text = "⚠️ Подписка истекла после уменьшения"

        await status_msg.edit_text(
            f"✅ <b>Дни успешно уменьшены!</b>\n\n"
            f"👤 Пользователь ID: {subscription['telegram_id']}\n"
            f"📦 Подписка: {subscription['tariff_name']}\n"
            f"➖ Уменьшено дней: {days}\n"
            f"⏳ Оставалось: {days_left:.1f} дней\n"
            f"⏳ Осталось: {days_left - days:.1f} дней\n"
            f"📅 Новая дата: {formatted_date}\n"
            f"📊 {status_text}\n"
            f"{notify_status}",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            "❌ Ошибка при уменьшении дней.\n"
            f"Username в панели: {subscription['username_in_panel']}\n\n"
            f"<i>Возможно, произошла ошибка API</i>",
            parse_mode="HTML"
        )

    await state.clear()


@router.callback_query(F.data == "confirm_reduce_no")
async def cancel_reduce(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # Возвращаемся в админ-меню
    fake_callback = types.CallbackQuery(
        id="0",
        from_user=callback.from_user,
        chat_instance="0",
        message=callback.message,
        data="admin_back"
    )
    from admin import back_to_admin
    await back_to_admin(fake_callback)
    await callback.answer("✅ Операция отменена")