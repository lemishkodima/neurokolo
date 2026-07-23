from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat


async def configure_bot(bot: Bot, admin_ids: list[int]) -> None:
    commands = [
        BotCommand(command="start", description="Відкрити особистий кабінет"),
        BotCommand(command="subscription", description="Моя підписка"),
        BotCommand(command="materials", description="Матеріали клубу"),
        BotCommand(command="support", description="Техпідтримка"),
        BotCommand(command="help", description="Допомога"),
    ]
    await bot.set_my_commands(commands)
    for telegram_id in admin_ids:
        await bot.set_my_commands(
            [*commands, BotCommand(command="admin", description="Адмін-панель")],
            scope=BotCommandScopeChat(chat_id=telegram_id),
        )
