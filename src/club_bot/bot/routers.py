from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import CallbackQuery, Message

from club_bot.bot.keyboards import (
    cancel_confirmation,
    checkout_plan_buttons,
    main_menu,
    resource_links,
    website_button,
)
from club_bot.config import Settings
from club_bot.domain.billing import billing_period_label
from club_bot.integrations.wayforpay import WayForPayError
from club_bot.services.access import AccessDeniedError, AccessService
from club_bot.services.admin import CatalogService, SettingsService
from club_bot.services.checkout_links import (
    add_query_parameter,
    create_personal_checkout_token,
)
from club_bot.services.landing_templates import LandingTemplateService
from club_bot.services.subscription_notifications import SubscriptionNotificationService
from club_bot.services.subscriptions import (
    CheckoutExpiredError,
    CheckoutNotFoundError,
    SubscriptionNotFoundError,
    SubscriptionService,
)
from club_bot.services.telegram_content import copy_telegram_content
from club_bot.services.users import UserService

router = Router(name="club")
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def start(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    landing_template_service: LandingTemplateService,
    subscription_service: SubscriptionService,
    settings_service: SettingsService,
    subscription_notification_service: SubscriptionNotificationService,
) -> None:
    if message.from_user is None:
        return
    argument = command.args or ""
    referral_code = argument.removeprefix("ref_") if argument.startswith("ref_") else None
    user = await user_service.upsert_telegram_user(
        message.from_user,
        referral_code=referral_code,
    )
    if argument.startswith("landing_"):
        await landing_template_service.record_start(
            user_id=user.id,
            slug=argument.removeprefix("landing_"),
        )
    labels = await settings_service.labels()

    if argument.startswith("claim_"):
        token = argument.removeprefix("claim_")
        try:
            result = await subscription_service.claim_checkout(token, message.from_user.id)
        except (CheckoutNotFoundError, CheckoutExpiredError):
            await message.answer(
                "Посилання на оплату недійсне або вже прострочене. "
                "Спробуйте оформити підписку ще раз.",
                reply_markup=main_menu(labels),
            )
            return
        if result.paid:
            await message.answer(
                "Особистий кабінет активовано.",
                reply_markup=main_menu(labels),
            )
            await subscription_notification_service.send_activated(message.from_user.id)
        else:
            await message.answer(
                "Акаунт прив’язано. Очікую підтвердження оплати від WayForPay.",
                reply_markup=main_menu(labels),
            )
        return

    await message.answer(await settings_service.get("welcome_text"), reply_markup=main_menu(labels))


@router.message(Command("about"))
async def about(message: Message, settings_service: SettingsService, bot: Bot) -> None:
    if not await _send_configured_content(message, "about", settings_service, bot):
        await message.answer(await settings_service.get("club_about_text"))


@router.message(F.text == "Доєднатися ✅")
async def join(
    message: Message,
    settings: Settings,
    access_service: AccessService,
    catalog_service: CatalogService,
    settings_service: SettingsService,
    bot: Bot,
) -> None:
    if message.from_user is None:
        return
    try:
        invites = await access_service.create_invites(message.from_user.id)
    except AccessDeniedError:
        invites = None

    if invites is not None:
        invite_buttons = [
            [{"text": invite.name, "url": invite.url, "style": "success"}]
            for invite in invites
        ]
        configured = await _send_configured_content(
            message,
            "join",
            settings_service,
            bot,
            extra_buttons=invite_buttons,
        )
        if invites:
            if not configured:
                await message.answer(
                    "Ваш доступ активний. Натисніть кнопку нижче та подайте заявку — "
                    "бот автоматично підтвердить лише ваш Telegram-акаунт.",
                    reply_markup=resource_links([(invite.name, invite.url) for invite in invites]),
                )
        else:
            await message.answer("Підписка активна, але для тарифу ще не додано каналів.")
        return

    await _offer_checkout(message, settings, catalog_service)


@router.message(Command("subscription"))
async def subscription_status(
    message: Message,
    subscription_service: SubscriptionService,
    settings_service: SettingsService,
    bot: Bot,
) -> None:
    if message.from_user is None:
        return
    await _send_configured_content(message, "subscription", settings_service, bot)
    subscriptions = await subscription_service.current_subscriptions_for_telegram_user(
        message.from_user.id
    )
    if not subscriptions:
        await message.answer("Активної підписки поки немає.")
        return
    blocks: list[str] = []
    multiple = len(subscriptions) > 1
    for index, subscription in enumerate(subscriptions, start=1):
        paid_until = (
            subscription.current_period_end.strftime("%d.%m.%Y %H:%M")
            if subscription.current_period_end
            else "—"
        )
        renewal = "увімкнено" if subscription.auto_renew_enabled else "вимкнено"
        status_details = {
            "active": "активна",
            "past_due": "очікує повторної оплати",
        }.get(subscription.status, subscription.status)
        recurring_details = {
            "active": "підтверджено WayForPay",
            "pending": "перевіряється",
            "created": "очікує активації WayForPay",
            "confirmed": "очікує активації WayForPay",
            "missing": "регулярний платіж не створено",
            "check_failed": "не вдалося перевірити",
            "suspended": "вимкнено",
            "removed": "видалено",
            "completed": "завершено",
            "not_applicable": "не застосовується",
        }.get(subscription.provider_recurring_status or "", "не підтверджено")
        title = f"<b>Підписка {index}</b>\n" if multiple else "<b>Моя підписка</b>\n"
        blocks.append(
            f"{title}"
            f"<b>Тариф:</b> {escape(subscription.plan_name)}\n"
            f"<b>Вартість:</b> {subscription.billing_amount:.2f} "
            f"{escape(subscription.billing_currency)}\n"
            f"<b>Період:</b> {billing_period_label(subscription.billing_months)}\n"
            f"<b>Статус:</b> {escape(status_details)}\n"
            f"<b>Доступ до:</b> {paid_until}\n"
            f"<b>Автопродовження:</b> {renewal}\n"
            f"<b>Статус WayForPay:</b> {recurring_details}"
        )
    await message.answer("\n\n".join(blocks))


@router.message(Command("materials"))
async def materials(
    message: Message,
    settings_service: SettingsService,
    bot: Bot,
) -> None:
    if message.from_user is None:
        return
    if not await _send_configured_content(message, "materials", settings_service, bot):
        await message.answer("Матеріали поки не налаштовані адміністратором.")


@router.message(F.text == "Скасувати підписку ❌")
async def ask_cancel(message: Message, subscription_service: SubscriptionService) -> None:
    if message.from_user is None:
        return
    subscription = await subscription_service.current_for_telegram_user(message.from_user.id)
    if subscription is None:
        await message.answer("Активної підписки немає.")
        return
    if subscription.cancel_at_period_end:
        await message.answer("Автопродовження вже вимкнене. Доступ збережено до кінця періоду.")
        return
    await message.answer(
        "Вимкнути наступне автоматичне списання? Доступ залишиться до кінця оплаченого періоду.",
        reply_markup=cancel_confirmation(),
    )


@router.callback_query(F.data == "subscription:cancel")
async def confirm_cancel(
    callback: CallbackQuery, subscription_service: SubscriptionService
) -> None:
    try:
        subscription = await subscription_service.cancel_for_telegram_user(callback.from_user.id)
    except SubscriptionNotFoundError:
        await callback.answer("Активної підписки немає", show_alert=True)
        return
    except WayForPayError:
        logger.exception(
            "WayForPay could not cancel the subscription for Telegram user %s",
            callback.from_user.id,
        )
        await callback.answer(
            "Не вдалося зв’язатися з оплатою. Спробуйте пізніше.", show_alert=True
        )
        return
    paid_until = (
        subscription.current_period_end.strftime("%d.%m.%Y %H:%M")
        if subscription.current_period_end
        else "кінця поточного періоду"
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Підписку скасовано. Наступного списання не буде, доступ діє до {paid_until}."
        )
    await callback.answer()


@router.callback_query(F.data == "subscription:keep")
async def keep_subscription(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Підписка залишається активною ✅")
    await callback.answer()


@router.message(Command("support"))
async def support(
    message: Message,
    settings: Settings,
    settings_service: SettingsService,
    bot: Bot,
) -> None:
    if not await _send_configured_content(message, "support", settings_service, bot):
        await message.answer(f"Підтримка: @{escape(settings.support_username)}")


@router.message(Command("help"))
async def help_command(message: Message, settings_service: SettingsService) -> None:
    labels = await settings_service.labels()
    await message.answer(
        "Використовуйте меню для керування підпискою, доступу до матеріалів і зв’язку "
        "з підтримкою.",
        reply_markup=main_menu(labels),
    )


@router.message(F.text)
async def dynamic_menu_action(
    message: Message,
    settings: Settings,
    settings_service: SettingsService,
    subscription_service: SubscriptionService,
    access_service: AccessService,
    catalog_service: CatalogService,
    bot: Bot,
) -> None:
    labels = await settings_service.labels()
    actions = {
        labels.about: "about",
        labels.join: "join",
        labels.subscription: "subscription",
        labels.materials: "materials",
        labels.support: "support",
    }
    match actions.get(message.text or ""):
        case "about":
            await about(message, settings_service, bot)
        case "join":
            await join(
                message,
                settings,
                access_service,
                catalog_service,
                settings_service,
                bot,
            )
        case "subscription":
            await subscription_status(message, subscription_service, settings_service, bot)
        case "materials":
            await materials(message, settings_service, bot)
        case "support":
            await support(message, settings, settings_service, bot)


async def _offer_checkout(
    message: Message,
    settings: Settings,
    catalog_service: CatalogService,
) -> None:
    if message.from_user is None:
        return
    plans = await catalog_service.list_plans(active=True)
    if not plans:
        await message.answer(
            "Наразі немає доступних тарифів. Зверніться до техпідтримки."
        )
        return
    owner_token = create_personal_checkout_token(
        message.from_user.id,
        settings.internal_api_key.get_secret_value(),
    )
    checkout_options: list[tuple[str, str]] = []
    for plan in plans:
        checkout_url = add_query_parameter(
            settings.membership_site_url,
            "owner",
            owner_token,
        )
        checkout_url = add_query_parameter(checkout_url, "plan_code", plan.code)
        label = (
            f"{plan.name} · {plan.price} {plan.currency} / "
            f"{billing_period_label(plan.billing_months)}"
        )
        if len(label) > 64:
            label = f"{label[:63]}…"
        checkout_options.append((label, checkout_url))

    if len(checkout_options) == 1:
        await message.answer(
            "Оформіть підписку на клуб. Після підтвердження WayForPay бот автоматично "
            "надішле повідомлення й персональні посилання доступу.",
            reply_markup=website_button(checkout_options[0][1]),
        )
        return
    await message.answer(
        "Оберіть тариф. Ціна вказана за весь період; після завершення цього періоду "
        "підписка автоматично продовжується на такий самий термін.",
        reply_markup=checkout_plan_buttons(checkout_options),
    )


async def _send_configured_content(
    message: Message,
    action: str,
    settings_service: SettingsService,
    bot: Bot,
    *,
    extra_buttons: list[list[dict[str, str]]] | None = None,
) -> bool:
    content = await settings_service.menu_content(action)
    if content is None:
        return False
    try:
        await copy_telegram_content(
            bot,
            destination_chat_id=message.chat.id,
            content=content,
            extra_buttons=extra_buttons,
        )
    except TelegramAPIError:
        logger.exception("Could not copy configured menu content for %s", action)
        return False
    return True
