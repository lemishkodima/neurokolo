from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


@dataclass(frozen=True)
class TelegramContent:
    source_chat_id: int
    source_message_ids: list[int]
    buttons: list[list[dict[str, str]]]


def url_buttons_markup(
    buttons: list[list[dict[str, str]]],
) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=item["text"],
                    url=item["url"],
                    style=item.get("style"),
                )
                for item in row
            ]
            for row in buttons
        ]
    )


async def copy_telegram_content(
    bot: Bot,
    *,
    destination_chat_id: int,
    content: TelegramContent,
    extra_buttons: list[list[dict[str, str]]] | None = None,
) -> None:
    copied = await bot.copy_messages(
        chat_id=destination_chat_id,
        from_chat_id=content.source_chat_id,
        message_ids=content.source_message_ids,
    )
    markup = url_buttons_markup([*content.buttons, *(extra_buttons or [])])
    if markup and copied:
        await bot.edit_message_reply_markup(
            chat_id=destination_chat_id,
            message_id=copied[-1].message_id,
            reply_markup=markup,
        )
