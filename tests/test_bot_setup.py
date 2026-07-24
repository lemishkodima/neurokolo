from __future__ import annotations

from typing import Any

from aiogram.types import BotCommandScopeChat

from club_bot.bot.setup import (
    configure_admin_commands,
    configure_bot,
    remove_admin_commands,
)


class FakeBot:
    def __init__(self) -> None:
        self.configured: list[tuple[list[str], int | None]] = []
        self.deleted: list[int] = []

    async def set_my_commands(
        self,
        commands: list[Any],
        scope: BotCommandScopeChat | None = None,
    ) -> None:
        chat_id = int(scope.chat_id) if scope is not None else None
        self.configured.append(([command.command for command in commands], chat_id))

    async def delete_my_commands(self, scope: BotCommandScopeChat) -> None:
        self.deleted.append(int(scope.chat_id))


async def test_configure_bot_adds_admin_command_for_every_persisted_admin() -> None:
    bot = FakeBot()

    await configure_bot(bot, [101, 202])  # type: ignore[arg-type]

    assert bot.configured == [
        (["start", "subscription", "materials", "support", "help"], None),
        (["start", "subscription", "materials", "support", "help", "admin"], 101),
        (["start", "subscription", "materials", "support", "help", "admin"], 202),
    ]


async def test_dynamic_admin_command_scope_can_be_added_and_removed() -> None:
    bot = FakeBot()

    await configure_admin_commands(bot, 303)  # type: ignore[arg-type]
    await remove_admin_commands(bot, 303)  # type: ignore[arg-type]

    assert bot.configured == [
        (["start", "subscription", "materials", "support", "help", "admin"], 303)
    ]
    assert bot.deleted == [303]
