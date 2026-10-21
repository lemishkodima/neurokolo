from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from club_bot.bot.system_router import review_join_request


@pytest.mark.parametrize("approved", [True, False])
async def test_join_confirmation_only_after_approval(approved: bool) -> None:
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, title="Клуб <AI>"),
        from_user=SimpleNamespace(id=501),
        invite_link=SimpleNamespace(invite_link="https://t.me/+personal"),
    )
    access = SimpleNamespace(handle_join_request=AsyncMock(return_value=approved))
    bot = SimpleNamespace(send_message=AsyncMock())
    await review_join_request(event, access, bot)  # type: ignore[arg-type]
    if approved:
        bot.send_message.assert_awaited_once()
        recipient, message = bot.send_message.call_args.args
        assert recipient == 501
        assert "Вступ підтверджено" in message
        assert "Клуб &lt;AI&gt;" in message
        assert "списку чатів Telegram" in message
    else:
        bot.send_message.assert_not_awaited()


async def test_confirmation_delivery_failure_does_not_retry_approval() -> None:
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123, title="Клуб"),
        from_user=SimpleNamespace(id=501),
        invite_link=None,
    )
    access = SimpleNamespace(handle_join_request=AsyncMock(return_value=True))
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramForbiddenError(
                method=SendMessage(chat_id=501, text="test"),
                message="Bot blocked",
            )
        )
    )
    await review_join_request(event, access, bot)  # type: ignore[arg-type]
    access.handle_join_request.assert_awaited_once()
