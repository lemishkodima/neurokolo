from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.bot.admin_router import _parse_buttons
from club_bot.bot.routers import join, materials, start
from club_bot.bot.system_router import track_bot_membership
from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import (
    BroadcastStatus,
    BroadcastTarget,
    DeliveryStatus,
    PaymentStatus,
    ResourceType,
    SubscriptionStatus,
)
from club_bot.domain.rules import utc_now
from club_bot.models import (
    Base,
    Broadcast,
    BroadcastRecipient,
    Payment,
    Plan,
    Subscription,
    TelegramResource,
    User,
    plan_resources,
)
from club_bot.services.access import AccessDeniedError, ResourceInvite
from club_bot.services.admin import (
    AdminService,
    CatalogService,
    ProtectedPlanError,
    SettingsService,
)
from club_bot.services.broadcasts import BroadcastService
from club_bot.services.checkout_links import verify_personal_checkout_token
from club_bot.services.landing_templates import LandingTemplateError, LandingTemplateService
from club_bot.services.stats import StatsService
from club_bot.services.telegram_content import TelegramContent, url_buttons_markup


async def _database(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return create_session_factory(engine)


async def test_admin_access_and_dynamic_settings(tmp_path: Path) -> None:
    session_factory = await _database(tmp_path)
    admins = AdminService(session_factory, [402152266])
    settings = SettingsService(session_factory)

    assert await admins.is_admin(402152266) is True
    assert await admins.is_admin(123) is False
    await admins.add_admin(123, added_by=402152266)
    assert await admins.is_admin(123) is True
    assert await admins.remove_admin(402152266) is False
    assert await admins.remove_admin(123) is True

    assert (await settings.labels()).about == "Про клуб 💎"
    await settings.set("button_about", "Про спільноту")
    assert (await settings.labels()).about == "Про спільноту"
    assert "Ласкаво просимо" in await settings.get("welcome_text")
    await settings.set("welcome_text", "<b>Новий старт</b>")
    await settings.set("payment_success_text", "Оплата успішна")
    assert await settings.get("welcome_text") == "<b>Новий старт</b>"
    assert await settings.get("payment_success_text") == "Оплата успішна"

    assert await settings.payment_test_mode_active() is False
    expires_at = await settings.enable_payment_test_mode()
    assert await settings.payment_test_mode_active() is True
    assert await settings.payment_test_mode_until() == expires_at
    await settings.disable_payment_test_mode()
    assert await settings.payment_test_mode_active() is False

    content = TelegramContent(
        source_chat_id=402152266,
        source_message_ids=[12, 11, 12],
        buttons=[[{"text": "Сайт", "url": "https://example.com"}]],
    )
    await settings.set_menu_content("about", content)
    stored_content = await settings.menu_content("about")
    assert stored_content is not None
    assert stored_content.source_message_ids == [11, 12]
    assert stored_content.buttons == content.buttons
    await settings.clear_menu_content("about")
    assert await settings.menu_content("about") is None


async def test_resources_are_registered_and_assigned_to_plan(tmp_path: Path) -> None:
    session_factory = await _database(tmp_path)
    catalog = CatalogService(session_factory, default_plan_code="club")
    resource = await catalog.register_resource(
        chat_id=-100123,
        title="Community",
        resource_type=ResourceType.SUPERGROUP,
        is_active=True,
    )
    plan = await catalog.create_plan(name="Club", price=Decimal("990"))
    assert plan.code == "club"
    assert await catalog.toggle_plan_resource(plan.id, resource.id) is True
    assigned = await catalog.plan_resources(plan.id)
    assert [(item.chat_id, selected) for item, selected in assigned] == [(-100123, True)]
    assert await catalog.toggle_plan_resource(plan.id, resource.id) is False


async def test_plan_edit_archive_restore_and_default_protection(tmp_path: Path) -> None:
    session_factory = await _database(tmp_path)
    catalog = CatalogService(session_factory, default_plan_code="club")
    default_plan = await catalog.create_plan(name="Club", price=Decimal("990"))
    second_plan = await catalog.create_plan(name="VIP", price=Decimal("1490"))

    updated = await catalog.update_plan(
        second_plan.id,
        name="VIP Plus",
        price=Decimal("1990.00"),
        billing_months=3,
    )
    assert updated is not None
    assert updated.name == "VIP Plus"
    assert updated.price == Decimal("1990.00")
    assert updated.billing_months == 3

    assert await catalog.archive_plan(second_plan.id) is True
    assert [item.id for item in await catalog.list_plans(active=True)] == [default_plan.id]
    assert [item.id for item in await catalog.list_plans(active=False)] == [second_plan.id]
    assert await catalog.restore_plan(second_plan.id) is True
    assert {item.id for item in await catalog.list_plans(active=True)} == {
        default_plan.id,
        second_plan.id,
    }

    with pytest.raises(ProtectedPlanError):
        await catalog.archive_plan(default_plan.id)

    async with session_factory() as session:
        assert await session.get(Plan, second_plan.id) is not None


async def test_landing_template_crud_validation_and_safe_rendering(tmp_path: Path) -> None:
    session_factory = await _database(tmp_path)
    service = LandingTemplateService(session_factory)
    html = """<!doctype html>
<html><body>
<h1>{{landing_title}}</h1>
<img src="{{avatar_url}}" alt="{{channel_title}}">
<p>{{landing_description}}</p>
<a href="{{open_url}}">Telegram</a>
<a href="{{download_url}}">Download</a>
</body></html>"""
    template = await service.create(
        name="Instagram",
        slug="instagram-july",
        landing_title="Neurokolo",
        channel_title="Club",
        landing_description="Daily lessons",
        html_template=html,
        created_by_telegram_id=402152266,
    )

    assert [item.id for item in await service.list_templates()] == [template.id]
    assert (await service.get_by_slug("instagram-july")).id == template.id  # type: ignore[union-attr]

    updated = await service.update_field(
        template.id,
        field="landing_title",
        value='New <title> "today"',
    )
    rendered = service.render(
        updated,
        avatar_url="data:image/jpeg;base64,YXZhdGFy",
        open_url="https://t.me/club_bot?start=landing_instagram-july",
    )
    assert "New &lt;title&gt; &quot;today&quot;" in rendered
    assert "data:image/jpeg;base64,YXZhdGFy" in rendered
    assert "{{" not in rendered

    async with session_factory() as session, session.begin():
        first_user = User(
            telegram_id=501,
            username="first",
            first_name="First",
            referral_code="LANDING-FIRST",
        )
        second_user = User(
            telegram_id=502,
            first_name="Second",
            referral_code="LANDING-SECOND",
        )
        plan = Plan(code="landing-plan", name="Landing plan", price=Decimal("100"))
        session.add_all([first_user, second_user, plan])
        await session.flush()
        first_user_id = first_user.id
        second_user_id = second_user.id
        plan_id = plan.id

    assert await service.record_start(user_id=first_user_id, slug=template.slug) is True
    assert await service.record_start(user_id=first_user_id, slug=template.slug) is True
    assert await service.record_start(user_id=second_user_id, slug=template.slug) is True
    assert await service.record_start(user_id=second_user_id, slug="missing") is False

    async with session_factory() as session, session.begin():
        subscription = Subscription(
            user_id=first_user_id,
            plan_id=plan_id,
            status=SubscriptionStatus.ACTIVE,
            billing_amount=Decimal("100"),
            billing_currency="UAH",
        )
        session.add(subscription)
        await session.flush()
        session.add(
            Payment(
                subscription_id=subscription.id,
                provider_event_id="landing-approved",
                order_reference="LANDING-APPROVED",
                amount=Decimal("100"),
                currency="UAH",
                status=PaymentStatus.APPROVED,
                paid_at=utc_now() + timedelta(minutes=1),
                provider_payload={},
            )
        )

    statistics = await service.statistics(template.id)
    assert statistics.total_starts == 3
    assert statistics.unique_users == 2
    assert statistics.paid_users == 1
    assert statistics.conversion_percent == 50.0
    visitor_ids = [visitor.telegram_id for visitor in statistics.recent_visitors]
    assert len(visitor_ids) == 3
    assert visitor_ids.count(501) == 2
    assert visitor_ids.count(502) == 1

    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><script>alert(1)</script>{{open_url}}</html>")
    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><a href='{{unknown_url}}'>Open</a></html>")
    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><p>No Telegram link</p></html>")

    assert await service.delete(template.id) is True
    assert await service.get(template.id) is None


async def test_start_records_landing_source() -> None:
    user_id = uuid.uuid4()

    class FakeUserService:
        async def upsert_telegram_user(
            self, _telegram_user: TelegramUser, *, referral_code: str | None
        ) -> SimpleNamespace:
            assert referral_code is None
            return SimpleNamespace(id=user_id)

    class FakeLandingService:
        def __init__(self) -> None:
            self.recorded: list[tuple[uuid.UUID, str]] = []

        async def record_start(self, *, user_id: uuid.UUID, slug: str) -> bool:
            self.recorded.append((user_id, slug))
            return True

    class FakeSettingsService:
        async def labels(self) -> SimpleNamespace:
            return SimpleNamespace(
                about="About",
                join="Join",
                subscription="Subscription",
                materials="Materials",
                support="Support",
            )

        async def get(self, key: str) -> str:
            assert key == "welcome_text"
            return "Welcome"

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = TelegramUser(id=501, is_bot=False, first_name="First")
            self.answers: list[str] = []

        async def answer(self, text: str, **_kwargs: object) -> None:
            self.answers.append(text)

    landing_service = FakeLandingService()
    message = FakeMessage()
    await start(
        message,  # type: ignore[arg-type]
        SimpleNamespace(args="landing_instagram-july"),  # type: ignore[arg-type]
        FakeUserService(),  # type: ignore[arg-type]
        landing_service,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    assert landing_service.recorded == [(user_id, "instagram-july")]
    assert message.answers == ["Welcome"]


async def test_join_button_contains_signed_personal_checkout_owner() -> None:
    class FakeAccessService:
        async def create_invites(self, _telegram_id: int) -> list[ResourceInvite]:
            raise AccessDeniedError

    class FakeSettingsService:
        async def menu_content(self, _action: str) -> None:
            return None

    class FakeCatalogService:
        async def list_plans(self, *, active: bool) -> list[SimpleNamespace]:
            assert active is True
            return [
                SimpleNamespace(
                    code="club",
                    name="Клуб",
                    price=Decimal("990"),
                    currency="UAH",
                    billing_months=1,
                )
            ]

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = TelegramUser(id=501, is_bot=False, first_name="Member")
            self.chat = SimpleNamespace(id=501)
            self.reply_markup: object | None = None

        async def answer(self, _text: str, *, reply_markup: object) -> None:
            self.reply_markup = reply_markup

    settings = SimpleNamespace(
        membership_site_url="https://neurokolo.com/club",
        internal_api_key=SecretStr("internal-secret"),
    )
    message = FakeMessage()

    await join(
        message,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        FakeAccessService(),  # type: ignore[arg-type]
        FakeCatalogService(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert message.reply_markup is not None
    button_url = message.reply_markup.inline_keyboard[0][0].url  # type: ignore[union-attr]
    assert button_url is not None
    owner_token = parse_qs(urlsplit(button_url).query)["owner"][0]
    assert verify_personal_checkout_token(owner_token, "internal-secret") == 501
    assert parse_qs(urlsplit(button_url).query)["plan_code"] == ["club"]


async def test_join_offers_every_active_plan_when_multiple_are_available() -> None:
    class FakeAccessService:
        async def create_invites(self, _telegram_id: int) -> list[ResourceInvite]:
            raise AccessDeniedError

    class FakeCatalogService:
        async def list_plans(self, *, active: bool) -> list[SimpleNamespace]:
            assert active is True
            return [
                SimpleNamespace(
                    code="monthly",
                    name="Щомісячний",
                    price=Decimal("990"),
                    currency="UAH",
                    billing_months=1,
                ),
                SimpleNamespace(
                    code="quarter",
                    name="Квартальний",
                    price=Decimal("2490"),
                    currency="UAH",
                    billing_months=3,
                ),
            ]

    class FakeSettingsService:
        async def menu_content(self, _action: str) -> None:
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = TelegramUser(id=501, is_bot=False, first_name="Member")
            self.chat = SimpleNamespace(id=501)
            self.text = ""
            self.reply_markup: object | None = None

        async def answer(self, text: str, *, reply_markup: object) -> None:
            self.text = text
            self.reply_markup = reply_markup

    settings = SimpleNamespace(
        membership_site_url="https://api.neurokolo.com/checkout",
        internal_api_key=SecretStr("internal-secret"),
    )
    message = FakeMessage()

    await join(
        message,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        FakeAccessService(),  # type: ignore[arg-type]
        FakeCatalogService(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert "Оберіть тариф" in message.text
    assert message.reply_markup is not None
    buttons = message.reply_markup.inline_keyboard  # type: ignore[union-attr]
    assert [row[0].text for row in buttons] == [
        "Щомісячний · 990 UAH / 1 місяць",
        "Квартальний · 2490 UAH / 3 місяці",
    ]
    urls = [row[0].url for row in buttons]
    assert [parse_qs(urlsplit(url).query)["plan_code"][0] for url in urls if url] == [
        "monthly",
        "quarter",
    ]
    owner_tokens = [parse_qs(urlsplit(url).query)["owner"][0] for url in urls if url]
    assert len(set(owner_tokens)) == 1
    assert verify_personal_checkout_token(owner_tokens[0], "internal-secret") == 501


async def test_join_with_subscription_sends_configured_content_and_invite() -> None:
    class MessageId:
        message_id = 900

    class FakeAccessService:
        async def create_invites(self, telegram_id: int) -> list[ResourceInvite]:
            assert telegram_id == 501
            return [ResourceInvite(name="Закритий канал", url="https://t.me/+personal")]

    class FakeSettingsService:
        async def menu_content(self, action: str) -> TelegramContent:
            assert action == "join"
            return TelegramContent(
                source_chat_id=100,
                source_message_ids=[10],
                buttons=[
                    [
                        {
                            "text": "Правила",
                            "url": "https://example.com/rules",
                            "style": "primary",
                        }
                    ]
                ],
            )

    class FakeBot:
        def __init__(self) -> None:
            self.markup: object | None = None

        async def copy_messages(
            self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
        ) -> list[MessageId]:
            assert (chat_id, from_chat_id, message_ids) == (501, 100, [10])
            return [MessageId()]

        async def edit_message_reply_markup(
            self, *, chat_id: int, message_id: int, reply_markup: object
        ) -> None:
            assert (chat_id, message_id) == (501, 900)
            self.markup = reply_markup

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = TelegramUser(id=501, is_bot=False, first_name="Member")
            self.chat = SimpleNamespace(id=501)

        async def answer(self, _text: str, **_kwargs: object) -> None:
            pytest.fail("Configured paid content must be copied instead of a fallback answer")

    bot = FakeBot()
    await join(
        FakeMessage(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeAccessService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        bot,  # type: ignore[arg-type]
    )

    assert bot.markup is not None
    keyboard = bot.markup.inline_keyboard  # type: ignore[union-attr]
    assert keyboard[0][0].text == "Правила"
    assert keyboard[0][0].style == "primary"
    assert keyboard[1][0].text == "Закритий канал"
    assert keyboard[1][0].url == "https://t.me/+personal"
    assert keyboard[1][0].style == "success"


async def test_join_with_subscription_without_resources_reports_configuration() -> None:
    class FakeAccessService:
        async def create_invites(self, telegram_id: int) -> list[ResourceInvite]:
            assert telegram_id == 501
            return []

    class FakeSettingsService:
        async def menu_content(self, action: str) -> None:
            assert action == "join"
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = TelegramUser(id=501, is_bot=False, first_name="Member")
            self.chat = SimpleNamespace(id=501)
            self.answers: list[str] = []

        async def answer(self, text: str, **_kwargs: object) -> None:
            self.answers.append(text)

    message = FakeMessage()
    await join(
        message,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeAccessService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert message.answers == ["Підписка активна, але для тарифу ще не додано каналів."]


async def test_materials_only_sends_admin_content() -> None:
    class FakeSettingsService:
        async def menu_content(self, action: str) -> TelegramContent:
            assert action == "materials"
            return TelegramContent(
                source_chat_id=100,
                source_message_ids=[20],
                buttons=[[{"text": "Конспект", "url": "https://example.com/notes"}]],
            )

    class FakeBot:
        async def copy_messages(
            self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
        ) -> list[SimpleNamespace]:
            assert (chat_id, from_chat_id, message_ids) == (501, 100, [20])
            return [SimpleNamespace(message_id=901)]

        async def edit_message_reply_markup(
            self, *, chat_id: int, message_id: int, reply_markup: object
        ) -> None:
            assert (chat_id, message_id) == (501, 901)
            assert reply_markup.inline_keyboard[0][0].text == "Конспект"  # type: ignore[attr-defined]

    message = SimpleNamespace(
        from_user=TelegramUser(id=501, is_bot=False, first_name="Member"),
        chat=SimpleNamespace(id=501),
    )
    await materials(
        message,  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        FakeBot(),  # type: ignore[arg-type]
    )


async def test_new_channel_notifies_all_admins(tmp_path: Path) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str) -> None:
            self.messages.append((chat_id, text))

    session_factory = await _database(tmp_path)
    admins = AdminService(session_factory, [402152266])
    await admins.add_admin(123, added_by=402152266)
    catalog = CatalogService(session_factory, default_plan_code="club")
    bot = FakeBot()
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100777, title="Уроки <VIP>", type=ChatType.CHANNEL),
        old_chat_member=SimpleNamespace(status=ChatMemberStatus.LEFT),
        new_chat_member=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
    )

    await track_bot_membership(event, bot, catalog, admins)  # type: ignore[arg-type]

    assert [chat_id for chat_id, _text in bot.messages] == [123, 402152266]
    assert all("Уроки &lt;VIP&gt;" in text for _chat_id, text in bot.messages)
    async with session_factory() as session:
        resource = await session.scalar(
            select(TelegramResource).where(TelegramResource.chat_id == -100777)
        )
    assert resource is not None and resource.is_active is True


async def test_removed_admin_deactivates_resource_and_notifies_all_admins(
    tmp_path: Path,
) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, chat_id: int, text: str) -> None:
            self.messages.append((chat_id, text))

    session_factory = await _database(tmp_path)
    admins = AdminService(session_factory, [402152266])
    await admins.add_admin(123, added_by=402152266)
    catalog = CatalogService(session_factory, default_plan_code="club")
    resource = await catalog.register_resource(
        chat_id=-100888,
        title="Спільнота <VIP>",
        resource_type=ResourceType.SUPERGROUP,
        is_active=True,
    )
    plan = await catalog.create_plan(name="Club", price=Decimal("990"))
    assert await catalog.toggle_plan_resource(plan.id, resource.id) is True
    bot = FakeBot()
    event = SimpleNamespace(
        chat=SimpleNamespace(
            id=-100888,
            title="Спільнота <VIP>",
            type=ChatType.SUPERGROUP,
        ),
        old_chat_member=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
        new_chat_member=SimpleNamespace(status=ChatMemberStatus.MEMBER),
    )

    await track_bot_membership(event, bot, catalog, admins)  # type: ignore[arg-type]

    assert [chat_id for chat_id, _text in bot.messages] == [123, 402152266]
    assert all("більше не адміністратор" in text for _chat_id, text in bot.messages)
    assert all("Спільнота &lt;VIP&gt;" in text for _chat_id, text in bot.messages)
    async with session_factory() as session:
        stored_resource = await session.scalar(
            select(TelegramResource).where(TelegramResource.chat_id == -100888)
        )
        attached_plan = await session.scalar(
            select(plan_resources.c.plan_id).where(
                plan_resources.c.resource_id == resource.id
            )
        )
    assert stored_resource is not None and stored_resource.is_active is False
    assert attached_plan is None
    assert await catalog.list_resources(active=True) == []


async def test_broadcast_queue_and_html_statistics(tmp_path: Path) -> None:
    session_factory = await _database(tmp_path)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                User(telegram_id=1, first_name="One", referral_code="ONE"),
                User(telegram_id=2, first_name="Two", referral_code="TWO"),
            ]
        )

    bot: Any = object()
    broadcasts = BroadcastService(session_factory, bot, batch_size=25)
    queued = await broadcasts.queue(
        created_by_telegram_id=402152266,
        source_chat_id=402152266,
        source_message_ids=[11, 10, 11],
        buttons=[[{"text": "Site", "url": "https://example.com"}]],
        target=BroadcastTarget.ALL_USERS,
    )
    assert queued.source_message_ids == [10, 11]
    assert queued.total_recipients == 2

    report = await StatsService(session_factory).render_html()
    assert "Статистика клубу" in report
    assert "Користувачі" in report


async def test_broadcast_worker_copies_content_and_completes(tmp_path: Path) -> None:
    class MessageId:
        def __init__(self, message_id: int) -> None:
            self.message_id = message_id

    class FakeBot:
        def __init__(self) -> None:
            self.copies: list[tuple[int, int, list[int]]] = []
            self.markups: list[tuple[int, int]] = []

        async def copy_messages(
            self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
        ) -> list[MessageId]:
            self.copies.append((chat_id, from_chat_id, message_ids))
            return [MessageId(101), MessageId(102)]

        async def edit_message_reply_markup(
            self, *, chat_id: int, message_id: int, reply_markup: object
        ) -> None:
            self.markups.append((chat_id, message_id))

    session_factory = await _database(tmp_path)
    async with session_factory() as session, session.begin():
        session.add(User(telegram_id=7, first_name="Seven", referral_code="SEVEN"))

    fake_bot = FakeBot()
    service = BroadcastService(session_factory, fake_bot, batch_size=25)  # type: ignore[arg-type]
    queued = await service.queue(
        created_by_telegram_id=402152266,
        source_chat_id=402152266,
        source_message_ids=[20, 21],
        buttons=[[{"text": "Клуб", "url": "https://example.com"}]],
        target=BroadcastTarget.ALL_USERS,
    )
    assert await service.process_batch() is True
    assert fake_bot.copies == [(7, 402152266, [20, 21])]
    assert fake_bot.markups == [(7, 102)]

    async with session_factory() as session:
        broadcast = await session.get(Broadcast, queued.id)
        recipient = await session.scalar(select(BroadcastRecipient))
    assert broadcast is not None and broadcast.status == BroadcastStatus.COMPLETED
    assert broadcast.sent_count == 1
    assert recipient is not None and recipient.status == DeliveryStatus.SENT


def test_broadcast_button_parser_supports_rows() -> None:
    buttons = _parse_buttons(
        "Сайт | https://example.com ;; Telegram | tg://resolve?domain=test\nДопомога | https://help.test"
    )
    assert len(buttons) == 2
    assert len(buttons[0]) == 2


def test_button_parser_supports_telegram_colors() -> None:
    buttons = _parse_buttons(
        "Канал - https://t.me/example (зелений) ;; "
        "Сайт - https://example.com (синій)\n"
        "Видалити - tg://resolve?domain=test (червоний)"
    )

    assert buttons == [
        [
            {"text": "Канал", "url": "https://t.me/example", "style": "success"},
            {"text": "Сайт", "url": "https://example.com", "style": "primary"},
        ],
        [
            {
                "text": "Видалити",
                "url": "tg://resolve?domain=test",
                "style": "danger",
            }
        ],
    ]
    markup = url_buttons_markup(buttons)
    assert markup is not None
    assert markup.inline_keyboard[0][0].style == "success"
    assert markup.inline_keyboard[0][1].style == "primary"
    assert markup.inline_keyboard[1][0].style == "danger"


def test_button_parser_rejects_unknown_color() -> None:
    with pytest.raises(ValueError, match="колір має бути"):
        _parse_buttons("Сайт - https://example.com (фіолетовий)")
