import os
import logging
import aiosqlite
import shutil
from datetime import datetime
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db import DB_PATH, is_admin
from config.config import ADMIN_ID

logger = logging.getLogger(__name__)
router = Router()

# Папка для временных файлов бэкапов
BACKUP_DIR = "/opt/vpnbot/backups"
# Изменил на постоянную папку
os.makedirs(BACKUP_DIR, exist_ok=True)


# ======================================================
# FSM СОСТОЯНИЯ ДЛЯ ВОССТАНОВЛЕНИЯ
# ======================================================

class RestoreBackup(StatesGroup):
    confirm = State()  # Подтверждение восстановления


class DeleteBackup(StatesGroup):
    confirm = State()  # Подтверждение удаления


# ======================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С БЭКАПАМИ
# ======================================================

async def create_backup() -> str:
    """Создать бэкап БД и вернуть путь к файлу"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{BACKUP_DIR}/vpnbot_backup_{timestamp}.db"

    # Копируем файл БД
    shutil.copy2(DB_PATH, backup_path)

    # Сжимаем для экономии места
    import gzip
    with open(backup_path, 'rb') as f_in:
        with gzip.open(f"{backup_path}.gz", 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)

    # Удаляем несжатый файл
    os.remove(backup_path)

    return f"{backup_path}.gz"


async def list_backups() -> list:
    """Получить список всех бэкапов"""
    backups = []
    for file in os.listdir(BACKUP_DIR):
        if file.endswith('.gz'):
            file_path = os.path.join(BACKUP_DIR, file)
            size = os.path.getsize(file_path)
            modified = datetime.fromtimestamp(os.path.getmtime(file_path))
            backups.append({
                'name': file,
                'path': file_path,
                'size': size,
                'modified': modified
            })

    # Сортируем по дате (новые сверху)
    backups.sort(key=lambda x: x['modified'], reverse=True)
    return backups


async def restore_backup(backup_path: str) -> bool:
    """Восстановить БД из бэкапа"""
    try:
        # Останавливаем бота (нужно перезапустить после)
        # Временно отключаем запись в БД через тач-файл
        with open('/tmp/vpnbot_restore.lock', 'w') as f:
            f.write('1')

        # Распаковываем если нужно
        import gzip
        temp_path = backup_path.replace('.gz', '')
        with gzip.open(backup_path, 'rb') as f_in:
            with open(temp_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Создаем бэкап текущей БД на всякий случай
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, f"{DB_PATH}.before_restore")

        # Восстанавливаем
        shutil.copy2(temp_path, DB_PATH)

        # Удаляем временный файл
        os.remove(temp_path)

        # Удаляем лок-файл
        os.remove('/tmp/vpnbot_restore.lock')

        return True
    except Exception as e:
        logger.error(f"Ошибка восстановления: {e}")
        return False


async def delete_backup(backup_path: str) -> bool:
    """Удалить бэкап"""
    try:
        if os.path.exists(backup_path):
            os.remove(backup_path)
            logger.info(f"✅ Бэкап удален: {backup_path}")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления бэкапа: {e}")
        return False


def format_size(size_bytes: int) -> str:
    """Форматировать размер файла"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


# ======================================================
# ОБРАБОТЧИКИ
# ======================================================

@router.callback_query(F.data == "admin_backup")
async def admin_backup_menu(callback: CallbackQuery):
    """Меню управления бэкапами"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Создать бэкап", callback_data="backup_create")],
        [InlineKeyboardButton(text="📋 Список бэкапов", callback_data="backup_list_1")],
        [InlineKeyboardButton(text="« Назад", callback_data="admin_back")]
    ])

    await callback.message.edit_text(
        "💾 <b>Управление резервными копиями</b>\n\n"
        "Здесь вы можете создать бэкап базы данных или восстановить существующий.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "backup_create")
async def backup_create(callback: CallbackQuery):
    """Создать новый бэкап"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    status_msg = await callback.message.edit_text(
        "⏳ Создаю резервную копию..."
    )

    try:
        backup_path = await create_backup()
        backup_name = os.path.basename(backup_path)
        backup_size = os.path.getsize(backup_path)

        # Отправляем файл админу
        with open(backup_path, 'rb') as f:
            await callback.message.answer_document(
                types.FSInputFile(backup_path),
                caption=f"✅ <b>Бэкап создан</b>\n\n"
                        f"📁 Файл: {backup_name}\n"
                        f"📦 Размер: {format_size(backup_size)}\n"
                        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
                parse_mode="HTML"
            )

        # Возвращаемся в меню бэкапов
        await admin_backup_menu(callback)

    except Exception as e:
        logger.error(f"Ошибка создания бэкапа: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка создания бэкапа: {str(e)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="admin_backup")]
            ])
        )

    await callback.answer()


@router.callback_query(F.data.startswith("backup_list_"))
async def backup_list(callback: CallbackQuery):
    """Показать список бэкапов с пагинацией"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Получаем номер страницы
    try:
        page = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        page = 1

    items_per_page = 5  # Показываем по 5 бэкапов на странице

    backups = await list_backups()
    total_backups = len(backups)
    total_pages = (total_backups + items_per_page - 1) // items_per_page

    # Корректируем страницу
    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages

    if not backups:
        await callback.message.edit_text(
            "📭 <b>Нет сохраненных бэкапов</b>\n\n"
            "Создайте первый бэкап с помощью кнопки 'Создать бэкап'.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="admin_backup")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Вычисляем срез для текущей страницы
    start_idx = (page - 1) * items_per_page
    end_idx = min(start_idx + items_per_page, total_backups)
    current_backups = backups[start_idx:end_idx]

    text = f"📋 <b>Доступные бэкапы</b> (страница {page}/{total_pages}):\n\n"
    keyboard = []

    for i, backup in enumerate(current_backups, start=start_idx + 1):
        modified_str = backup['modified'].strftime("%d.%m.%Y %H:%M")
        # Обрезаем имя файла для красоты
        display_name = backup['name'].replace('vpnbot_backup_', '').replace('.db.gz', '')
        text += f"{i}. <b>{display_name}</b>\n"
        text += f"   📦 {format_size(backup['size'])} | 🕐 {modified_str}\n\n"

        # Кнопки для каждого бэкапа
        keyboard.append([
            InlineKeyboardButton(
                text=f"🔄 Восстановить #{i}",
                callback_data=f"restore_backup_{backup['name']}"
            ),
            InlineKeyboardButton(
                text=f"📤 Отправить #{i}",
                callback_data=f"send_backup_{backup['name']}"
            ),
            InlineKeyboardButton(
                text=f"🗑️ Удалить #{i}",
                callback_data=f"delete_backup_{backup['name']}"
            )
        ])

    # Кнопки навигации
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущая",
            callback_data=f"backup_list_{page - 1}"
        ))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующая ▶️",
            callback_data=f"backup_list_{page + 1}"
        ))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="« Назад", callback_data="admin_backup")])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )
    await callback.answer()


# ======================================================
# ОТПРАВКА БЭКАПА
# ======================================================

@router.callback_query(F.data.startswith("send_backup_"))
async def send_backup(callback: CallbackQuery):
    """Отправить бэкап в чат"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    backup_name = callback.data.replace("send_backup_", "")
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    if not os.path.exists(backup_path):
        await callback.answer("❌ Файл бэкапа не найден", show_alert=True)
        return

    # Отправляем файл
    try:
        backup_size = os.path.getsize(backup_path)
        modified = datetime.fromtimestamp(os.path.getmtime(backup_path))

        with open(backup_path, 'rb') as f:
            await callback.message.answer_document(
                types.FSInputFile(backup_path),
                caption=f"📦 <b>Бэкап</b>\n\n"
                        f"📁 Файл: {backup_name}\n"
                        f"📦 Размер: {format_size(backup_size)}\n"
                        f"🕐 Создан: {modified.strftime('%d.%m.%Y %H:%M:%S')}",
                parse_mode="HTML"
            )

        await callback.answer("✅ Бэкап отправлен")

    except Exception as e:
        logger.error(f"Ошибка отправки бэкапа: {e}")
        await callback.answer("❌ Ошибка отправки", show_alert=True)


# ======================================================
# УДАЛЕНИЕ БЭКАПА
# ======================================================

@router.callback_query(F.data.startswith("delete_backup_"))
async def delete_backup_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления бэкапа"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    backup_name = callback.data.replace("delete_backup_", "")
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    if not os.path.exists(backup_path):
        await callback.answer("❌ Файл бэкапа не найден", show_alert=True)
        return

    # Сохраняем путь в состоянии
    await state.update_data(backup_path=backup_path, backup_name=backup_name)
    await state.set_state(DeleteBackup.confirm)

    # Информация о бэкапе
    backup_size = os.path.getsize(backup_path)
    modified = datetime.fromtimestamp(os.path.getmtime(backup_path))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="delete_confirm_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="delete_confirm_no")
        ]
    ])

    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение удаления</b>\n\n"
        f"Вы собираетесь удалить бэкап:\n"
        f"📁 {backup_name}\n"
        f"📦 Размер: {format_size(backup_size)}\n"
        f"🕐 Создан: {modified.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        f"<b>Это действие необратимо!</b>\n\n"
        f"Вы уверены?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "delete_confirm_yes", DeleteBackup.confirm)
async def delete_backup_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнить удаление бэкапа"""
    data = await state.get_data()
    backup_path = data.get('backup_path')
    backup_name = data.get('backup_name')

    success = await delete_backup(backup_path)

    if success:
        await callback.message.edit_text(
            f"✅ <b>Бэкап удален</b>\n\n"
            f"Файл: {backup_name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« К списку бэкапов", callback_data="backup_list_1")]
            ]),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка при удалении бэкапа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="backup_list_1")]
            ])
        )

    await state.clear()


@router.callback_query(F.data == "delete_confirm_no", DeleteBackup.confirm)
async def delete_backup_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена удаления"""
    await state.clear()
    # Возвращаемся к списку бэкапов на первой странице
    fake_callback = types.CallbackQuery(
        id="0",
        from_user=callback.from_user,
        chat_instance="0",
        message=callback.message,
        data="backup_list_1"
    )
    await backup_list(fake_callback)
    await callback.answer("✅ Удаление отменено")


# ======================================================
# ВОССТАНОВЛЕНИЕ БЭКАПА
# ======================================================

@router.callback_query(F.data.startswith("restore_backup_"))
async def restore_backup_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтверждение восстановления бэкапа"""
    if not await is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    backup_name = callback.data.replace("restore_backup_", "")
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    if not os.path.exists(backup_path):
        await callback.answer("❌ Файл бэкапа не найден", show_alert=True)
        return

    # Сохраняем путь в состоянии
    await state.update_data(backup_path=backup_path)
    await state.set_state(RestoreBackup.confirm)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, восстановить", callback_data="restore_confirm_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="restore_confirm_no")
        ]
    ])

    # Информация о бэкапе
    backup_size = os.path.getsize(backup_path)
    modified = datetime.fromtimestamp(os.path.getmtime(backup_path))

    await callback.message.edit_text(
        f"⚠️ <b>ВНИМАНИЕ!</b>\n\n"
        f"Вы собираетесь восстановить базу данных из бэкапа:\n"
        f"📁 {backup_name}\n"
        f"📦 Размер: {format_size(backup_size)}\n"
        f"🕐 Создан: {modified.strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        f"<b>Текущая база данных будет перезаписана!</b>\n"
        f"Все изменения, сделанные после этого бэкапа, будут потеряны.\n\n"
        f"После восстановления бота нужно перезапустить.\n\n"
        f"Вы уверены?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "restore_confirm_yes", RestoreBackup.confirm)
async def restore_backup_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнить восстановление"""
    data = await state.get_data()
    backup_path = data.get('backup_path')

    status_msg = await callback.message.edit_text(
        "⏳ Восстанавливаю базу данных..."
    )

    success = await restore_backup(backup_path)

    if success:
        await status_msg.edit_text(
            "✅ <b>База данных восстановлена!</b>\n\n"
            "Теперь нужно перезапустить бота командой:\n"
            "<code>systemctl restart vpnbot</code>\n\n"
            "Или дождитесь автоматического перезапуска через 10 секунд...",
            parse_mode="HTML"
        )

        # Автоматический перезапуск через 10 секунд
        import asyncio
        await asyncio.sleep(10)

        # Перезапускаем бота
        os.system("systemctl restart vpnbot")
    else:
        await status_msg.edit_text(
            "❌ Ошибка при восстановлении базы данных.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад", callback_data="admin_backup")]
            ])
        )

    await state.clear()


@router.callback_query(F.data == "restore_confirm_no", RestoreBackup.confirm)
async def restore_backup_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена восстановления"""
    await state.clear()
    await admin_backup_menu(callback)
    await callback.answer("✅ Восстановление отменено")