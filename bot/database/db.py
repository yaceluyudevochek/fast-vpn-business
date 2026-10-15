import aiosqlite
import logging
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)
DB_PATH = "bot_database.db"

async def init_db():
    """Инициализация базы данных с новой структурой"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                role TEXT DEFAULT 'user',
                ref_code TEXT UNIQUE,
                referred_by INTEGER,
                referrals_activated INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица типов тарифов (Default, Premium и т.д.)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tariff_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                squad_id TEXT NOT NULL,
                hwid_limit INTEGER DEFAULT 3,
                description TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица сроков и цен для каждого типа тарифа
        await db.execute("""
                    CREATE TABLE IF NOT EXISTS tariff_plans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tariff_type_id INTEGER NOT NULL,
                        days INTEGER NOT NULL,
                        price INTEGER NOT NULL,
                        traffic_limit_bytes BIGINT DEFAULT 0,  -- 0 = безлимит
                        is_active INTEGER DEFAULT 1,
                        FOREIGN KEY (tariff_type_id) REFERENCES tariff_types(id) ON DELETE CASCADE
                    )
                """)

        # Таблица подписок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                tariff_type_id INTEGER NOT NULL,
                tariff_plan_id INTEGER NOT NULL,
                short_uuid TEXT UNIQUE,
                username_in_panel TEXT UNIQUE,
                subscription_expiry TEXT,
                status TEXT DEFAULT 'ACTIVE',
                trial_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (tariff_type_id) REFERENCES tariff_types(id) ON DELETE RESTRICT,
                FOREIGN KEY (tariff_plan_id) REFERENCES tariff_plans(id) ON DELETE RESTRICT
            )
        """)

        # Таблица сбросов устройств
        await db.execute("""
            CREATE TABLE IF NOT EXISTS device_resets (
                subscription_id INTEGER PRIMARY KEY,
                last_reset TIMESTAMP,
                reset_count INTEGER DEFAULT 0,
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
                    CREATE TABLE IF NOT EXISTS payment_transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transaction_id TEXT UNIQUE NOT NULL,      -- UUID из Platega
                        telegram_id INTEGER NOT NULL,
                        plan_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        currency TEXT DEFAULT 'RUB',
                        status TEXT DEFAULT 'PENDING',            -- PENDING, CONFIRMED, CANCELED, CHARGEBACKED
                        payment_method INTEGER,
                        payload TEXT,                              -- наши данные (user_plan)
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        confirmed_at TIMESTAMP,
                        FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE,
                        FOREIGN KEY (plan_id) REFERENCES tariff_plans(id) ON DELETE RESTRICT
                    )
                """)

        await db.commit()
        await run_migrations(db)
        await create_default_tariffs(db)
        logger.info("✅ База данных инициализирована")


# ======================================================
# МИГРАЦИИ СХЕМЫ
# ======================================================
# Версия схемы хранится в PRAGMA user_version. Каждая миграция идемпотентна
# (сама проверяет, нужно ли ей что-то делать), поэтому безопасна и для боевой
# БД, где часть изменений уже была применена до появления версионирования.
# Чтобы изменить схему — добавьте функцию в конец _MIGRATIONS, не правя старые.

async def _column_names(db, table: str) -> set:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def _add_column(db, table: str, column: str, definition: str) -> bool:
    """Добавить колонку, если её ещё нет. Возвращает True, если добавили."""
    if column in await _column_names(db, table):
        return False
    await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


async def _m001_payment_activation(db):
    """Учёт реальной активации подписки по транзакции (v2.0.2)"""
    added = await _add_column(db, "payment_transactions", "activated_at", "TIMESTAMP")
    await _add_column(db, "payment_transactions", "activation_claimed_at", "TIMESTAMP")
    if added:
        # Все платежи, подтверждённые до миграции, считаем уже обработанными,
        # чтобы повторный callback по старой транзакции не продлил подписку ещё раз
        await db.execute("""
            UPDATE payment_transactions
            SET activated_at = COALESCE(confirmed_at, updated_at, CURRENT_TIMESTAMP)
            WHERE status = 'CONFIRMED'
        """)


async def _m002_tariff_full_description(db):
    """Полное описание тарифа (показывается на экране выбора срока)"""
    await _add_column(db, "tariff_types", "full_description", "TEXT")


async def _m003_trial_nudge(db):
    """Статус напоминания триальщику, который не подключился: NULL / sent / connected"""
    await _add_column(db, "subscriptions", "trial_nudge_status", "TEXT")


async def _m004_promo_codes(db):
    """Промокоды: коды, область действия, применения (по факту оплаты) и события для воронки"""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            kind TEXT NOT NULL DEFAULT 'regular',        -- regular | ad (рекламный, с учётом партнёра)
            title TEXT,
            discount_percent INTEGER NOT NULL DEFAULT 0,
            max_activations INTEGER,                      -- NULL = без лимита
            valid_until TEXT,                             -- ISO UTC, NULL = бессрочно
            first_payment_only INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            partner_name TEXT,
            commission_percent REAL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Пустая область действия = все планы
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_scope (
            promo_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            PRIMARY KEY (promo_id, plan_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            transaction_id TEXT NOT NULL UNIQUE,
            plan_id INTEGER,
            original_amount INTEGER NOT NULL,
            paid_amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_promo_redemptions_promo ON promo_redemptions(promo_id)")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            event TEXT NOT NULL,                          -- link | applied
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_promo_events_promo ON promo_events(promo_id, event)")
    await _add_column(db, "payment_transactions", "promo_id", "INTEGER")
    await _add_column(db, "payment_transactions", "original_amount", "INTEGER")


_MIGRATIONS = [
    (1, _m001_payment_activation),
    (2, _m002_tariff_full_description),
    (3, _m003_trial_nudge),
    (4, _m004_promo_codes),
]


async def run_migrations(db):
    async with db.execute("PRAGMA user_version") as cursor:
        current = (await cursor.fetchone())[0]

    for version, migration in _MIGRATIONS:
        if version <= current:
            continue
        await migration(db)
        await db.execute(f"PRAGMA user_version = {version}")
        await db.commit()
        logger.info(f"🧱 Применена миграция схемы БД #{version}: {migration.__name__}")


async def create_default_tariffs(db):
    """Создание дефолтных тарифов"""
    # Проверяем, есть ли уже типы тарифов
    async with db.execute("SELECT COUNT(*) FROM tariff_types") as cursor:
        count = await cursor.fetchone()
        if count[0] == 0:
            # Создаем тип "Default"
            await db.execute("""
                INSERT INTO tariff_types (name, squad_id, hwid_limit, description)
                VALUES (?, ?, ?, ?)
            """, ("Default", "REPLACE-WITH-YOUR-SQUAD-UUID", 3, "Стандартная подписка"))

            # Создаем тип "Premium"
            await db.execute("""
                INSERT INTO tariff_types (name, squad_id, hwid_limit, description)
                VALUES (?, ?, ?, ?)
            """, ("Premium", "REPLACE-WITH-YOUR-SQUAD-UUID", 5, "Премиум подписка"))

            # Получаем ID созданных типов
            async with db.execute("SELECT id FROM tariff_types WHERE name = ?", ("Default",)) as cursor:
                default_id = (await cursor.fetchone())[0]
            async with db.execute("SELECT id FROM tariff_types WHERE name = ?", ("Premium",)) as cursor:
                premium_id = (await cursor.fetchone())[0]

            # Добавляем планы для Default (безлимитный трафик = 0)
            plans_default = [
                (default_id, 30, 150, 0),   # безлимит
                (default_id, 90, 250, 0),   # безлимит
                (default_id, 180, 450, 0)   # безлимит
            ]

            # Добавляем планы для Premium (можно тоже безлимит или с лимитом)
            plans_premium = [
                (premium_id, 30, 300, 0),   # безлимит
                (premium_id, 90, 500, 0),   # безлимит
                (premium_id, 180, 900, 0)   # безлимит
            ]

            for plan in plans_default + plans_premium:
                await db.execute("""
                    INSERT INTO tariff_plans (tariff_type_id, days, price, traffic_limit_bytes)
                    VALUES (?, ?, ?, ?)
                """, plan)

            await db.commit()
            logger.info("✅ Дефолтные тарифы и планы созданы")
            logger.warning(
                "⚠️ У тарифов 'Default'/'Premium' сейчас заглушка вместо squad_id. "
                "Откройте админ-панель бота и укажите реальный squad_id из вашей "
                "Remnawave-панели, иначе выдача подписки не сработает."
            )

# Новые функции для работы с тарифами
async def get_all_tariff_types(active_only: bool = True) -> List[Dict[str, Any]]:
    """Получить все типы тарифов"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM tariff_types"
        if active_only:
            query += " WHERE is_active = 1"

        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_tariff_plans(tariff_type_id: int) -> List[Dict[str, Any]]:
    """Получить все планы для конкретного типа тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
                              SELECT *
                              FROM tariff_plans
                              WHERE tariff_type_id = ?
                                AND is_active = 1
                              ORDER BY days
                              """, (tariff_type_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_tariff_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о плане"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT tp.*, tt.name as tariff_name, tt.squad_id, tt.hwid_limit
            FROM tariff_plans tp
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE tp.id = ?
        """, (plan_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_tariff_type(type_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о типе тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM tariff_types WHERE id = ?",
                (type_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# ======================================================
# USERS
# ======================================================

async def generate_ref_code() -> str:
    return uuid.uuid4().hex[:8]


async def get_user(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,)
        ) as cursor:
            return await cursor.fetchone()


async def get_user_by_ref_code(ref_code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM users WHERE ref_code = ?",
                (ref_code,)
        ) as cursor:
            return await cursor.fetchone()


async def add_user(telegram_id: int, username: str, referred_by: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        ref_code = await generate_ref_code()

        await db.execute("""
                         INSERT
                         OR IGNORE INTO users 
            (telegram_id, username, ref_code, referred_by)
            VALUES (?, ?, ?, ?)
                         """, (telegram_id, username, ref_code, referred_by))

        await db.commit()


async def set_user_role(telegram_id: int, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?",
            (role, telegram_id)
        )
        await db.commit()


# ======================================================
# SUBSCRIPTIONS
# ======================================================


async def mark_subscription_trial_used(subscription_id: int):
    """Отметить подписку как использовавшую триал"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET trial_used = 1
            WHERE id = ?
        """, (subscription_id,))
        await db.commit()

async def get_user_subscriptions(telegram_id: int) -> List[Dict[str, Any]]:
    """Получить все подписки пользователя с информацией о тарифах"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tp.days as tariff_days,
                   tp.price as tariff_price
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.telegram_id = ?
            ORDER BY s.created_at DESC
        """, (telegram_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_subscription(subscription_id: int) -> Optional[Dict[str, Any]]:
    """Получить информацию о конкретной подписке"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tt.squad_id,
                   tp.days as tariff_days,
                   tp.price as tariff_price
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.id = ?
        """, (subscription_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_subscription_by_short_uuid(short_uuid: str) -> Optional[Dict[str, Any]]:
    """Получить подписку по short_uuid"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
                              SELECT s.*, t.name as tariff_name
                              FROM subscriptions s
                                       JOIN tariffs t ON s.tariff_id = t.id
                              WHERE s.short_uuid = ?
                              """, (short_uuid,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_user_subscription_by_tariff_type(telegram_id: int, tariff_type_id: int) -> Optional[Dict[str, Any]]:
    """Получить подписку пользователя на конкретный тип тарифа"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, tt.name as tariff_name
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            WHERE s.telegram_id = ? AND s.tariff_type_id = ?
        """, (telegram_id, tariff_type_id)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def create_subscription(
        telegram_id: int,
        tariff_type_id: int,  # было tariff_id
        tariff_plan_id: int,  # новый параметр
        short_uuid: str,
        expiry: str,
        username_in_panel: str,
        trial: bool = False
) -> int:
    """Создать новую подписку"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO subscriptions 
            (telegram_id, tariff_type_id, tariff_plan_id, short_uuid, subscription_expiry, username_in_panel, trial_used)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (telegram_id, tariff_type_id, tariff_plan_id, short_uuid, expiry, username_in_panel, 1 if trial else 0))

        await db.commit()
        return cursor.lastrowid


async def update_subscription_expiry(subscription_id: int, new_expiry: str):
    """Обновить дату окончания подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE subscriptions
                         SET subscription_expiry = ?
                         WHERE id = ?
                         """, (new_expiry, subscription_id))
        await db.commit()

async def update_subscription_plan(subscription_id: int, new_plan_id: int):
    """Обновить план подписки (например, при изменении лимита трафика)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE subscriptions
            SET tariff_plan_id = ?
            WHERE id = ?
        """, (new_plan_id, subscription_id))
        await db.commit()
        logger.info(f"✅ Subscription {subscription_id} plan updated to {new_plan_id}")


async def get_subscriptions_by_telegram_id(telegram_id: int) -> List[Dict[str, Any]]:
    """Получить все подписки пользователя по Telegram ID (для админки)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*,
                   tt.name as tariff_name,
                   tt.hwid_limit as tariff_hwid_limit,
                   tp.days as tariff_days,
                   tp.price as tariff_price,
                   tp.traffic_limit_bytes
            FROM subscriptions s
            JOIN tariff_types tt ON s.tariff_type_id = tt.id
            JOIN tariff_plans tp ON s.tariff_plan_id = tp.id
            WHERE s.telegram_id = ?
            ORDER BY s.created_at DESC
        """, (telegram_id,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_subscription_status(subscription_id: int, status: str):
    """Обновить статус подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE subscriptions
                         SET status = ?
                         WHERE id = ?
                         """, (status, subscription_id))
        await db.commit()


async def get_all_subscriptions() -> List[tuple]:
    """Получить все подписки (для reminder)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
                              SELECT id, telegram_id, subscription_expiry, status
                              FROM subscriptions
                              """) as cursor:
            return await cursor.fetchall()


# ======================================================
# REFERRALS
# ======================================================

async def increment_referral_activated(referrer_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
                         UPDATE users
                         SET referrals_activated = referrals_activated + 1
                         WHERE telegram_id = ?
                         """, (referrer_id,))
        await db.commit()


# ======================================================
# ADMIN FUNCTIONS
# ======================================================

async def is_admin(telegram_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT role FROM users WHERE telegram_id = ?",
                (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            from config.config import ADMIN_ID
            return (row and row[0] == "admin") or telegram_id == ADMIN_ID


async def get_all_admins():
    """Возвращает список всех администраторов"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT telegram_id, username FROM users WHERE role = 'admin'"
        ) as cursor:
            return await cursor.fetchall()


async def get_all_users():
    """Возвращает список всех пользователей (только telegram_id)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT telegram_id FROM users") as cursor:
            return await cursor.fetchall()


MAILING_AUDIENCES = {
    "all": "Все пользователи бота",
    "active": "С активной подпиской",
    "expired": "С истёкшей подпиской",
    "both": "С активной или истёкшей подпиской",
}


def _is_subscription_active(expiry: Optional[str], now: datetime) -> bool:
    if not expiry:
        return False
    try:
        return datetime.fromisoformat(expiry.replace("Z", "+00:00")) > now
    except ValueError:
        return False


async def get_mailing_recipients(audience: str) -> List[int]:
    """
    Получатели рассылки.
      all     — все пользователи (как и раньше, включая забаненных);
      active  — есть хотя бы одна действующая подписка;
      expired — подписки были, но ни одна уже не действует;
      both    — подписки есть/были (активные + истёкшие).
    Активность определяется по дате окончания, а не по полю status, которое
    может отставать (например, после ручного продления). Забаненные и
    пользователи без подписок в отфильтрованные группы не попадают.
    """
    if audience not in MAILING_AUDIENCES:
        raise ValueError(f"Неизвестная аудитория рассылки: {audience}")

    async with aiosqlite.connect(DB_PATH) as db:
        if audience == "all":
            async with db.execute("SELECT telegram_id FROM users") as cursor:
                return [row[0] for row in await cursor.fetchall()]

        async with db.execute("""
            SELECT u.telegram_id, s.subscription_expiry
            FROM users u
            JOIN subscriptions s ON s.telegram_id = u.telegram_id
            WHERE COALESCE(u.role, 'user') != 'banned' AND s.status != 'DISABLED'
        """) as cursor:
            rows = await cursor.fetchall()

    now = datetime.now().astimezone()
    has_active, has_any = set(), set()
    for telegram_id, expiry in rows:
        has_any.add(telegram_id)
        if _is_subscription_active(expiry, now):
            has_active.add(telegram_id)

    if audience == "active":
        return sorted(has_active)
    if audience == "expired":
        return sorted(has_any - has_active)
    return sorted(has_any)


# ======================================================
# НАПОМИНАНИЕ ТРИАЛЬЩИКАМ, КОТОРЫЕ НЕ ПОДКЛЮЧИЛИСЬ
# ======================================================

async def get_trial_nudge_candidates(min_age_hours: float, max_age_hours: float) -> List[Dict[str, Any]]:
    """
    Подписки-триалы, созданные от min_age до max_age часов назад, которым ещё не
    отправляли напоминание и у владельцев которых нет ни одной оплаты.
    (Триал, "навешенный" на уже существующую подписку, сюда не попадает: у неё
    created_at старый.)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.id, s.telegram_id, s.username_in_panel, s.subscription_expiry
            FROM subscriptions s
            WHERE s.trial_used = 1
              AND s.trial_nudge_status IS NULL
              AND s.status != 'DISABLED'
              AND s.created_at <= datetime('now', ?)
              AND s.created_at >= datetime('now', ?)
              AND NOT EXISTS (
                  SELECT 1 FROM payment_transactions p
                  WHERE p.telegram_id = s.telegram_id AND p.status = 'CONFIRMED'
              )
        """, (f"-{min_age_hours} hours", f"-{max_age_hours} hours")) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def set_trial_nudge_status(subscription_id: int, status: str):
    """status: 'sent' (напоминание отправлено) или 'connected' (уже пользуется, не беспокоим)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscriptions SET trial_nudge_status = ? WHERE id = ?",
            (status, subscription_id)
        )
        await db.commit()


# ======================================================
# DEVICE RESET FUNCTIONS (обновленные)
# ======================================================

async def can_reset_devices(subscription_id: int) -> tuple[bool, str]:
    """
    Проверяет, можно ли сбросить устройства для конкретной подписки
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT last_reset FROM device_resets WHERE subscription_id = ?",
                (subscription_id,)
        ) as cursor:
            row = await cursor.fetchone()

            if not row or not row[0]:
                return True, ""

            last_reset = datetime.fromisoformat(row[0])
            now = datetime.now()

            time_diff = now - last_reset
            if time_diff.total_seconds() >= 24 * 60 * 60:
                return True, ""
            else:
                hours_left = 24 - (time_diff.total_seconds() / 3600)
                hours = int(hours_left)
                minutes = int((hours_left - hours) * 60)

                if hours > 0:
                    wait_message = f"⏳ Следующий сброс будет доступен через {hours} ч. {minutes} мин."
                else:
                    wait_message = f"⏳ Следующий сброс будет доступен через {minutes} мин."

                return False, wait_message


async def record_device_reset(subscription_id: int):
    """Записывает время сброса устройств для конкретной подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.now().isoformat()

        await db.execute("""
                         INSERT INTO device_resets (subscription_id, last_reset, reset_count)
                         VALUES (?, ?, 1) ON CONFLICT(subscription_id) DO
                         UPDATE SET
                             last_reset = excluded.last_reset,
                             reset_count = reset_count + 1
                         """, (subscription_id, now))

        await db.commit()


async def get_last_reset_info(subscription_id: int) -> dict:
    """Получает информацию о последнем сбросе для подписки"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
                "SELECT last_reset, reset_count FROM device_resets WHERE subscription_id = ?",
                (subscription_id,)
        ) as cursor:
            row = await cursor.fetchone()

            if row:
                return {
                    "last_reset": row[0],
                    "reset_count": row[1]
                }
            return {
                "last_reset": None,
                "reset_count": 0
            }


# ======================================================
# ФУНКЦИИ ДЛЯ РАБОТЫ С ТРАНЗАКЦИЯМИ
# ======================================================

async def create_payment_transaction(
        transaction_id: str,
        telegram_id: int,
        plan_id: int,
        amount: int,
        payment_method: int,
        currency: str = "RUB",
        promo_id: Optional[int] = None,
        original_amount: Optional[int] = None
) -> int:
    """Создать запись о транзакции (amount — фактическая сумма к оплате, уже со скидкой по промокоду)"""
    payload = f"{telegram_id}_{plan_id}_{int(datetime.now().timestamp())}"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO payment_transactions
            (transaction_id, telegram_id, plan_id, amount, currency, payment_method, payload, status,
             promo_id, original_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """, (transaction_id, telegram_id, plan_id, amount, currency, payment_method, payload,
              promo_id, original_amount))

        await db.commit()
        return cursor.lastrowid


async def get_transaction(transaction_id: str) -> Optional[Dict[str, Any]]:
    """Получить информацию о транзакции по её ID"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT pt.*, tp.days, tp.price, tt.name as tariff_name, tt.squad_id, tt.hwid_limit
            FROM payment_transactions pt
            JOIN tariff_plans tp ON pt.plan_id = tp.id
            JOIN tariff_types tt ON tp.tariff_type_id = tt.id
            WHERE pt.transaction_id = ?
        """, (transaction_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_transaction_by_payload(payload: str) -> Optional[Dict[str, Any]]:
    """Получить транзакцию по payload (наши данные)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM payment_transactions WHERE payload = ?
        """, (payload,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_transaction_status(transaction_id: str, status: str):
    """Обновить статус транзакции"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions 
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (status, transaction_id))

        if status == "CONFIRMED":
            await db.execute("""
                UPDATE payment_transactions 
                SET confirmed_at = CURRENT_TIMESTAMP
                WHERE transaction_id = ?
            """, (transaction_id,))

        await db.commit()


async def claim_activation(transaction_id: str) -> bool:
    """
    Атомарно "занять" активацию транзакции. True — активировать должен именно
    этот вызов; False — уже активирована либо активируется другим вызовом.
    Зависший claim (старше 10 минут, например после падения бота) перехватывается.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE payment_transactions
            SET activation_claimed_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
              AND activated_at IS NULL
              AND (activation_claimed_at IS NULL
                   OR activation_claimed_at < datetime('now', '-10 minutes'))
        """, (transaction_id,))
        await db.commit()
        return cursor.rowcount == 1


async def mark_activated(transaction_id: str):
    """Отметить, что подписка по транзакции успешно активирована"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions
            SET activated_at = CURRENT_TIMESTAMP
            WHERE transaction_id = ?
        """, (transaction_id,))
        await db.commit()


async def release_activation_claim(transaction_id: str):
    """Снять claim после неудачной активации, чтобы её можно было повторить"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE payment_transactions
            SET activation_claimed_at = NULL
            WHERE transaction_id = ? AND activated_at IS NULL
        """, (transaction_id,))
        await db.commit()


async def get_pending_transactions() -> List[Dict[str, Any]]:
    """Получить все ожидающие транзакции (для проверки)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM payment_transactions 
            WHERE status = 'PENDING' 
            AND datetime(created_at) > datetime('now', '-1 day')
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

