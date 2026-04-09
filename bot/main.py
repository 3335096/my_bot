from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.config import settings
from bot.db import Database
from bot.handlers import build_router
from bot.logging_setup import setup_logging
from bot.openrouter_client import OpenRouterClient


logger = logging.getLogger(__name__)


async def run() -> None:
    setup_logging()

    db = Database(settings.database_url)
    await db.connect()
    await db.init_schema()

    llm = OpenRouterClient()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(build_router(db, llm))

    # Remove any stale webhook and drop updates queued while bot was offline
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Starting bot polling")
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        logger.info("Stopping bot polling")
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(run())
