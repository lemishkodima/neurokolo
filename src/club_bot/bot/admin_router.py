from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from club_bot.bot.admin_keyboards import (
    admin_menu,
    admins_keyboard,
    archived_plans_keyboard,
    back_to_admin,
    broadcast_confirm_keyboard,
    broadcast_menu,
    broadcast_target_keyboard,
    landing_template_actions_keyboard,
    landing_template_delete_confirm_keyboard,
    landing_templates_keyboard,
    menu_content_keyboard,
    payment_test_confirm_keyboard,
    payments_keyboard,
    plan_actions_keyboard,
    plan_delete_confirm_keyboard,
    plan_resources_keyboard,
    plans_keyboard,
    settings_keyboard,
)
from club_bot.bot.admin_states import (
    AdminAddStates,
    BroadcastCreateStates,
    LandingTemplateCreateStates,
    LandingTemplateEditStates,
    MenuContentStates,
    PlanCreateStates,
    PlanEditStates,
    SettingEditStates,
)
from club_bot.config import Settings
from club_bot.domain.enums import BroadcastTarget
from club_bot.models import LandingTemplate, Plan
from club_bot.services.admin import (
    AdminService,
    CatalogService,
    ProtectedPlanError,
    SettingsService,
)
from club_bot.services.broadcasts import BroadcastService
from club_bot.services.landing_templates import (
    MAX_HTML_TEMPLATE_BYTES,
    LandingTemplateError,
    LandingTemplateNotFoundError,
    LandingTemplateService,
)
from club_bot.services.stats import StatsService
from club_bot.services.telegram_content import (
    TelegramContent,
    copy_telegram_content,
    url_buttons_markup,
)

admin_router = Router(name="admin")
RICH_TEXT_SETTINGS = frozenset(
    {
        "club_about_text",
        "welcome_text",
        "payment_success_text",
    }
)
KYIV_TIMEZONE = ZoneInfo("Europe/Kyiv")


async def _authorized(event: Message | CallbackQuery, admin_service: AdminService) -> bool:
    if event.from_user is None:
        return False
    allowed = await admin_service.is_admin(event.from_user.id)
    if not allowed:
        if isinstance(event, CallbackQuery):
            await event.answer("Недостатньо прав", show_alert=True)
        else:
            await event.answer("Команда недоступна.")
    return allowed


def _plan_details(plan: Plan) -> str:
    return (
        f"<b>{escape(plan.name)}</b>\n"
        f"Ціна: <b>{plan.price} {plan.currency}</b> / місяць\n"
        f"Ресурсів: <b>{len(plan.resources)}</b>\n"
        f"Код: <code>{escape(plan.code)}</code>\n\n"
        "Оберіть, що потрібно змінити."
    )


def _landing_details(template: LandingTemplate, public_url: str) -> str:
    return (
        f"<b>{escape(template.name)}</b>\n"
        f"URL: <code>{escape(public_url)}</code>\n"
        f"Заголовок: <b>{escape(template.landing_title)}</b>\n"
        f"Канал: {escape(template.channel_title)}\n\n"
        f"{escape(template.landing_description)}"
    )


async def _validated_landing_text(message: Message, field: str) -> str | None:
    if not message.text:
        await message.answer("Надішліть значення текстом.")
        return None
    try:
        return LandingTemplateService.validate_field(field, message.text)
    except LandingTemplateError as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return None


async def _landing_html_from_message(message: Message, bot: Bot) -> str | None:
    if message.document is not None:
        file_name = message.document.file_name or ""
        if not file_name.casefold().endswith((".html", ".htm")):
            await message.answer("Надішліть документ із розширенням .html або .htm.")
            return None
        if (
            message.document.file_size is not None
            and message.document.file_size > MAX_HTML_TEMPLATE_BYTES
        ):
            await message.answer("HTML-файл перевищує 256 КБ.")
            return None
        destination = BytesIO()
        await bot.download(message.document, destination=destination)
        try:
            value = destination.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await message.answer("HTML-файл має бути збережений у кодуванні UTF-8.")
            return None
    elif message.text is not None:
        value = message.text
    else:
        await message.answer("Надішліть HTML як текст або як документ .html.")
        return None
    try:
        return LandingTemplateService.validate_html(value)
    except LandingTemplateError as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return None


@admin_router.message(Command("admin"))
async def open_admin(message: Message, admin_service: AdminService, state: FSMContext) -> None:
    if not await _authorized(message, admin_service):
        return
    await state.clear()
    await message.answer("<b>Адмін-панель клубу</b>", reply_markup=admin_menu())


@admin_router.callback_query(F.data == "adm:home")
async def admin_home(
    callback: CallbackQuery, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("<b>Адмін-панель клубу</b>", reply_markup=admin_menu())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:plans")
async def show_plans(
    callback: CallbackQuery, admin_service: AdminService, catalog_service: CatalogService
) -> None:
    if not await _authorized(callback, admin_service):
        return
    plans = await catalog_service.list_plans(active=True)
    text = "Тарифи визначають, до яких каналів і груп бот видає доступ."
    if not plans:
        text += "\n\nПоки немає жодного тарифу."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=plans_keyboard(plans))
    await callback.answer()


@admin_router.callback_query(F.data == "adm:plan_new")
async def new_plan(callback: CallbackQuery, admin_service: AdminService, state: FSMContext) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.set_state(PlanCreateStates.name)
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Введіть внутрішню назву тарифу:")
    await callback.answer()


@admin_router.message(PlanCreateStates.name)
async def plan_name(message: Message, admin_service: AdminService, state: FSMContext) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    await state.update_data(plan_name=message.text.strip())
    await state.set_state(PlanCreateStates.price)
    await message.answer("Введіть щомісячну ціну в UAH, наприклад <code>990</code>:")


@admin_router.message(PlanCreateStates.price)
async def plan_price(
    message: Message,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    try:
        price = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if price <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("Некоректна ціна. Введіть додатне число, наприклад 990.")
        return
    data = await state.get_data()
    plan = await catalog_service.create_plan(name=str(data["plan_name"]), price=price)
    await state.clear()
    await message.answer(
        f"✅ Тариф «{escape(plan.name)}» створено. Тепер виберіть для нього ресурси.",
        reply_markup=plans_keyboard(await catalog_service.list_plans(active=True)),
    )


@admin_router.callback_query(F.data.startswith("adm:plan:"))
async def select_plan(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    plan = await catalog_service.get_plan(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не знайдено", show_alert=True)
        return
    await state.update_data(selected_plan_id=str(plan_id))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _plan_details(plan),
            reply_markup=plan_actions_keyboard(
                plan,
                can_archive=plan.code != catalog_service.default_plan_code,
            ),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:plan_resources:"))
async def show_plan_resources(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    plan = await catalog_service.get_plan(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не знайдено", show_alert=True)
        return
    await state.update_data(selected_plan_id=str(plan_id))
    resources = await catalog_service.plan_resources(plan_id)
    text = f"Оберіть канали й групи для тарифу «{escape(plan.name)}»:"
    if not resources:
        text += "\n\nДодайте бота адміністратором до потрібного каналу або групи."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text,
            reply_markup=plan_resources_keyboard(resources, plan_id=plan_id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:resource:"))
async def toggle_plan_resource(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    data = await state.get_data()
    if "selected_plan_id" not in data:
        await callback.answer("Спочатку оберіть тариф", show_alert=True)
        return
    plan_id = uuid.UUID(str(data["selected_plan_id"]))
    resource_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    selected = await catalog_service.toggle_plan_resource(plan_id, resource_id)
    resources = await catalog_service.plan_resources(plan_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=plan_resources_keyboard(resources, plan_id=plan_id)
        )
    await callback.answer("Додано" if selected else "Прибрано")


@admin_router.callback_query(F.data.startswith("adm:plan_name:"))
async def edit_plan_name(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    plan = await catalog_service.get_plan(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(selected_plan_id=str(plan_id))
    await state.set_state(PlanEditStates.name)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Поточна назва: <b>{escape(plan.name)}</b>\n\nВведіть нову назву тарифу:"
        )
    await callback.answer()


@admin_router.message(PlanEditStates.name)
async def save_plan_name(
    message: Message,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    name = message.text.strip()
    if not name or len(name) > 255:
        await message.answer("Назва має містити від 1 до 255 символів.")
        return
    data = await state.get_data()
    plan = await catalog_service.update_plan(
        uuid.UUID(str(data["selected_plan_id"])),
        name=name,
    )
    await state.clear()
    if plan is None:
        await message.answer("Тариф не знайдено.", reply_markup=admin_menu())
        return
    await message.answer(
        "✅ Назву тарифу оновлено.",
        reply_markup=plan_actions_keyboard(
            plan,
            can_archive=plan.code != catalog_service.default_plan_code,
        ),
    )


@admin_router.callback_query(F.data.startswith("adm:plan_price:"))
async def edit_plan_price(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    plan = await catalog_service.get_plan(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не знайдено", show_alert=True)
        return
    await state.clear()
    await state.update_data(selected_plan_id=str(plan_id))
    await state.set_state(PlanEditStates.price)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Поточна ціна: <b>{plan.price} {plan.currency}</b>\n\n"
            "Введіть нову щомісячну ціну в UAH:"
        )
    await callback.answer()


@admin_router.message(PlanEditStates.price)
async def save_plan_price(
    message: Message,
    admin_service: AdminService,
    catalog_service: CatalogService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    try:
        price = Decimal(message.text.replace(",", ".")).quantize(Decimal("0.01"))
        if price <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("Некоректна ціна. Введіть додатне число, наприклад 990.")
        return
    data = await state.get_data()
    plan = await catalog_service.update_plan(
        uuid.UUID(str(data["selected_plan_id"])),
        price=price,
    )
    await state.clear()
    if plan is None:
        await message.answer("Тариф не знайдено.", reply_markup=admin_menu())
        return
    await message.answer(
        "✅ Ціну тарифу оновлено. Нові checkout використовуватимуть нову ціну; "
        "вже створені оплати й підписки не змінено.",
        reply_markup=plan_actions_keyboard(
            plan,
            can_archive=plan.code != catalog_service.default_plan_code,
        ),
    )


@admin_router.callback_query(F.data.startswith("adm:plan_delete:"))
async def confirm_plan_delete(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    plan = await catalog_service.get_plan(plan_id)
    if plan is None or not plan.is_active:
        await callback.answer("Тариф не знайдено", show_alert=True)
        return
    if plan.code == catalog_service.default_plan_code:
        await callback.answer(
            "Основний тариф не можна видалити. Його можна перейменувати та змінити ціну.",
            show_alert=True,
        )
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Видалити тариф «{escape(plan.name)}»?\n\n"
            "Він зникне з нових оплат, але історичні підписки та платежі збережуться.",
            reply_markup=plan_delete_confirm_keyboard(plan_id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:plan_del_yes:"))
async def delete_plan(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    try:
        removed = await catalog_service.archive_plan(plan_id)
    except ProtectedPlanError:
        await callback.answer("Основний тариф не можна видалити", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "✅ Тариф видалено." if removed else "Тариф не знайдено.",
            reply_markup=plans_keyboard(await catalog_service.list_plans(active=True)),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:plans_archived")
async def show_archived_plans(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    plans = await catalog_service.list_plans(active=False)
    text = "Видалені тарифи можна відновити без втрати історії."
    if not plans:
        text += "\n\nВидалених тарифів немає."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=archived_plans_keyboard(plans))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:plan_restore:"))
async def restore_plan(
    callback: CallbackQuery,
    admin_service: AdminService,
    catalog_service: CatalogService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    plan_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    restored = await catalog_service.restore_plan(plan_id)
    plans = await catalog_service.list_plans(active=False)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "✅ Тариф відновлено." if restored else "Тариф не знайдено.",
            reply_markup=archived_plans_keyboard(plans),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:resources")
async def show_resources(
    callback: CallbackQuery, admin_service: AdminService, catalog_service: CatalogService
) -> None:
    if not await _authorized(callback, admin_service):
        return
    resources = await catalog_service.list_resources()
    lines = ["<b>Збережені Telegram-ресурси</b>"]
    lines.extend(
        f"\n{'✅' if item.is_active else '⏸'} {escape(item.name)}\n<code>{item.chat_id}</code>"
        for item in resources
    )
    if not resources:
        lines.append(
            "\nДодайте бота адміністратором до каналу або групи — ресурс збережеться автоматично."
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text("".join(lines), reply_markup=back_to_admin())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:landings")
async def show_landing_templates(
    callback: CallbackQuery,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    templates = await landing_template_service.list_templates()
    text = (
        "<b>HTML-сторінки вступу</b>\n\n"
        "Кожна сторінка відкриває бота через персональний URL. Аватар завжди "
        "завантажується з поточного профілю Telegram-бота."
    )
    if not templates:
        text += "\n\nШаблонів поки немає."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text,
            reply_markup=landing_templates_keyboard(templates),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:landing_new")
async def new_landing_template(
    callback: CallbackQuery,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.clear()
    await state.set_state(LandingTemplateCreateStates.name)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Введіть внутрішню назву шаблону, наприклад <b>Instagram — липень</b>:"
        )
    await callback.answer()


@admin_router.message(LandingTemplateCreateStates.name)
async def landing_create_name(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    value = await _validated_landing_text(message, "name")
    if value is None:
        return
    await state.update_data(landing_name=value)
    await state.set_state(LandingTemplateCreateStates.slug)
    await message.answer(
        "Введіть slug для посилання, наприклад <code>instagram-july</code>.\n"
        "Дозволені малі латинські літери, цифри й дефіси."
    )


@admin_router.message(LandingTemplateCreateStates.slug)
async def landing_create_slug(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    value = await _validated_landing_text(message, "slug")
    if value is None:
        return
    await state.update_data(landing_slug=value)
    await state.set_state(LandingTemplateCreateStates.landing_title)
    await message.answer("Введіть головний заголовок сторінки:")


@admin_router.message(LandingTemplateCreateStates.landing_title)
async def landing_create_title(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    value = await _validated_landing_text(message, "landing_title")
    if value is None:
        return
    await state.update_data(landing_title=value)
    await state.set_state(LandingTemplateCreateStates.channel_title)
    await message.answer("Введіть назву каналу або проєкту:")


@admin_router.message(LandingTemplateCreateStates.channel_title)
async def landing_create_channel(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    value = await _validated_landing_text(message, "channel_title")
    if value is None:
        return
    await state.update_data(channel_title=value)
    await state.set_state(LandingTemplateCreateStates.landing_description)
    await message.answer("Введіть короткий опис сторінки:")


@admin_router.message(LandingTemplateCreateStates.landing_description)
async def landing_create_description(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    value = await _validated_landing_text(message, "landing_description")
    if value is None:
        return
    await state.update_data(landing_description=value)
    await state.set_state(LandingTemplateCreateStates.html_template)
    await message.answer(
        "Надішліть повний HTML-шаблон як документ <code>.html</code> (рекомендовано) "
        "або вставте його текстом.\n\n"
        "Доступні плейсхолдери:\n"
        "<code>{{landing_title}}</code>, <code>{{channel_title}}</code>, "
        "<code>{{landing_description}}</code>, <code>{{avatar_url}}</code>, "
        "<code>{{open_url}}</code>, <code>{{download_url}}</code>."
    )


@admin_router.message(LandingTemplateCreateStates.html_template)
async def landing_create_html(
    message: Message,
    bot: Bot,
    settings: Settings,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    html_template = await _landing_html_from_message(message, bot)
    if html_template is None or message.from_user is None:
        return
    data = await state.get_data()
    try:
        template = await landing_template_service.create(
            name=str(data["landing_name"]),
            slug=str(data["landing_slug"]),
            landing_title=str(data["landing_title"]),
            channel_title=str(data["channel_title"]),
            landing_description=str(data["landing_description"]),
            html_template=html_template,
            created_by_telegram_id=message.from_user.id,
        )
    except LandingTemplateError as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return
    await state.clear()
    public_url = f"{settings.landing_base_url}/join/{template.slug}"
    await message.answer(
        "✅ HTML-шаблон створено.\n\n" + _landing_details(template, public_url),
        reply_markup=landing_template_actions_keyboard(template, public_url=public_url),
    )


@admin_router.callback_query(F.data.startswith("adm:landing:"))
async def select_landing_template(
    callback: CallbackQuery,
    settings: Settings,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    template = await landing_template_service.get(uuid.UUID(callback.data.rsplit(":", 1)[1]))
    if template is None:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return
    public_url = f"{settings.landing_base_url}/join/{template.slug}"
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            _landing_details(template, public_url),
            reply_markup=landing_template_actions_keyboard(template, public_url=public_url),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:landing_edit:"))
async def edit_landing_template(
    callback: CallbackQuery,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    _adm, _landing_edit, code, raw_template_id = callback.data.split(":")
    template_id = uuid.UUID(raw_template_id)
    template = await landing_template_service.get(template_id)
    if template is None:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return
    fields = {
        "n": ("name", "внутрішню назву"),
        "s": ("slug", "slug"),
        "t": ("landing_title", "головний заголовок"),
        "c": ("channel_title", "назву каналу"),
        "d": ("landing_description", "опис"),
        "u": ("download_url", "HTTPS-посилання для завантаження Telegram"),
        "h": ("html_template", "HTML-шаблон"),
    }
    field, label = fields[code]
    await state.clear()
    await state.update_data(landing_template_id=str(template_id), landing_field=field)
    await state.set_state(LandingTemplateEditStates.value)
    if isinstance(callback.message, Message):
        if field == "html_template":
            prompt = (
                "Надішліть новий HTML як документ <code>.html</code> або текстом. "
                "Старий HTML буде замінено після успішної перевірки."
            )
        else:
            current = escape(str(getattr(template, field)))
            prompt = f"Поточне значення:\n<code>{current}</code>\n\nВведіть {label}:"
        await callback.message.edit_text(prompt)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:landing_stats:"))
async def show_landing_template_statistics(
    callback: CallbackQuery,
    settings: Settings,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    template = await landing_template_service.get(uuid.UUID(callback.data.rsplit(":", 1)[1]))
    if template is None:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return
    statistics = await landing_template_service.statistics(template.id)
    lines = [
        f"<b>📈 Джерело: {escape(template.name)}</b>",
        f"Slug: <code>{escape(template.slug)}</code>",
        "",
        f"Переходів у бот: <b>{statistics.total_starts}</b>",
        f"Унікальних користувачів: <b>{statistics.unique_users}</b>",
        f"Оплатили після переходу: <b>{statistics.paid_users}</b>",
        f"Конверсія: <b>{statistics.conversion_percent:.1f}%</b>",
        "",
        "<b>Останні переходи</b>",
    ]
    if statistics.recent_visitors:
        for visitor in statistics.recent_visitors:
            seen_at = visitor.seen_at.astimezone(KYIV_TIMEZONE)
            username = (
                f"@{escape(visitor.username)}"
                if visitor.username
                else f"<code>{visitor.telegram_id}</code>"
            )
            full_name = " ".join(
                part for part in (visitor.first_name, visitor.last_name) if part
            )
            lines.append(
                f"{seen_at:%d.%m %H:%M} · {username} · {escape(full_name)}"
            )
    else:
        lines.append("Переходів із цієї сторінки ще не було.")
    public_url = f"{settings.landing_base_url}/join/{template.slug}"
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=landing_template_actions_keyboard(
                template,
                public_url=public_url,
            ),
        )
    await callback.answer()


@admin_router.message(LandingTemplateEditStates.value)
async def save_landing_template_field(
    message: Message,
    bot: Bot,
    settings: Settings,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    field = str(data["landing_field"])
    if field == "html_template":
        value = await _landing_html_from_message(message, bot)
    else:
        value = await _validated_landing_text(message, field)
    if value is None:
        return
    try:
        template = await landing_template_service.update_field(
            uuid.UUID(str(data["landing_template_id"])),
            field=field,
            value=value,
        )
    except (LandingTemplateError, LandingTemplateNotFoundError) as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return
    await state.clear()
    public_url = f"{settings.landing_base_url}/join/{template.slug}"
    await message.answer(
        "✅ Шаблон оновлено.\n\n" + _landing_details(template, public_url),
        reply_markup=landing_template_actions_keyboard(template, public_url=public_url),
    )


@admin_router.callback_query(F.data.startswith("adm:landing_html:"))
async def download_landing_template_html(
    callback: CallbackQuery,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    template = await landing_template_service.get(uuid.UUID(callback.data.rsplit(":", 1)[1]))
    if template is None:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.answer_document(
            BufferedInputFile(
                template.html_template.encode("utf-8"),
                filename=f"{template.slug}.html",
            ),
            caption=f"HTML-шаблон «{escape(template.name)}»",
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:landing_delete:"))
async def confirm_landing_template_delete(
    callback: CallbackQuery,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    template_id = uuid.UUID(callback.data.rsplit(":", 1)[1])
    template = await landing_template_service.get(template_id)
    if template is None:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"Видалити HTML-шаблон «{escape(template.name)}»?\n\n"
            "Публічне посилання одразу перестане працювати.",
            reply_markup=landing_template_delete_confirm_keyboard(template_id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:landing_del_yes:"))
async def delete_landing_template(
    callback: CallbackQuery,
    admin_service: AdminService,
    landing_template_service: LandingTemplateService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    removed = await landing_template_service.delete(
        uuid.UUID(callback.data.rsplit(":", 1)[1])
    )
    templates = await landing_template_service.list_templates()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "✅ Шаблон видалено." if removed else "Шаблон не знайдено.",
            reply_markup=landing_templates_keyboard(templates),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:payments")
async def show_payments(
    callback: CallbackQuery,
    admin_service: AdminService,
    settings_service: SettingsService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    expires_at = await settings_service.payment_test_mode_until()
    if expires_at is None:
        text = (
            "<b>WayForPay: бойовий режим</b>\n\n"
            "Нові checkout використовують production-магазин і можуть списувати реальні кошти."
        )
    else:
        text = (
            "<b>🧪 WayForPay: тестовий режим</b>\n\n"
            f"Автоматично вимкнеться о <b>{expires_at:%H:%M UTC}</b>. "
            "Нові checkout використовують офіційний тестовий магазин WayForPay; "
            "реальні кошти не списуються."
        )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            text,
            reply_markup=payments_keyboard(test_mode=expires_at is not None),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:payment_test_enable")
async def confirm_payment_test_mode(
    callback: CallbackQuery,
    admin_service: AdminService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>Увімкнути тестові платежі?</b>\n\n"
            "Протягом 30 хвилин усі нові checkout будуть тестовими. Тестовий approved callback "
            "активує підписку та видає Telegram-доступ, щоб можна було перевірити весь сценарій. "
            "Не поширюйте платіжне посилання під час тесту.",
            reply_markup=payment_test_confirm_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:payment_test_confirm")
async def enable_payment_test_mode(
    callback: CallbackQuery,
    admin_service: AdminService,
    settings_service: SettingsService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    expires_at = await settings_service.enable_payment_test_mode()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>🧪 Тестовий режим увімкнено.</b>\n\n"
            f"Він автоматично вимкнеться о <b>{expires_at:%H:%M UTC}</b>. "
            "Відкрийте користувацьку кнопку «Доєднатися» та завершіть тестову оплату.",
            reply_markup=payments_keyboard(test_mode=True),
        )
    await callback.answer("Тестовий режим активний")


@admin_router.callback_query(F.data == "adm:payment_test_disable")
async def disable_payment_test_mode(
    callback: CallbackQuery,
    admin_service: AdminService,
    settings_service: SettingsService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await settings_service.disable_payment_test_mode()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "<b>WayForPay: бойовий режим</b>\n\n"
            "Тестовий режим вимкнено. Усі нові checkout використовують production-магазин.",
            reply_markup=payments_keyboard(test_mode=False),
        )
    await callback.answer("Бойовий режим активний")


@admin_router.callback_query(F.data == "adm:settings")
async def show_settings(callback: CallbackQuery, admin_service: AdminService) -> None:
    if not await _authorized(callback, admin_service):
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Оберіть текст або кнопку для редагування:", reply_markup=settings_keyboard()
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:setting:"))
async def edit_setting(
    callback: CallbackQuery, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    key = callback.data.rsplit(":", 1)[1]
    # Old admin-menu messages used this callback for the about text. Keep it
    # compatible and route it to the full rich-content editor.
    if key == "club_about_text":
        await _begin_menu_content_edit(callback, state, "about")
        await callback.answer()
        return
    await state.update_data(setting_key=key)
    await state.set_state(SettingEditStates.value)
    prompt = (
        "Надішліть новий текст повідомлення. Telegram-форматування буде збережене."
        if key in RICH_TEXT_SETTINGS
        else "Надішліть новий текст кнопки (до 64 символів)."
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(prompt)
    await callback.answer()


@admin_router.message(SettingEditStates.value)
async def save_setting(
    message: Message,
    admin_service: AdminService,
    settings_service: SettingsService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    key = str(data["setting_key"])
    rich_text = key in RICH_TEXT_SETTINGS
    plain_text = (message.text or "").strip()
    value = message.html_text if rich_text else plain_text
    limit = 4096 if rich_text else 64
    if not plain_text or len(plain_text) > limit:
        await message.answer("Значення порожнє або задовге. Спробуйте ще раз.")
        return
    await settings_service.set(key, value)
    await state.clear()
    await message.answer(
        "✅ Збережено. Новий текст застосовується одразу.", reply_markup=admin_menu()
    )


@admin_router.callback_query(F.data == "adm:menu_content")
async def show_menu_content(
    callback: CallbackQuery,
    admin_service: AdminService,
    settings_service: SettingsService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    configured = await _configured_menu_content(settings_service)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Оберіть кнопку, для якої потрібно налаштувати текст, медіа та URL-кнопки. "
            "✅ означає, що контент уже налаштовано.",
            reply_markup=menu_content_keyboard(configured),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:menu_content_edit:"))
async def edit_menu_content(
    callback: CallbackQuery,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    action = callback.data.rsplit(":", 1)[1]
    await _begin_menu_content_edit(callback, state, action)
    await callback.answer()


@admin_router.message(MenuContentStates.content, Command("done"))
async def menu_content_done(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    message_ids = list(set(data.get("menu_content_message_ids", [])))
    if not message_ids:
        await message.answer("Спочатку надішліть текст або медіа.")
        return
    if len(message_ids) > 100:
        await message.answer("Можна зберегти не більше 100 повідомлень.")
        return
    await state.set_state(MenuContentStates.buttons)
    await message.answer(
        "Додайте URL-кнопки у форматі:\n"
        "<code>Сайт - https://example.com (синій)</code>\n\n"
        "Кольори: <code>синій</code>, <code>зелений</code>, <code>червоний</code>. "
        "Колір можна не вказувати.\n"
        "Кожен рядок — окремий ряд кнопок, <code>;;</code> розділяє кнопки в одному рядку. "
        "Якщо вони не потрібні — /skip."
    )


@admin_router.message(MenuContentStates.content)
async def collect_menu_content(
    message: Message,
    admin_service: AdminService,
    state: FSMContext,
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    message_ids = list(data.get("menu_content_message_ids", []))
    message_ids.append(message.message_id)
    acknowledged_groups = set(data.get("menu_content_acknowledged_groups", []))
    media_group_id = message.media_group_id
    should_acknowledge = media_group_id is None or media_group_id not in acknowledged_groups
    if media_group_id is not None:
        acknowledged_groups.add(media_group_id)
    await state.update_data(
        menu_content_message_ids=message_ids,
        menu_content_source_chat_id=message.chat.id,
        menu_content_acknowledged_groups=sorted(acknowledged_groups),
    )
    if should_acknowledge:
        await message.answer(
            "✅ Контент отримано. Щоб зберегти його, введіть /done, а потім додайте "
            "URL-кнопки або введіть /skip."
        )


@admin_router.message(MenuContentStates.buttons, Command("skip"))
async def skip_menu_content_buttons(
    message: Message,
    admin_service: AdminService,
    settings_service: SettingsService,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await _authorized(message, admin_service):
        return
    await _save_menu_content(message, settings_service, state, bot, [])


@admin_router.message(MenuContentStates.buttons)
async def save_menu_content_buttons(
    message: Message,
    admin_service: AdminService,
    settings_service: SettingsService,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    try:
        buttons = _parse_buttons(message.text)
    except ValueError as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return
    await _save_menu_content(message, settings_service, state, bot, buttons)


@admin_router.callback_query(F.data.startswith("adm:menu_content_clear:"))
async def clear_menu_content(
    callback: CallbackQuery,
    admin_service: AdminService,
    settings_service: SettingsService,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    action = callback.data.rsplit(":", 1)[1]
    await settings_service.clear_menu_content(action)
    configured = await _configured_menu_content(settings_service)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=menu_content_keyboard(configured))
    await callback.answer("Контент видалено")


@admin_router.callback_query(F.data == "adm:admins")
async def show_admins(callback: CallbackQuery, admin_service: AdminService) -> None:
    if not await _authorized(callback, admin_service):
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "🔒 — головний адміністратор, його не можна видалити.\n"
            "Натисніть ❌ біля доданого адміністратора, щоб забрати доступ.",
            reply_markup=admins_keyboard(await admin_service.list_admins()),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:admin_add")
async def add_admin_start(
    callback: CallbackQuery, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.set_state(AdminAddStates.telegram_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Надішліть числовий Telegram ID нового адміністратора:")
    await callback.answer()


@admin_router.message(AdminAddStates.telegram_id)
async def add_admin_save(message: Message, admin_service: AdminService, state: FSMContext) -> None:
    if (
        not await _authorized(message, admin_service)
        or not message.text
        or message.from_user is None
    ):
        return
    try:
        telegram_id = int(message.text.strip())
        if telegram_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("ID має бути додатним числом.")
        return
    await admin_service.add_admin(telegram_id, added_by=message.from_user.id)
    await state.clear()
    await message.answer(
        f"✅ Адміністратора <code>{telegram_id}</code> додано.",
        reply_markup=admins_keyboard(await admin_service.list_admins()),
    )


@admin_router.callback_query(F.data.startswith("adm:admin_remove:"))
async def remove_admin(callback: CallbackQuery, admin_service: AdminService) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    removed = await admin_service.remove_admin(telegram_id)
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(
            reply_markup=admins_keyboard(await admin_service.list_admins())
        )
    await callback.answer("Видалено" if removed else "Не можна видалити")


@admin_router.callback_query(F.data == "adm:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@admin_router.callback_query(F.data == "adm:broadcasts")
async def broadcasts(callback: CallbackQuery, admin_service: AdminService) -> None:
    if not await _authorized(callback, admin_service):
        return
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Керування розсилками:", reply_markup=broadcast_menu())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:broadcast_new")
async def broadcast_new(
    callback: CallbackQuery, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.clear()
    await state.set_state(BroadcastCreateStates.content)
    await state.update_data(source_message_ids=[])
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Надішліть повідомлення або медіагрупу для розсилки. Форматування, caption і медіа "
            "будуть збережені. Коли все надіслано — введіть /done."
        )
    await callback.answer()


@admin_router.message(BroadcastCreateStates.content, Command("done"))
async def broadcast_content_done(
    message: Message, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    if not data.get("source_message_ids"):
        await message.answer("Спочатку надішліть повідомлення або медіагрупу.")
        return
    if len(set(data["source_message_ids"])) > 100:
        await message.answer("В одній розсилці може бути не більше 100 повідомлень.")
        return
    await state.set_state(BroadcastCreateStates.buttons)
    await message.answer(
        "За потреби надішліть кнопки у форматі:\n"
        "<code>Сайт | https://example.com</code>\n\n"
        "Кожен рядок — окремий ряд кнопок. Дві кнопки в одному рядку розділяйте <code>;;</code>. "
        "Якщо кнопки не потрібні — /skip."
    )


@admin_router.message(BroadcastCreateStates.content)
async def collect_broadcast_content(
    message: Message, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(message, admin_service):
        return
    data = await state.get_data()
    ids = list(data.get("source_message_ids", []))
    ids.append(message.message_id)
    await state.update_data(source_message_ids=ids, source_chat_id=message.chat.id)


@admin_router.message(BroadcastCreateStates.buttons, Command("skip"))
async def skip_broadcast_buttons(
    message: Message, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(message, admin_service):
        return
    await state.update_data(buttons=[])
    await state.set_state(BroadcastCreateStates.target)
    await message.answer("Кому надіслати розсилку?", reply_markup=broadcast_target_keyboard())


@admin_router.message(BroadcastCreateStates.buttons)
async def save_broadcast_buttons(
    message: Message, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(message, admin_service) or not message.text:
        return
    try:
        buttons = _parse_buttons(message.text)
    except ValueError as error:
        await message.answer(f"Помилка: {escape(str(error))}")
        return
    await state.update_data(buttons=buttons)
    await state.set_state(BroadcastCreateStates.target)
    await message.answer("Кому надіслати розсилку?", reply_markup=broadcast_target_keyboard())


@admin_router.callback_query(BroadcastCreateStates.target, F.data.startswith("adm:target:"))
async def choose_broadcast_target(
    callback: CallbackQuery,
    admin_service: AdminService,
    state: FSMContext,
    bot: Bot,
) -> None:
    if not await _authorized(callback, admin_service) or callback.data is None:
        return
    target = BroadcastTarget(callback.data.rsplit(":", 1)[1])
    await state.update_data(target=target.value)
    data = await state.get_data()
    copied = await bot.copy_messages(
        chat_id=callback.from_user.id,
        from_chat_id=int(data["source_chat_id"]),
        message_ids=sorted(set(data["source_message_ids"])),
    )
    buttons = data.get("buttons", [])
    if buttons and copied:
        await bot.edit_message_reply_markup(
            chat_id=callback.from_user.id,
            message_id=copied[-1].message_id,
            reply_markup=url_buttons_markup(buttons),
        )
    await state.set_state(BroadcastCreateStates.confirm)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Це попередній перегляд. Запустити розсилку?",
            reply_markup=broadcast_confirm_keyboard(),
        )
    await callback.answer()


@admin_router.callback_query(BroadcastCreateStates.confirm, F.data == "adm:broadcast_send")
async def send_broadcast(
    callback: CallbackQuery,
    admin_service: AdminService,
    broadcast_service: BroadcastService,
    state: FSMContext,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    data = await state.get_data()
    broadcast = await broadcast_service.queue(
        created_by_telegram_id=callback.from_user.id,
        source_chat_id=int(data["source_chat_id"]),
        source_message_ids=list(data["source_message_ids"]),
        buttons=list(data.get("buttons", [])),
        target=BroadcastTarget(str(data["target"])),
    )
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"✅ Розсилку поставлено в чергу для {broadcast.total_recipients} користувачів.",
            reply_markup=admin_menu(),
        )
    await callback.answer()


@admin_router.callback_query(F.data == "adm:broadcast_cancel")
async def cancel_broadcast(
    callback: CallbackQuery, admin_service: AdminService, state: FSMContext
) -> None:
    if not await _authorized(callback, admin_service):
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Розсилку скасовано.", reply_markup=admin_menu())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:broadcast_recent")
async def recent_broadcasts(
    callback: CallbackQuery,
    admin_service: AdminService,
    broadcast_service: BroadcastService,
) -> None:
    if not await _authorized(callback, admin_service):
        return
    recent = await broadcast_service.recent()
    lines = ["<b>Останні розсилки</b>"]
    lines.extend(
        f"\n{item.created_at:%d.%m %H:%M} · {item.status.value} · "
        f"{item.sent_count}/{item.total_recipients}"
        for item in recent
    )
    if not recent:
        lines.append("\nРозсилок ще немає.")
    if isinstance(callback.message, Message):
        await callback.message.edit_text("".join(lines), reply_markup=broadcast_menu())
    await callback.answer()


@admin_router.callback_query(F.data == "adm:stats")
async def stats(
    callback: CallbackQuery, admin_service: AdminService, stats_service: StatsService
) -> None:
    if not await _authorized(callback, admin_service):
        return
    report = await stats_service.render_html()
    if isinstance(callback.message, Message):
        await callback.message.answer_document(
            BufferedInputFile(report.encode(), filename="club-statistics.html"),
            caption="📊 Статистика клубу",
        )
    await callback.answer()


def _parse_buttons(text: str) -> list[list[dict[str, str]]]:
    styles = {
        "синій": "primary",
        "синя": "primary",
        "blue": "primary",
        "primary": "primary",
        "зелений": "success",
        "зелена": "success",
        "green": "success",
        "success": "success",
        "червоний": "danger",
        "червона": "danger",
        "red": "danger",
        "danger": "danger",
    }
    rows: list[list[dict[str, str]]] = []
    for raw_row in text.splitlines():
        if not raw_row.strip():
            continue
        row: list[dict[str, str]] = []
        for raw_button in raw_row.split(";;"):
            separator = "|" if "|" in raw_button else " - "
            parts = [item.strip() for item in raw_button.split(separator, maxsplit=1)]
            if len(parts) != 2 or not all(parts):
                raise ValueError(
                    "кожна кнопка повинна мати формат Назва - URL (колір)"
                )
            raw_url = parts[1]
            style: str | None = None
            if raw_url.endswith(")") and " (" in raw_url:
                raw_url, raw_style = raw_url.rsplit(" (", maxsplit=1)
                style_name = raw_style[:-1].strip().casefold()
                style = styles.get(style_name)
                if style is None:
                    raise ValueError(
                        "колір має бути: синій, зелений або червоний"
                    )
                raw_url = raw_url.strip()
            parsed = urlparse(raw_url)
            if parsed.scheme not in {"http", "https", "tg"}:
                raise ValueError("URL має починатися з https://, http:// або tg://")
            button = {"text": parts[0][:64], "url": raw_url}
            if style is not None:
                button["style"] = style
            row.append(button)
        rows.append(row)
    if not rows:
        raise ValueError("не знайдено жодної кнопки")
    return rows


async def _save_menu_content(
    message: Message,
    settings_service: SettingsService,
    state: FSMContext,
    bot: Bot,
    buttons: list[list[dict[str, str]]],
) -> None:
    data = await state.get_data()
    content = TelegramContent(
        source_chat_id=int(data["menu_content_source_chat_id"]),
        source_message_ids=sorted(set(data["menu_content_message_ids"])),
        buttons=buttons,
    )
    action = str(data["menu_content_action"])
    await settings_service.set_menu_content(action, content)
    await state.clear()
    await copy_telegram_content(bot, destination_chat_id=message.chat.id, content=content)
    configured = await _configured_menu_content(settings_service)
    await message.answer(
        "✅ Контент збережено. Вище — його попередній перегляд.",
        reply_markup=menu_content_keyboard(configured),
    )


async def _begin_menu_content_edit(
    callback: CallbackQuery,
    state: FSMContext,
    action: str,
) -> None:
    await state.clear()
    await state.set_state(MenuContentStates.content)
    await state.update_data(
        menu_content_action=action,
        menu_content_message_ids=[],
        menu_content_acknowledged_groups=[],
    )
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            "Надішліть текст, фото, відео або медіагрупу. Telegram-форматування і підписи "
            "будуть збережені.\n\n<b>Після надсилання обов’язково введіть /done.</b>"
        )


async def _configured_menu_content(settings_service: SettingsService) -> set[str]:
    actions = {"about", "join", "subscription", "materials", "support"}
    return {action for action in actions if await settings_service.menu_content(action) is not None}
