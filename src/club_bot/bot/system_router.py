import logging
from html import escape

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from club_bot.domain.enums import ResourceType
from club_bot.services.access import AccessService
from club_bot.services.admin import AdminService, CatalogService

system_router = Router(name="system")
logger = logging.getLogger(__name__)


@system_router.chat_join_request()
async def review_join_request(
    event: ChatJoinRequest,
    access_service: AccessService,
) -> None:
    approved = await access_service.handle_join_request(
        chat_id=event.chat.id,
        telegram_id=event.from_user.id,
        invite_link=event.invite_link.invite_link if event.invite_link is not None else None,
    )
    logger.info(
        "Telegram join request %s for user %s in chat %s",
        "approved" if approved else "declined",
        event.from_user.id,
        event.chat.id,
    )


@system_router.my_chat_member()
async def track_bot_membership(
    event: ChatMemberUpdated,
    bot: Bot,
    catalog_service: CatalogService,
    admin_service: AdminService,
) -> None:
    if event.chat.type not in {ChatType.CHANNEL, ChatType.SUPERGROUP}:
        return
    active = event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR
    was_active = event.old_chat_member.status == ChatMemberStatus.ADMINISTRATOR
    resource_type = (
        ResourceType.CHANNEL if event.chat.type == ChatType.CHANNEL else ResourceType.SUPERGROUP
    )
    if active:
        await catalog_service.register_resource(
            chat_id=event.chat.id,
            title=event.chat.title or str(event.chat.id),
            resource_type=resource_type,
            is_active=True,
        )
    elif was_active:
        await catalog_service.deactivate_resource(event.chat.id)
    else:
        return

    if active == was_active:
        return
    resource_label = "каналу" if event.chat.type == ChatType.CHANNEL else "групи"
    title = escape(event.chat.title or str(event.chat.id))
    if active:
        text = (
            f"✅ <b>Бота додано до {resource_label}</b>\n\n"
            f"<b>Назва:</b> {title}\n"
            f"<b>Telegram ID:</b> <code>{event.chat.id}</code>\n\n"
            "Ресурс автоматично збережено. Тепер його можна додати до тарифу в адмін-панелі."
        )
    else:
        text = (
            f"⚠️ <b>Бот більше не адміністратор {resource_label}</b>\n\n"
            f"<b>Назва:</b> {title}\n"
            f"<b>Telegram ID:</b> <code>{event.chat.id}</code>\n\n"
            "Ресурс деактивовано й видалено з усіх тарифів. Нові посилання доступу "
            "створюватися не будуть. Щоб повернути ресурс, знову додайте бота "
            "адміністратором, а потім додайте канал або групу до потрібного тарифу."
        )
    for telegram_id, _is_bootstrap in await admin_service.list_admins():
        try:
            await bot.send_message(telegram_id, text)
        except TelegramAPIError:
            # An administrator may not have opened the bot yet or may have blocked it.
            continue
