from __future__ import annotations

import asyncio
import logging

from club_bot.config import get_settings
from club_bot.container import build_container

logger = logging.getLogger(__name__)


async def _worker() -> None:
    settings = get_settings()
    container = build_container(settings)
    try:
        while True:
            did_broadcast_work = False
            try:
                count = await container.access_service.expire_due(
                    grace_period_hours=settings.payment_grace_period_hours
                )
                if count:
                    logger.info("Revoked %s expired subscriptions", count)
            except Exception:
                logger.exception("Subscription expiration cycle failed")
            try:
                did_broadcast_work = await container.broadcast_service.process_batch()
            except Exception:
                logger.exception("Broadcast delivery cycle failed")
            await asyncio.sleep(1 if did_broadcast_work else settings.worker_interval_seconds)
    finally:
        await container.close()


def run() -> None:
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(_worker())


if __name__ == "__main__":
    run()
