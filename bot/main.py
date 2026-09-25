import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiohttp import web

from config.config import (
    TOKEN,
    PANEL_API_URL,
    PANEL_API_KEY,
    WEBHOOK_HOST,
    WEBHOOK_PORT,
    PLATEGA_WEBHOOK_URL
)
from database.db import init_db
from handlers import handlers, admin
from handlers.payment import router as payment_router  # Новый роутер
from services.panel_api import PanelAPI
from services.platega_api import PlategaAPI
from services.payment_webhook import init_webhook, handle_platega_callback
from services.reminder import SubscriptionReminder
from handlers import backup  # Импортируем новый модуль

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def init_web_server():
    """Инициализация веб-сервера для callback'ов"""
    app = web.Application()
    app.router.add_post('/webhook/platega', handle_platega_callback)
    return app


async def main():
    # Инициализация БД с новой структурой
    await init_db()

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(backup.router)
    # Инициализация API панели
    panel_api = PanelAPI(PANEL_API_URL, PANEL_API_KEY)

    # Инициализация API Platega (для использования в хендлерах)
    # platega_api = PlategaAPI(PLATEGA_MERCHANT_ID, PLATEGA_SECRET_KEY, PLATEGA_API_URL)

    # Передаем panel_api в webhook-обработчик
    init_webhook(panel_api)

    # Инициализация и запуск напоминалки
    reminder = SubscriptionReminder(bot, check_interval=60)
    await reminder.start()

    # Регистрация роутеров
    dp.include_router(admin.router)
    dp.include_router(handlers.router)
    dp.include_router(payment_router)  # Новый роутер для платежей

    # Запуск веб-сервера для callback'ов
    web_app = await init_web_server()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()

    logging.info(f"🌐 Веб-сервер для callback'ов запущен на {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    logging.info(f"🔗 Callback URL: {PLATEGA_WEBHOOK_URL}")
    logging.info("🚀 Бот запущен с поддержкой платежей Platega.io")

    try:
        await dp.start_polling(bot)
    finally:
        await reminder.stop()
        await panel_api.close()
        # await platega_api.close()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("👋 Бот остановлен")