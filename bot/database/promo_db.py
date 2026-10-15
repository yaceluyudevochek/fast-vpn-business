"""Доступ к данным промокодов (таблицы создаёт миграция #4 в database/db.py)."""
from typing import Any, Dict, List, Optional

import aiosqlite

from database import db as core

# Сколько минут открытый (неоплаченный) счёт с промокодом занимает слот лимита активаций
PENDING_HOLD_MINUTES = 60

EDITABLE_FIELDS = {
    "title", "discount_percent", "max_activations", "valid_until",
    "first_payment_only", "is_active", "partner_name", "commission_percent",
}


def _connect():
    return aiosqlite.connect(core.DB_PATH)


async def create_promo(data: Dict[str, Any], plan_ids: List[int]) -> int:
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT INTO promo_codes
            (code, kind, title, discount_percent, max_activations, valid_until,
             first_payment_only, partner_name, commission_percent, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["code"], data.get("kind", "regular"), data.get("title"),
            data["discount_percent"], data.get("max_activations"), data.get("valid_until"),
            1 if data.get("first_payment_only") else 0,
            data.get("partner_name"), data.get("commission_percent"), data.get("created_by"),
        ))
        promo_id = cursor.lastrowid
        for plan_id in sorted(set(plan_ids)):
            await db.execute("INSERT INTO promo_scope (promo_id, plan_id) VALUES (?, ?)", (promo_id, plan_id))
        await db.commit()
        return promo_id


async def get_promo_by_code(code: str) -> Optional[Dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes WHERE code = ?", (code.strip(),)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_promo(promo_id: int) -> Optional[Dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM promo_codes WHERE id = ?", (promo_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def list_promos() -> List[Dict[str, Any]]:
    """Все промокоды с числом оплаченных применений"""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM promo_redemptions r WHERE r.promo_id = p.id) AS used
            FROM promo_codes p
            ORDER BY p.is_active DESC, p.id DESC
        """) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_scope_plan_ids(promo_id: int) -> List[int]:
    async with _connect() as db:
        async with db.execute("SELECT plan_id FROM promo_scope WHERE promo_id = ? ORDER BY plan_id", (promo_id,)) as cursor:
            return [row[0] for row in await cursor.fetchall()]


async def update_promo(promo_id: int, **fields):
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Нельзя менять поля: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    async with _connect() as db:
        await db.execute(f"UPDATE promo_codes SET {assignments} WHERE id = ?", (*fields.values(), promo_id))
        await db.commit()


async def count_redemptions(promo_id: int) -> int:
    async with _connect() as db:
        async with db.execute("SELECT COUNT(*) FROM promo_redemptions WHERE promo_id = ?", (promo_id,)) as cursor:
            return (await cursor.fetchone())[0]


async def count_pending_holds(promo_id: int, exclude_user: Optional[int] = None) -> int:
    """Пользователи с открытым неоплаченным счётом по этому промокоду (занимают слот лимита)"""
    async with _connect() as db:
        async with db.execute(f"""
            SELECT COUNT(DISTINCT telegram_id) FROM payment_transactions
            WHERE promo_id = ? AND status = 'PENDING'
              AND created_at >= datetime('now', '-{int(PENDING_HOLD_MINUTES)} minutes')
              AND telegram_id != ?
        """, (promo_id, exclude_user if exclude_user is not None else -1)) as cursor:
            return (await cursor.fetchone())[0]


async def user_redeemed(promo_id: int, telegram_id: int) -> bool:
    async with _connect() as db:
        async with db.execute(
                "SELECT 1 FROM promo_redemptions WHERE promo_id = ? AND telegram_id = ?",
                (promo_id, telegram_id)) as cursor:
            return await cursor.fetchone() is not None


async def user_has_confirmed_payment(telegram_id: int) -> bool:
    async with _connect() as db:
        async with db.execute(
                "SELECT 1 FROM payment_transactions WHERE telegram_id = ? AND status = 'CONFIRMED' LIMIT 1",
                (telegram_id,)) as cursor:
            return await cursor.fetchone() is not None


async def record_event(promo_id: int, telegram_id: int, event: str):
    """event: 'link' (открыл бота по ссылке с кодом) или 'applied' (код применён к плану)"""
    async with _connect() as db:
        await db.execute(
            "INSERT INTO promo_events (promo_id, telegram_id, event) VALUES (?, ?, ?)",
            (promo_id, telegram_id, event))
        await db.commit()


async def record_redemption(transaction: Dict[str, Any]) -> bool:
    """
    Зафиксировать применение промокода по оплаченной и активированной транзакции.
    Идемпотентно (transaction_id уникален). True — запись создана.
    """
    promo_id = transaction.get("promo_id")
    if not promo_id:
        return False
    paid = int(transaction["amount"])
    original = int(transaction.get("original_amount") or paid)
    async with _connect() as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO promo_redemptions
            (promo_id, telegram_id, transaction_id, plan_id, original_amount, paid_amount)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (promo_id, transaction["telegram_id"], transaction["transaction_id"],
              transaction.get("plan_id"), original, paid))
        await db.commit()
        return cursor.rowcount == 1


async def promo_report(promo_id: int) -> Dict[str, Any]:
    """Статистика по промокоду для расчёта заработка того, кто его распространял"""
    async with _connect() as db:
        async def one(query, params=()):
            async with db.execute(query, params) as cursor:
                return (await cursor.fetchone())[0] or 0

        report = {
            "link_users": await one(
                "SELECT COUNT(DISTINCT telegram_id) FROM promo_events WHERE promo_id = ? AND event = 'link'", (promo_id,)),
            "applied_users": await one(
                "SELECT COUNT(DISTINCT telegram_id) FROM promo_events WHERE promo_id = ? AND event = 'applied'", (promo_id,)),
            "payments": await one("SELECT COUNT(*) FROM promo_redemptions WHERE promo_id = ?", (promo_id,)),
            "paying_users": await one(
                "SELECT COUNT(DISTINCT telegram_id) FROM promo_redemptions WHERE promo_id = ?", (promo_id,)),
            "revenue": await one("SELECT SUM(paid_amount) FROM promo_redemptions WHERE promo_id = ?", (promo_id,)),
            "discount_given": await one(
                "SELECT SUM(original_amount - paid_amount) FROM promo_redemptions WHERE promo_id = ?", (promo_id,)),
            # Все оплаты привлечённых пользователей начиная с первой оплаты по коду
            # (в т.ч. последующие продления уже без кода) — для расчёта "пожизненного" дохода партнёра
            "lifetime_revenue": await one("""
                SELECT SUM(p.amount) FROM payment_transactions p
                JOIN (SELECT r.telegram_id, MIN(pt.created_at) AS first_at
                      FROM promo_redemptions r
                      JOIN payment_transactions pt ON pt.transaction_id = r.transaction_id
                      WHERE r.promo_id = ? GROUP BY r.telegram_id) f
                  ON f.telegram_id = p.telegram_id
                WHERE p.status = 'CONFIRMED' AND p.created_at >= f.first_at
            """, (promo_id,)),
        }
        return report
