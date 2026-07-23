from __future__ import annotations

from dataclasses import dataclass

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from club_bot.bot.admin_router import admin_router
from club_bot.bot.routers import router
from club_bot.bot.system_router import system_router
from club_bot.config import Settings
from club_bot.db import create_engine, create_session_factory
from club_bot.integrations.wayforpay import WayForPayClient
from club_bot.services.access import AccessService
from club_bot.services.admin import AdminService, CatalogService, SettingsService
from club_bot.services.broadcasts import BroadcastService
from club_bot.services.stats import StatsService
from club_bot.services.subscription_notifications import SubscriptionNotificationService
from club_bot.services.subscriptions import SubscriptionService
from club_bot.services.users import UserService


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    http_client: httpx.AsyncClient
    bot: Bot
    dispatcher: Dispatcher
    wayforpay: WayForPayClient
    user_service: UserService
    subscription_service: SubscriptionService
    access_service: AccessService
    admin_service: AdminService
    catalog_service: CatalogService
    settings_service: SettingsService
    broadcast_service: BroadcastService
    stats_service: StatsService
    subscription_notification_service: SubscriptionNotificationService

    def workflow_data(self) -> dict[str, object]:
        return {
            "settings": self.settings,
            "user_service": self.user_service,
            "subscription_service": self.subscription_service,
            "access_service": self.access_service,
            "admin_service": self.admin_service,
            "catalog_service": self.catalog_service,
            "settings_service": self.settings_service,
            "broadcast_service": self.broadcast_service,
            "stats_service": self.stats_service,
            "subscription_notification_service": self.subscription_notification_service,
        }

    async def close(self) -> None:
        await self.bot.session.close()
        await self.http_client.aclose()
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = create_engine(
        settings.database_url,
        echo=settings.environment == "development" and settings.log_level == "DEBUG",
    )
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient(timeout=15)
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(system_router)
    dispatcher.include_router(admin_router)
    dispatcher.include_router(router)
    wayforpay = WayForPayClient(
        merchant_account=settings.wayforpay_merchant_account,
        merchant_domain=settings.wayforpay_merchant_domain,
        secret_key=settings.wayforpay_secret_key.get_secret_value(),
        merchant_password=settings.wayforpay_merchant_password.get_secret_value(),
        api_url=settings.wayforpay_api_url,
        checkout_url=settings.wayforpay_checkout_url,
        http_client=http_client,
    )
    user_service = UserService(session_factory)
    subscription_service = SubscriptionService(
        session_factory,
        wayforpay,
        bot_username=settings.bot_username,
        service_url=settings.wayforpay_service_url,
        default_return_url=settings.membership_site_url,
    )
    access_service = AccessService(
        session_factory,
        bot,
        invite_ttl_seconds=settings.invite_ttl_seconds,
        grace_period_hours=settings.payment_grace_period_hours,
    )
    admin_service = AdminService(session_factory, settings.admin_telegram_ids)
    catalog_service = CatalogService(session_factory, default_plan_code=settings.default_plan_code)
    settings_service = SettingsService(session_factory)
    broadcast_service = BroadcastService(
        session_factory,
        bot,
        batch_size=settings.broadcast_batch_size,
    )
    stats_service = StatsService(session_factory)
    subscription_notification_service = SubscriptionNotificationService(bot, access_service)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        http_client=http_client,
        bot=bot,
        dispatcher=dispatcher,
        wayforpay=wayforpay,
        user_service=user_service,
        subscription_service=subscription_service,
        access_service=access_service,
        admin_service=admin_service,
        catalog_service=catalog_service,
        settings_service=settings_service,
        broadcast_service=broadcast_service,
        stats_service=stats_service,
        subscription_notification_service=subscription_notification_service,
    )
