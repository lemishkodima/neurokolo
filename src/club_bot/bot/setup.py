import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat

logger = logging.getLogger(__name__)


def user_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Відкрити особистий кабінет"),
        BotCommand(command="subscription", description="Моя підписка"),
        BotCommand(command="materials", description="Матеріали клубу"),
        BotCommand(command="support", description="Техпідтримка"),
        BotCommand(command="help", description="Допомога"),
    ]


async def configure_admin_commands(bot: Bot, telegram_id: int) -> None:
    await bot.set_my_commands(
        [
            *user_commands(),
            BotCommand(command="admin", description="Адмін-панель"),
        ],
        scope=BotCommandScopeChat(chat_id=telegram_id),
    )


async def remove_admin_commands(bot: Bot, telegram_id: int) -> None:
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=telegram_id))


async def configure_bot(bot: Bot, admin_ids: Iterable[int]) -> None:
    commands = user_commands()
    await bot.set_my_commands(commands)
    for telegram_id in admin_ids:
        try:
            await configure_admin_commands(bot, telegram_id)
        except TelegramAPIError:
            logger.exception(
                "failed_to_configure_admin_commands",
                extra={"telegram_id": telegram_id},
            )
