from __future__ import annotations

from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.domain.rules import generate_referral_code
from club_bot.models import Referral, User
from club_bot.repositories import UserRepository


class UserService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def upsert_telegram_user(
        self, telegram_user: TelegramUser, *, referral_code: str | None = None
    ) -> User:
        async with self.session_factory() as session, session.begin():
            repository = UserRepository(session)
            user = await repository.by_telegram_id(telegram_user.id)
            if user is None:
                user = User(
                    telegram_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    last_name=telegram_user.last_name,
                    language_code=telegram_user.language_code,
                    referral_code=generate_referral_code(),
                )
                session.add(user)
                await session.flush()
                await self._attach_referrer(session, user, referral_code)
            else:
                user.username = telegram_user.username
                user.first_name = telegram_user.first_name
                user.last_name = telegram_user.last_name
                user.language_code = telegram_user.language_code
                if user.referred_by_user_id is None:
                    await self._attach_referrer(session, user, referral_code)
            return user

    @staticmethod
    async def _attach_referrer(
        session: AsyncSession, user: User, referral_code: str | None
    ) -> None:
        if not referral_code:
            return
        referrer = await UserRepository(session).by_referral_code(referral_code)
        if referrer is None or referrer.id == user.id:
            return
        user.referred_by_user_id = referrer.id
        session.add(Referral(referrer_user_id=referrer.id, referred_user_id=user.id))
