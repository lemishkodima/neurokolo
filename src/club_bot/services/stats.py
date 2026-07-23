from __future__ import annotations

from datetime import timedelta
from html import escape
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from club_bot.domain.enums import BroadcastStatus, PaymentStatus, SubscriptionStatus
from club_bot.domain.rules import utc_now
from club_bot.models import Broadcast, Payment, Plan, Subscription, TelegramResource, User


class StatsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def render_html(self) -> str:
        now = utc_now()
        async with self.session_factory() as session:
            total_users = await self._count(session, User)
            new_7d = await self._count(session, User, User.created_at >= now - timedelta(days=7))
            new_30d = await self._count(session, User, User.created_at >= now - timedelta(days=30))
            active_subscriptions = await self._count(
                session,
                Subscription,
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
                Subscription.current_period_end > now,
            )
            past_due = await self._count(
                session, Subscription, Subscription.status == SubscriptionStatus.PAST_DUE
            )
            expired = await self._count(
                session, Subscription, Subscription.status == SubscriptionStatus.EXPIRED
            )
            plans = await self._count(session, Plan, Plan.is_active.is_(True))
            resources = await self._count(
                session, TelegramResource, TelegramResource.is_active.is_(True)
            )
            broadcasts = await self._count(
                session, Broadcast, Broadcast.status == BroadcastStatus.COMPLETED
            )
            revenue = await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == PaymentStatus.APPROVED
                )
            )
            recent_payments = await self._count(
                session,
                Payment,
                Payment.status == PaymentStatus.APPROVED,
                Payment.paid_at >= now - timedelta(days=30),
            )

        conversion = active_subscriptions / total_users * 100 if total_users else 0
        cards = [
            ("Користувачі", total_users, f"+{new_7d} за 7 днів · +{new_30d} за 30 днів"),
            (
                "Активні підписки",
                active_subscriptions,
                f"Конверсія {conversion:.1f}% · проблемних {past_due}",
            ),
            ("Завершені підписки", expired, "Доступ автоматично закрито"),
            ("Успішні оплати", recent_payments, f"Загальний оборот {revenue} UAH"),
            ("Тарифи / ресурси", f"{plans} / {resources}", "Активні конфігурації"),
            ("Розсилки", broadcasts, "Завершені кампанії"),
        ]
        card_html = "".join(
            '<section class="card">'
            f'<div class="label">{escape(str(label))}</div>'
            f'<div class="value">{escape(str(value))}</div>'
            f'<div class="note">{escape(str(note))}</div>'
            "</section>"
            for label, value, note in cards
        )
        return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Статистика клубу</title><style>
:root{{--bg:#0b1020;--card:#151d33;--text:#f5f7ff;--muted:#9eacc9;--accent:#7c5cff}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#0b1020,#111a31);
color:var(--text);font:16px/1.5 Inter,Arial,sans-serif}}
main{{max-width:1040px;margin:auto;padding:48px 24px}}
h1{{font-size:38px;margin:0 0 6px}}.subtitle{{color:var(--muted);margin-bottom:30px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:16px}}
.card{{background:var(--card);border:1px solid #26314f;border-radius:18px;padding:24px;
box-shadow:0 12px 34px #0004}}.label,.note{{color:var(--muted)}}.value{{font-size:36px;
font-weight:800;margin:8px 0;color:#fff}}footer{{color:var(--muted);margin-top:32px;font-size:13px}}
</style></head><body><main><h1>Статистика клубу</h1>
<div class="subtitle">Операційний звіт станом на {now:%d.%m.%Y %H:%M} UTC</div>
<div class="grid">{card_html}</div><footer>Згенеровано Telegram Subscription Club</footer>
</main></body></html>"""

    @staticmethod
    async def _count(
        session: AsyncSession,
        model: type[Any],
        *conditions: ColumnElement[bool],
    ) -> int:
        statement = select(func.count()).select_from(model)
        if conditions:
            statement = statement.where(*conditions)
        return int(await session.scalar(statement) or 0)
