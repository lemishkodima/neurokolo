from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from club_bot.services.access import AccessService
from club_bot.services.admin import SettingsService

logger = logging.getLogger(__name__)


class SubscriptionNotificationService:
    def __init__(
        self,
        bot: Bot,
        access_service: AccessService,
        settings_service: SettingsService,
    ) -> None:
        self.bot = bot
        self.access_service = access_service
        self.settings_service = settings_service

    async def send_activated(self, telegram_id: int) -> bool:
        try:
            invites = await self.access_service.create_invites(telegram_id)
        except Exception:
            logger.exception("Could not create subscription invites for %s", telegram_id)
            invites = []

        markup = None
        if invites:
            multiple = len(invites) > 1
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(f"{invite.name} 💎" if multiple else "Доєднатися 💎"),
                            url=invite.url,
                        )
                    ]
                    for invite in invites
                ]
            )
        text = await self.settings_service.get("payment_success_text")
        if invites:
            text += "\n\nНатисніть кнопку нижче, щоб доєднатися. Посилання персональне."
        else:
            text += "\n\nВідкрити доступ можна через кнопку «Матеріали»."
        try:
            await self.bot.send_message(telegram_id, text, reply_markup=markup)
        except TelegramAPIError:
            logger.exception("Could not notify Telegram user %s about activation", telegram_id)
            return False
        return True
