from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.enums import ChatMemberStatus, ChatType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.bot.admin_router import _parse_buttons
from club_bot.bot.system_router import track_bot_membership
from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import BroadcastStatus, BroadcastTarget, DeliveryStatus, ResourceType
from club_bot.models import (
    Base,
    Broadcast,
    BroadcastRecipient,
    Plan,
    TelegramResource,
    User,
)
from club_bot.services.admin import (
    AdminService,
    CatalogService,
    ProtectedPlanError,
    SettingsService,
)
from club_bot.services.broadcasts import BroadcastService
from club_bot.services.landing_templates import LandingTemplateError, LandingTemplateService
from club_bot.services.stats import StatsService
from club_bot.services.telegram_content import TelegramContent


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
    )
    assert updated is not None
    assert updated.name == "VIP Plus"
    assert updated.price == Decimal("1990.00")

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

    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><script>alert(1)</script>{{open_url}}</html>")
    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><a href='{{unknown_url}}'>Open</a></html>")
    with pytest.raises(LandingTemplateError):
        service.validate_html("<html><p>No Telegram link</p></html>")

    assert await service.delete(template.id) is True
    assert await service.get(template.id) is None


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
