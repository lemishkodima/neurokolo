from __future__ import annotations

import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from club_bot.models import Plan, TelegramResource


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Тарифи й доступ", callback_data="adm:plans"),
                InlineKeyboardButton(text="💬 Канали та групи", callback_data="adm:resources"),
            ],
            [
                InlineKeyboardButton(text="📣 Розсилки", callback_data="adm:broadcasts"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
            ],
            [
                InlineKeyboardButton(text="✏️ Тексти й кнопки", callback_data="adm:settings"),
                InlineKeyboardButton(text="👮 Адміністратори", callback_data="adm:admins"),
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплата WayForPay",
                    callback_data="adm:payments",
                )
            ],
        ]
    )


def back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Адмін-меню", callback_data="adm:home")]]
    )


def plans_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{'✅' if plan.is_active else '⏸'} {plan.name} · {plan.price} {plan.currency}"
                ),
                callback_data=f"adm:plan:{plan.id}",
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="➕ Створити тариф", callback_data="adm:plan_new")])
    rows.append(
        [InlineKeyboardButton(text="🗄 Видалені тарифи", callback_data="adm:plans_archived")]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_actions_keyboard(plan: Plan, *, can_archive: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Змінити назву",
                callback_data=f"adm:plan_name:{plan.id}",
            ),
            InlineKeyboardButton(
                text="💰 Змінити ціну",
                callback_data=f"adm:plan_price:{plan.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💬 Канали та групи",
                callback_data=f"adm:plan_resources:{plan.id}",
            )
        ],
    ]
    if can_archive:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🗑 Видалити тариф",
                    callback_data=f"adm:plan_delete:{plan.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← До тарифів", callback_data="adm:plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def archived_plans_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"↩️ {plan.name} · {plan.price} {plan.currency}",
                callback_data=f"adm:plan_restore:{plan.id}",
            )
        ]
        for plan in plans
    ]
    rows.append([InlineKeyboardButton(text="← До тарифів", callback_data="adm:plans")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_delete_confirm_keyboard(plan_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Так, видалити",
                    callback_data=f"adm:plan_del_yes:{plan_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Ні, повернутися",
                    callback_data=f"adm:plan:{plan_id}",
                )
            ],
        ]
    )


def plan_resources_keyboard(
    resources: list[tuple[TelegramResource, bool]],
    *,
    plan_id: uuid.UUID,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if selected else '▫️'} {resource.name}",
                callback_data=f"adm:resource:{resource.id}",
            )
        ]
        for resource, selected in resources
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="← До тарифу",
                    callback_data=f"adm:plan:{plan_id}",
                )
            ],
            [InlineKeyboardButton(text="Адмін-меню", callback_data="adm:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard() -> InlineKeyboardMarkup:
    fields = [
        ("Кнопка «Про клуб»", "button_about"),
        ("Кнопка «Доєднатися»", "button_join"),
        ("Кнопка «Моя підписка»", "button_subscription"),
        ("Кнопка «Матеріали»", "button_materials"),
        ("Кнопка «Техпідтримка»", "button_support"),
    ]
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm:setting:{key}")]
        for label, key in fields
    ]
    rows[0:0] = [
        [
            InlineKeyboardButton(
                text="👋 Стартове повідомлення",
                callback_data="adm:setting:welcome_text",
            )
        ],
        [
            InlineKeyboardButton(
                text="✅ Повідомлення після оплати",
                callback_data="adm:setting:payment_success_text",
            )
        ],
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="🖼 Текст, медіа та URL-кнопки",
                callback_data="adm:menu_content",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payments_keyboard(*, test_mode: bool) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(
            text="🔴 Вимкнути тестовий режим",
            callback_data="adm:payment_test_disable",
        )
        if test_mode
        else InlineKeyboardButton(
            text="🧪 Увімкнути тестовий режим",
            callback_data="adm:payment_test_enable",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle],
            [InlineKeyboardButton(text="← Адмін-меню", callback_data="adm:home")],
        ]
    )


def payment_test_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧪 Увімкнути на 30 хв",
                    callback_data="adm:payment_test_confirm",
                )
            ],
            [InlineKeyboardButton(text="Скасувати", callback_data="adm:payments")],
        ]
    )


def menu_content_keyboard(configured: set[str]) -> InlineKeyboardMarkup:
    actions = [
        ("Про клуб", "about"),
        ("Доєднатися", "join"),
        ("Моя підписка", "subscription"),
        ("Матеріали", "materials"),
        ("Техпідтримка", "support"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for label, action in actions:
        row = [
            InlineKeyboardButton(
                text=f"{'✅' if action in configured else '▫️'} {label}",
                callback_data=f"adm:menu_content_edit:{action}",
            )
        ]
        if action in configured:
            row.append(
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"adm:menu_content_clear:{action}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="← До налаштувань", callback_data="adm:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admins_keyboard(admins: list[tuple[int, bool]]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'🔒' if bootstrap else '❌'} {telegram_id}",
                callback_data="adm:noop" if bootstrap else f"adm:admin_remove:{telegram_id}",
            )
        ]
        for telegram_id, bootstrap in admins
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Додати адміністратора", callback_data="adm:admin_add")],
            [InlineKeyboardButton(text="← Назад", callback_data="adm:home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Створити розсилку", callback_data="adm:broadcast_new")],
            [
                InlineKeyboardButton(
                    text="🕓 Останні розсилки", callback_data="adm:broadcast_recent"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="adm:home")],
        ]
    )


def broadcast_target_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Усі користувачі", callback_data="adm:target:all_users")],
            [
                InlineKeyboardButton(
                    text="Лише активні підписники",
                    callback_data="adm:target:active_subscribers",
                )
            ],
        ]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Запустити", callback_data="adm:broadcast_send")],
            [InlineKeyboardButton(text="Скасувати", callback_data="adm:broadcast_cancel")],
        ]
    )


def resource_callback_id(value: str) -> uuid.UUID:
    return uuid.UUID(value)
