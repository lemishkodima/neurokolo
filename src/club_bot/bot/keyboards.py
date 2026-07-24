from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from club_bot.services.admin import MenuLabels


def main_menu(labels: MenuLabels | None = None) -> ReplyKeyboardMarkup:
    labels = labels or MenuLabels(
        about="Про клуб 💎",
        join="Доєднатися ✅",
        subscription="Моя підписка",
        materials="Матеріали 📚",
        support="Техпідтримка ⚙️",
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=labels.about), KeyboardButton(text=labels.join)],
            [
                KeyboardButton(text=labels.subscription),
                KeyboardButton(text=labels.materials),
            ],
            [
                KeyboardButton(text="Скасувати підписку ❌"),
                KeyboardButton(text=labels.support),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Оберіть дію",
    )


def website_button(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Перейти до оформлення", url=url)],
        ]
    )


def checkout_plan_buttons(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=url)]
            for label, url in items
        ]
    )


def cancel_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Так, скасувати", callback_data="subscription:cancel"),
                InlineKeyboardButton(text="Залишити", callback_data="subscription:keep"),
            ]
        ]
    )


def resource_links(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=name, url=url)] for name, url in items]
    )
