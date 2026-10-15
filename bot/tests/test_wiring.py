"""Проверка, что все роутеры бота подключаются к диспетчеру так же, как в main.py."""
import asyncio

import _bootstrap  # noqa: F401  (путь к проекту и конфиг)

from aiogram import Dispatcher


async def main():
    from handlers import admin, backup, handlers
    from handlers.admin_promo import router as admin_promo_router
    from handlers.payment import router as payment_router
    from handlers.promo import router as promo_router

    dp = Dispatcher()
    for router in (backup.router, admin.router, promo_router, admin_promo_router, handlers.router, payment_router):
        dp.include_router(router)

    callback_handlers = sum(len(r.callback_query.handlers) for r in dp.sub_routers)
    message_handlers = sum(len(r.message.handlers) for r in dp.sub_routers)
    assert callback_handlers > 50 and message_handlers > 5, (callback_handlers, message_handlers)
    print(f"OK  роутеры подключаются как в main.py (callback-обработчиков: {callback_handlers}, message: {message_handlers})")


if __name__ == "__main__":
    asyncio.run(main())
