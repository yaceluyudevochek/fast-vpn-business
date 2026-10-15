"""Логика промокодов: проверка применимости и расчёт цены со скидкой."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from database import promo_db

MSK = timezone(timedelta(hours=3))

# Сообщения пользователю (причины, по которым промокод нельзя применить)
MSG_NOT_FOUND = "Промокод не найден или больше не действует"
MSG_EXPIRED = "Срок действия промокода истёк"
MSG_EXHAUSTED = "Лимит активаций промокода исчерпан"
MSG_WRONG_PLAN = "Промокод не действует на этот тариф"
MSG_USED = "Ты уже использовал этот промокод"
MSG_FIRST_ONLY = "Промокод действует только на первую оплату"


def apply_discount(price: int, percent: int) -> int:
    """Цена со скидкой, округление до целых рублей (0.5 — вверх), не меньше 1 ₽"""
    if percent <= 0:
        return price
    discounted = (price * (100 - percent) * 2 + 100) // 200  # round-half-up без float
    return max(1, discounted)


def end_of_day_msk_to_utc_iso(day: datetime) -> str:
    """Дата, введённая админом (DD.MM.YYYY), включительно: до 23:59:59 МСК этого дня, в UTC"""
    end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=MSK)
    return end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_valid_until(valid_until: Optional[str]) -> str:
    if not valid_until:
        return "бессрочно"
    moment = datetime.fromisoformat(valid_until.replace("Z", "+00:00")).astimezone(MSK)
    return moment.strftime("%d.%m.%Y")


def normalize_code(raw: str) -> str:
    return raw.strip().upper()


@dataclass
class PromoCheck:
    ok: bool
    message: str = ""
    promo: Optional[Dict[str, Any]] = None
    original_price: int = 0
    final_price: int = 0

    @property
    def discount(self) -> int:
        return self.original_price - self.final_price


async def check_promo(code: str, telegram_id: int, plan: Dict[str, Any]) -> PromoCheck:
    """Можно ли применить промокод этим пользователем к этому плану, и итоговая цена"""
    price = int(plan["price"])
    promo = await promo_db.get_promo_by_code(normalize_code(code))
    if not promo or not promo["is_active"]:
        return PromoCheck(False, MSG_NOT_FOUND)

    if promo["valid_until"]:
        until = datetime.fromisoformat(promo["valid_until"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > until:
            return PromoCheck(False, MSG_EXPIRED, promo)

    scope = await promo_db.get_scope_plan_ids(promo["id"])
    if scope and plan["id"] not in scope:
        return PromoCheck(False, MSG_WRONG_PLAN, promo)

    if await promo_db.user_redeemed(promo["id"], telegram_id):
        return PromoCheck(False, MSG_USED, promo)

    if promo["first_payment_only"] and await promo_db.user_has_confirmed_payment(telegram_id):
        return PromoCheck(False, MSG_FIRST_ONLY, promo)

    limit = promo["max_activations"]
    if limit is not None:
        taken = await promo_db.count_redemptions(promo["id"]) \
            + await promo_db.count_pending_holds(promo["id"], exclude_user=telegram_id)
        if taken >= limit:
            return PromoCheck(False, MSG_EXHAUSTED, promo)

    return PromoCheck(
        True, promo=promo,
        original_price=price,
        final_price=apply_discount(price, promo["discount_percent"]),
    )
