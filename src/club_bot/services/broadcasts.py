from __future__ import annotations

import asyncio
import uuid

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import (
    BroadcastStatus,
    BroadcastTarget,
    DeliveryStatus,
    SubscriptionStatus,
)
from club_bot.domain.rules import utc_now
from club_bot.models import Broadcast, BroadcastRecipient, Subscription, User
from club_bot.services.telegram_content import TelegramContent, copy_telegram_content


class BroadcastService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        batch_size: int,
    ) -> None:
        self.session_factory = session_factory
        self.bot = bot
        self.batch_size = batch_size

    async def queue(
        self,
        *,
        created_by_telegram_id: int,
        source_chat_id: int,
        source_message_ids: list[int],
        buttons: list[list[dict[str, str]]],
        target: BroadcastTarget,
    ) -> Broadcast:
        if not source_message_ids:
            raise ValueError("Broadcast must have at least one source message")
        if len(set(source_message_ids)) > 100:
            raise ValueError("Broadcast cannot contain more than 100 source messages")
        async with self.session_factory() as session, session.begin():
            broadcast = Broadcast(
                created_by_telegram_id=created_by_telegram_id,
                status=BroadcastStatus.QUEUED,
                target=target,
                source_chat_id=source_chat_id,
                source_message_ids=sorted(set(source_message_ids)),
                buttons=buttons,
                scheduled_at=utc_now(),
            )
            session.add(broadcast)
            await session.flush()
            user_ids = await self._target_user_ids(session, target)
            session.add_all(
                [
                    BroadcastRecipient(broadcast_id=broadcast.id, user_id=user_id)
                    for user_id in user_ids
                ]
            )
            broadcast.total_recipients = len(user_ids)
            return broadcast

    async def recent(self, limit: int = 10) -> list[Broadcast]:
        async with self.session_factory() as session:
            result = await session.scalars(
                select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit)
            )
            return list(result.all())

    async def process_batch(self) -> bool:
        broadcast_id = await self._claim_broadcast()
        if broadcast_id is None:
            return False
        async with self.session_factory() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return False
            recipients = list(
                (
                    await session.scalars(
                        select(BroadcastRecipient)
                        .options(selectinload(BroadcastRecipient.user))
                        .where(
                            BroadcastRecipient.broadcast_id == broadcast_id,
                            BroadcastRecipient.status == DeliveryStatus.PENDING,
                        )
                        .order_by(BroadcastRecipient.created_at)
                        .limit(self.batch_size)
                    )
                ).all()
            )

        if not recipients:
            await self._complete(broadcast_id)
            return True

        for recipient in recipients:
            try:
                await self._deliver(broadcast, recipient.user.telegram_id)
            except TelegramRetryAfter as error:
                await asyncio.sleep(min(float(error.retry_after), 30.0))
                break
            except TelegramForbiddenError as error:
                await self._mark_delivery(
                    recipient.id,
                    sent=False,
                    error=str(error),
                    block_user_id=recipient.user_id,
                )
            except Exception as error:
                await self._mark_delivery(recipient.id, sent=False, error=str(error))
            else:
                await self._mark_delivery(recipient.id, sent=True)
            await asyncio.sleep(0.04)
        await self._refresh_counts(broadcast_id)
        return True

    async def _claim_broadcast(self) -> uuid.UUID | None:
        async with self.session_factory() as session, session.begin():
            broadcast = await session.scalar(
                select(Broadcast)
                .where(
                    Broadcast.status.in_([BroadcastStatus.QUEUED, BroadcastStatus.SENDING]),
                    Broadcast.scheduled_at <= utc_now(),
                )
                .order_by(Broadcast.scheduled_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if broadcast is None:
                return None
            if broadcast.status == BroadcastStatus.QUEUED:
                broadcast.status = BroadcastStatus.SENDING
                broadcast.started_at = utc_now()
            return broadcast.id

    async def _deliver(self, broadcast: Broadcast, telegram_id: int) -> None:
        await copy_telegram_content(
            self.bot,
            destination_chat_id=telegram_id,
            content=TelegramContent(
                source_chat_id=broadcast.source_chat_id,
                source_message_ids=broadcast.source_message_ids,
                buttons=broadcast.buttons,
            ),
        )

    async def _mark_delivery(
        self,
        recipient_id: uuid.UUID,
        *,
        sent: bool,
        error: str | None = None,
        block_user_id: uuid.UUID | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            recipient = await session.get(BroadcastRecipient, recipient_id, with_for_update=True)
            if recipient is None:
                return
            recipient.attempts += 1
            if sent:
                recipient.status = DeliveryStatus.SENT
                recipient.sent_at = utc_now()
                recipient.error_message = None
            elif recipient.attempts >= 3 or block_user_id is not None:
                recipient.status = DeliveryStatus.FAILED
                recipient.error_message = (error or "Unknown delivery error")[:1000]
            else:
                recipient.error_message = (error or "Transient delivery error")[:1000]
            if block_user_id is not None:
                await session.execute(
                    update(User).where(User.id == block_user_id).values(is_blocked=True)
                )

    async def _refresh_counts(self, broadcast_id: uuid.UUID) -> None:
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                select(BroadcastRecipient.status, func.count())
                .where(BroadcastRecipient.broadcast_id == broadcast_id)
                .group_by(BroadcastRecipient.status)
            )
            counts: dict[DeliveryStatus, int] = {
                status: count for status, count in result.tuples().all()
            }
            broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
            if broadcast is None:
                return
            broadcast.sent_count = counts.get(DeliveryStatus.SENT, 0)
            broadcast.failed_count = counts.get(DeliveryStatus.FAILED, 0)
            if counts.get(DeliveryStatus.PENDING, 0) == 0:
                broadcast.status = BroadcastStatus.COMPLETED
                broadcast.completed_at = utc_now()

    async def _complete(self, broadcast_id: uuid.UUID) -> None:
        async with self.session_factory() as session, session.begin():
            broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
            if broadcast:
                broadcast.status = BroadcastStatus.COMPLETED
                broadcast.completed_at = utc_now()

    @staticmethod
    async def _target_user_ids(session: AsyncSession, target: BroadcastTarget) -> list[uuid.UUID]:
        statement = select(User.id).where(User.is_blocked.is_(False))
        if target == BroadcastTarget.ACTIVE_SUBSCRIBERS:
            statement = (
                statement.join(Subscription, Subscription.user_id == User.id)
                .where(
                    Subscription.status.in_(
                        [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
                    ),
                    Subscription.current_period_end > utc_now(),
                )
                .distinct()
            )
        return list(await session.scalars(statement))
