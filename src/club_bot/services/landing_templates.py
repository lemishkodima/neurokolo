from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Final
from urllib.parse import urlparse

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.domain.enums import PaymentStatus
from club_bot.models import LandingTemplate, LandingVisit, Payment, Subscription, User

MAX_HTML_TEMPLATE_BYTES: Final = 256 * 1024
DEFAULT_DOWNLOAD_URL: Final = "https://telegram.org/apps"
ALLOWED_PLACEHOLDERS: Final = frozenset(
    {
        "avatar_url",
        "channel_title",
        "download_url",
        "landing_description",
        "landing_title",
        "open_url",
    }
)
PLACEHOLDER_PATTERN: Final = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}")
SLUG_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UNSAFE_HTML_PATTERNS: Final = (
    re.compile(r"<\s*/?\s*(?:script|iframe|object|embed|base|form)\b", re.IGNORECASE),
    re.compile(r"<\s*meta\b[^>]*\bhttp-equiv\s*=", re.IGNORECASE),
    re.compile(r"\s+on[a-z]+\s*=", re.IGNORECASE),
    re.compile(r"(?:javascript|vbscript)\s*:", re.IGNORECASE),
    re.compile(r"data\s*:\s*text/html", re.IGNORECASE),
)


class LandingTemplateError(ValueError):
    pass


class LandingTemplateNotFoundError(LookupError):
    pass


class LandingTemplateSlugExistsError(LandingTemplateError):
    pass


@dataclass(frozen=True)
class LandingVisitor:
    seen_at: datetime
    telegram_id: int
    username: str | None
    first_name: str
    last_name: str | None


@dataclass(frozen=True)
class LandingStatistics:
    total_starts: int
    unique_users: int
    paid_users: int
    recent_visitors: list[LandingVisitor]

    @property
    def conversion_percent(self) -> float:
        if self.unique_users == 0:
            return 0.0
        return round(self.paid_users / self.unique_users * 100, 1)


class LandingTemplateService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def list_templates(self) -> list[LandingTemplate]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(LandingTemplate).order_by(LandingTemplate.created_at, LandingTemplate.name)
            )
            return list(result.all())

    async def get(self, template_id: uuid.UUID) -> LandingTemplate | None:
        async with self.session_factory() as session:
            template: LandingTemplate | None = await session.get(LandingTemplate, template_id)
            return template

    async def get_by_slug(self, slug: str) -> LandingTemplate | None:
        async with self.session_factory() as session:
            template: LandingTemplate | None = await session.scalar(
                select(LandingTemplate).where(LandingTemplate.slug == slug)
            )
            return template

    async def create(
        self,
        *,
        name: str,
        slug: str,
        landing_title: str,
        channel_title: str,
        landing_description: str,
        html_template: str,
        created_by_telegram_id: int,
        download_url: str = DEFAULT_DOWNLOAD_URL,
    ) -> LandingTemplate:
        values = self._validated_values(
            name=name,
            slug=slug,
            landing_title=landing_title,
            channel_title=channel_title,
            landing_description=landing_description,
            html_template=html_template,
            download_url=download_url,
        )
        async with self.session_factory() as session, session.begin():
            existing = await session.scalar(
                select(LandingTemplate.id).where(LandingTemplate.slug == values["slug"])
            )
            if existing is not None:
                raise LandingTemplateSlugExistsError("Такий slug уже використовується.")
            template = LandingTemplate(
                **values,
                created_by_telegram_id=created_by_telegram_id,
            )
            session.add(template)
            await session.flush()
            return template

    async def update_field(
        self,
        template_id: uuid.UUID,
        *,
        field: str,
        value: str,
    ) -> LandingTemplate:
        editable_fields = {
            "name",
            "slug",
            "landing_title",
            "channel_title",
            "landing_description",
            "html_template",
            "download_url",
        }
        if field not in editable_fields:
            raise LandingTemplateError("Невідоме поле шаблону.")

        validated = self._validate_field(field, value)
        async with self.session_factory() as session, session.begin():
            template = await session.get(LandingTemplate, template_id, with_for_update=True)
            if template is None:
                raise LandingTemplateNotFoundError("Шаблон не знайдено.")
            if field == "slug":
                existing = await session.scalar(
                    select(LandingTemplate.id).where(
                        LandingTemplate.slug == validated,
                        LandingTemplate.id != template_id,
                    )
                )
                if existing is not None:
                    raise LandingTemplateSlugExistsError("Такий slug уже використовується.")
            setattr(template, field, validated)
            await session.flush()
            return template

    async def delete(self, template_id: uuid.UUID) -> bool:
        async with self.session_factory() as session, session.begin():
            template = await session.get(LandingTemplate, template_id, with_for_update=True)
            if template is None:
                return False
            await session.delete(template)
            return True

    async def record_start(self, *, user_id: uuid.UUID, slug: str) -> bool:
        if not SLUG_PATTERN.fullmatch(slug) or len(slug) > 56:
            return False
        async with self.session_factory() as session, session.begin():
            template = await session.scalar(
                select(LandingTemplate).where(LandingTemplate.slug == slug)
            )
            if template is None:
                return False
            session.add(
                LandingVisit(
                    landing_template_id=template.id,
                    landing_slug=template.slug,
                    user_id=user_id,
                )
            )
            return True

    async def statistics(self, template_id: uuid.UUID) -> LandingStatistics:
        async with self.session_factory() as session:
            total_starts, unique_users = (
                await session.execute(
                    select(
                        func.count(LandingVisit.id),
                        func.count(distinct(LandingVisit.user_id)),
                    ).where(LandingVisit.landing_template_id == template_id)
                )
            ).one()
            approved_after_visit = (
                select(Payment.id)
                .join(Subscription, Subscription.id == Payment.subscription_id)
                .where(
                    Subscription.user_id == LandingVisit.user_id,
                    Payment.status == PaymentStatus.APPROVED,
                    Payment.paid_at >= LandingVisit.created_at,
                )
                .exists()
            )
            paid_users = await session.scalar(
                select(func.count(distinct(LandingVisit.user_id))).where(
                    LandingVisit.landing_template_id == template_id,
                    approved_after_visit,
                )
            )
            recent_rows = (
                await session.execute(
                    select(
                        LandingVisit.created_at,
                        User.telegram_id,
                        User.username,
                        User.first_name,
                        User.last_name,
                    )
                    .join(User, User.id == LandingVisit.user_id)
                    .where(LandingVisit.landing_template_id == template_id)
                    .order_by(LandingVisit.created_at.desc())
                    .limit(10)
                )
            ).all()
        return LandingStatistics(
            total_starts=int(total_starts or 0),
            unique_users=int(unique_users or 0),
            paid_users=int(paid_users or 0),
            recent_visitors=[
                LandingVisitor(
                    seen_at=row[0],
                    telegram_id=row[1],
                    username=row[2],
                    first_name=row[3],
                    last_name=row[4],
                )
                for row in recent_rows
            ],
        )

    @classmethod
    def render(
        cls,
        template: LandingTemplate,
        *,
        avatar_url: str,
        open_url: str,
    ) -> str:
        values = {
            "avatar_url": avatar_url,
            "channel_title": template.channel_title,
            "download_url": template.download_url,
            "landing_description": template.landing_description,
            "landing_title": template.landing_title,
            "open_url": open_url,
        }
        escaped_values = {key: escape(value, quote=True) for key, value in values.items()}
        return PLACEHOLDER_PATTERN.sub(
            lambda match: escaped_values[match.group(1)],
            template.html_template,
        )

    @classmethod
    def validate_html(cls, html_template: str) -> str:
        value = html_template.lstrip("\ufeff").strip()
        if not value:
            raise LandingTemplateError("HTML-шаблон порожній.")
        if len(value.encode("utf-8")) > MAX_HTML_TEMPLATE_BYTES:
            raise LandingTemplateError("HTML-шаблон перевищує 256 КБ.")
        lowered = value.casefold()
        if "<html" not in lowered or "</html>" not in lowered:
            raise LandingTemplateError("HTML-шаблон має містити повний елемент <html>…</html>.")
        for pattern in UNSAFE_HTML_PATTERNS:
            if pattern.search(value):
                raise LandingTemplateError(
                    "HTML може містити розмітку й CSS, але не JavaScript, форми, iframe "
                    "або event-handler атрибути."
                )
        placeholders = set(PLACEHOLDER_PATTERN.findall(value))
        unknown = placeholders - ALLOWED_PLACEHOLDERS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise LandingTemplateError(f"Невідомі плейсхолдери: {names}.")
        if "open_url" not in placeholders:
            raise LandingTemplateError("Додайте плейсхолдер {{open_url}} для кнопки Telegram.")
        return value

    @classmethod
    def validate_field(cls, field: str, value: str) -> str:
        return cls._validate_field(field, value)

    @classmethod
    def _validated_values(cls, **values: str) -> dict[str, str]:
        return {field: cls._validate_field(field, value) for field, value in values.items()}

    @classmethod
    def _validate_field(cls, field: str, value: str) -> str:
        if field == "html_template":
            return cls.validate_html(value)
        cleaned = value.strip()
        limits = {
            "name": 100,
            "slug": 56,
            "landing_title": 255,
            "channel_title": 255,
            "landing_description": 2000,
            "download_url": 2048,
        }
        limit = limits[field]
        if not cleaned or len(cleaned) > limit:
            raise LandingTemplateError(f"Поле має містити від 1 до {limit} символів.")
        if field == "slug" and not SLUG_PATTERN.fullmatch(cleaned):
            raise LandingTemplateError(
                "Slug може містити лише малі латинські літери, цифри та дефіси."
            )
        if field == "download_url":
            parsed = urlparse(cleaned)
            if parsed.scheme != "https" or not parsed.netloc:
                raise LandingTemplateError("Посилання завантаження має бути абсолютним HTTPS URL.")
        return cleaned
