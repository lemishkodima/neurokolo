from html import escape

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated

from club_bot.domain.enums import ResourceType
from club_bot.services.admin import AdminService, CatalogService

system_router = Router(name="system")


@system_router.my_chat_member()
async def track_bot_membership(
    event: ChatMemberUpdated,
    bot: Bot,
    catalog_service: CatalogService,
    admin_service: AdminService,
) -> None:
    if event.chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
        return
    active_statuses = {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    }
    active = event.new_chat_member.status in active_statuses
    was_active = event.old_chat_member.status in active_statuses
    resource_type = (
        ResourceType.CHANNEL if event.chat.type == ChatType.CHANNEL else ResourceType.SUPERGROUP
    )
    await catalog_service.register_resource(
        chat_id=event.chat.id,
        title=event.chat.title or str(event.chat.id),
        resource_type=resource_type,
        is_active=active,
    )
    if active and not was_active:
        resource_label = "каналу" if event.chat.type == ChatType.CHANNEL else "групи"
        title = escape(event.chat.title or str(event.chat.id))
        text = (
            f"✅ <b>Бота додано до {resource_label}</b>\n\n"
            f"<b>Назва:</b> {title}\n"
            f"<b>Telegram ID:</b> <code>{event.chat.id}</code>\n\n"
            "Ресурс автоматично збережено. Тепер його можна додати до тарифу в адмін-панелі."
        )
        for telegram_id, _is_bootstrap in await admin_service.list_admins():
            try:
                await bot.send_message(telegram_id, text)
            except TelegramAPIError:
                # An administrator may not have opened the bot yet or may have blocked it.
                continue
