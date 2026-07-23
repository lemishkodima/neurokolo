from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import ResourceType
from club_bot.models import Admin, AppSetting, Plan, TelegramResource, plan_resources
from club_bot.services.telegram_content import TelegramContent


@dataclass(frozen=True)
class MenuLabels:
    about: str
    join: str
    subscription: str
    materials: str
    support: str


DEFAULT_SETTINGS = {
    "button_about": "Про клуб 💎",
    "button_join": "Доєднатися ✅",
    "button_subscription": "Моя підписка 👤",
    "button_materials": "Матеріали 📚",
    "button_support": "Техпідтримка ⚙️",
    "club_about_text": (
        "<b>Закритий клуб</b> — уроки, оновлення, тематичні обговорення та підтримка "
        "в одному Telegram-просторі. Доступ діє, поки активна щомісячна підписка."
    ),
}

MENU_CONTENT_ACTIONS = frozenset({"about", "join", "subscription", "materials", "support"})


class AdminService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bootstrap_ids: list[int],
    ) -> None:
        self.session_factory = session_factory
        self.bootstrap_ids = frozenset(bootstrap_ids)

    async def is_admin(self, telegram_id: int) -> bool:
        if telegram_id in self.bootstrap_ids:
            return True
        async with self.session_factory() as session:
            result = await session.scalar(
                select(Admin.id).where(
                    Admin.telegram_id == telegram_id,
                    Admin.is_active.is_(True),
                )
            )
            return result is not None

    async def list_admins(self) -> list[tuple[int, bool]]:
        async with self.session_factory() as session:
            stored = list(
                await session.scalars(
                    select(Admin.telegram_id)
                    .where(Admin.is_active.is_(True))
                    .order_by(Admin.telegram_id)
                )
            )
        ids = sorted(self.bootstrap_ids | set(stored))
        return [(telegram_id, telegram_id in self.bootstrap_ids) for telegram_id in ids]

    async def add_admin(self, telegram_id: int, *, added_by: int) -> None:
        if telegram_id in self.bootstrap_ids:
            return
        async with self.session_factory() as session, session.begin():
            admin = await session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
            if admin is None:
                admin = Admin(telegram_id=telegram_id, added_by_telegram_id=added_by)
                session.add(admin)
            admin.is_active = True
            admin.added_by_telegram_id = added_by

    async def remove_admin(self, telegram_id: int) -> bool:
        if telegram_id in self.bootstrap_ids:
            return False
        async with self.session_factory() as session, session.begin():
            admin = await session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
            if admin is None:
                return False
            admin.is_active = False
            return True


class SettingsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get(self, key: str) -> str:
        async with self.session_factory() as session:
            value = await session.scalar(select(AppSetting.value).where(AppSetting.key == key))
            return value if value is not None else DEFAULT_SETTINGS[key]

    async def set(self, key: str, value: str) -> None:
        if key not in DEFAULT_SETTINGS:
            raise KeyError(key)
        async with self.session_factory() as session, session.begin():
            setting = await session.get(AppSetting, key)
            if setting is None:
                setting = AppSetting(key=key, value=value)
                session.add(setting)
            else:
                setting.value = value

    async def labels(self) -> MenuLabels:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AppSetting.key, AppSetting.value).where(
                    AppSetting.key.in_(
                        [
                            "button_about",
                            "button_join",
                            "button_subscription",
                            "button_materials",
                            "button_support",
                        ]
                    )
                )
            )
            rows: dict[str, str] = {key: value for key, value in result.tuples().all()}
        return MenuLabels(
            about=rows.get("button_about", DEFAULT_SETTINGS["button_about"]),
            join=rows.get("button_join", DEFAULT_SETTINGS["button_join"]),
            subscription=rows.get("button_subscription", DEFAULT_SETTINGS["button_subscription"]),
            materials=rows.get("button_materials", DEFAULT_SETTINGS["button_materials"]),
            support=rows.get("button_support", DEFAULT_SETTINGS["button_support"]),
        )

    async def menu_content(self, action: str) -> TelegramContent | None:
        self._validate_menu_action(action)
        async with self.session_factory() as session:
            value = await session.scalar(
                select(AppSetting.value).where(AppSetting.key == self._content_key(action))
            )
        if value is None:
            return None
        payload = json.loads(value)
        return TelegramContent(
            source_chat_id=int(payload["source_chat_id"]),
            source_message_ids=[int(item) for item in payload["source_message_ids"]],
            buttons=list(payload.get("buttons", [])),
        )

    async def set_menu_content(self, action: str, content: TelegramContent) -> None:
        self._validate_menu_action(action)
        if not content.source_message_ids or len(content.source_message_ids) > 100:
            raise ValueError("Menu content must contain from 1 to 100 messages")
        value = json.dumps(
            {
                "source_chat_id": content.source_chat_id,
                "source_message_ids": sorted(set(content.source_message_ids)),
                "buttons": content.buttons,
            },
            ensure_ascii=False,
        )
        key = self._content_key(action)
        async with self.session_factory() as session, session.begin():
            setting = await session.get(AppSetting, key)
            if setting is None:
                session.add(AppSetting(key=key, value=value))
            else:
                setting.value = value

    async def clear_menu_content(self, action: str) -> None:
        self._validate_menu_action(action)
        async with self.session_factory() as session, session.begin():
            setting = await session.get(AppSetting, self._content_key(action))
            if setting is not None:
                await session.delete(setting)

    @staticmethod
    def _content_key(action: str) -> str:
        return f"menu_content_{action}"

    @staticmethod
    def _validate_menu_action(action: str) -> None:
        if action not in MENU_CONTENT_ACTIONS:
            raise ValueError(f"Unknown menu action: {action}")


class CatalogService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        default_plan_code: str,
    ) -> None:
        self.session_factory = session_factory
        self.default_plan_code = default_plan_code

    async def register_resource(
        self,
        *,
        chat_id: int,
        title: str,
        resource_type: ResourceType,
        is_active: bool,
    ) -> TelegramResource:
        async with self.session_factory() as session, session.begin():
            resource = await session.scalar(
                select(TelegramResource).where(TelegramResource.chat_id == chat_id)
            )
            if resource is None:
                resource = TelegramResource(
                    code=f"tg_{abs(chat_id)}",
                    name=title,
                    chat_id=chat_id,
                    resource_type=resource_type,
                )
                session.add(resource)
            resource.name = title
            resource.resource_type = resource_type
            resource.is_active = is_active
            await session.flush()
            return resource

    async def list_resources(self) -> list[TelegramResource]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(TelegramResource).order_by(
                    TelegramResource.is_active.desc(), TelegramResource.name
                )
            )
            return list(result.all())

    async def list_plans(self) -> list[Plan]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(Plan)
                .options(selectinload(Plan.resources))
                .order_by(Plan.is_active.desc(), Plan.sort_order, Plan.name)
            )
            return list(result.unique().all())

    async def create_plan(self, *, name: str, price: Decimal, currency: str = "UAH") -> Plan:
        async with self.session_factory() as session, session.begin():
            count = await session.scalar(select(func.count()).select_from(Plan)) or 0
            code = self.default_plan_code if count == 0 else self._plan_code(name)
            while await session.scalar(select(Plan.id).where(Plan.code == code)):
                code = f"{code[:50]}_{uuid.uuid4().hex[:6]}"
            plan = Plan(
                code=code,
                name=name,
                price=price,
                currency=currency.upper(),
                sort_order=count,
            )
            session.add(plan)
            await session.flush()
            return plan

    async def plan_resources(self, plan_id: uuid.UUID) -> list[tuple[TelegramResource, bool]]:
        async with self.session_factory() as session:
            plan = await session.scalar(
                select(Plan).options(selectinload(Plan.resources)).where(Plan.id == plan_id)
            )
            if plan is None:
                return []
            selected = {resource.id for resource in plan.resources}
            resources = list(
                await session.scalars(
                    select(TelegramResource)
                    .where(TelegramResource.is_active.is_(True))
                    .order_by(TelegramResource.name)
                )
            )
            return [(resource, resource.id in selected) for resource in resources]

    async def toggle_plan_resource(self, plan_id: uuid.UUID, resource_id: uuid.UUID) -> bool:
        async with self.session_factory() as session, session.begin():
            exists = await session.scalar(
                select(plan_resources.c.plan_id).where(
                    plan_resources.c.plan_id == plan_id,
                    plan_resources.c.resource_id == resource_id,
                )
            )
            if exists:
                await session.execute(
                    plan_resources.delete().where(
                        plan_resources.c.plan_id == plan_id,
                        plan_resources.c.resource_id == resource_id,
                    )
                )
                return False
            await session.execute(
                plan_resources.insert().values(plan_id=plan_id, resource_id=resource_id)
            )
            return True

    @staticmethod
    def _plan_code(name: str) -> str:
        code = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
        return code[:50] or f"plan_{uuid.uuid4().hex[:8]}"
