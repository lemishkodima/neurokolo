from __future__ import annotations

import asyncio

import uvicorn

from club_bot.api import create_app
from club_bot.bot.setup import configure_bot
from club_bot.config import get_settings
from club_bot.container import build_container


def run_api() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )


async def _polling() -> None:
    settings = get_settings()
    container = build_container(settings)
    await container.bot.delete_webhook(drop_pending_updates=False)
    await configure_bot(container.bot, settings.admin_telegram_ids)
    try:
        await container.dispatcher.start_polling(
            container.bot,
            settings=container.settings,
            user_service=container.user_service,
            subscription_service=container.subscription_service,
            access_service=container.access_service,
            admin_service=container.admin_service,
            catalog_service=container.catalog_service,
            settings_service=container.settings_service,
            broadcast_service=container.broadcast_service,
            stats_service=container.stats_service,
        )
    finally:
        await container.close()


def run_polling() -> None:
    asyncio.run(_polling())


if __name__ == "__main__":
    run_api()
