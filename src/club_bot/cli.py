from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select

from club_bot.config import get_settings
from club_bot.db import create_engine, create_session_factory
from club_bot.domain.billing import SUPPORTED_BILLING_MONTHS
from club_bot.domain.enums import ResourceType
from club_bot.models import Plan, TelegramResource


async def _seed_plan(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            plan = await session.scalar(select(Plan).where(Plan.code == args.code))
            if plan is None:
                plan = Plan(code=args.code)
                session.add(plan)
            plan.name = args.name
            plan.description = args.description
            plan.price = Decimal(args.price)
            plan.currency = args.currency.upper()
            plan.billing_months = args.billing_months
            plan.is_active = True
        print(f"Plan '{args.code}' saved")
    finally:
        await engine.dispose()


async def _seed_resource(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            resource = await session.scalar(
                select(TelegramResource).where(TelegramResource.code == args.code)
            )
            if resource is None:
                resource = TelegramResource(code=args.code)
                session.add(resource)
            resource.name = args.name
            resource.chat_id = args.chat_id
            resource.resource_type = ResourceType(args.type)
            resource.is_active = True
        print(f"Resource '{args.code}' saved")
    finally:
        await engine.dispose()


async def _attach_resource(args: argparse.Namespace) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            plan = await session.scalar(select(Plan).where(Plan.code == args.plan))
            resource = await session.scalar(
                select(TelegramResource).where(TelegramResource.code == args.resource)
            )
            if plan is None or resource is None:
                raise SystemExit("Plan or resource does not exist")
            await session.refresh(plan, attribute_names=["resources"])
            if resource not in plan.resources:
                plan.resources.append(resource)
        print(f"Resource '{args.resource}' attached to plan '{args.plan}'")
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Club administration utility")
    commands = parser.add_subparsers(required=True)

    plan = commands.add_parser("seed-plan")
    plan.add_argument("--code", required=True)
    plan.add_argument("--name", required=True)
    plan.add_argument("--description", default="")
    plan.add_argument("--price", required=True)
    plan.add_argument("--currency", default="UAH")
    plan.add_argument("--billing-months", type=int, choices=SUPPORTED_BILLING_MONTHS, default=1)
    plan.set_defaults(handler=_seed_plan)

    resource = commands.add_parser("seed-resource")
    resource.add_argument("--code", required=True)
    resource.add_argument("--name", required=True)
    resource.add_argument("--chat-id", required=True, type=int)
    resource.add_argument("--type", choices=[item.value for item in ResourceType], required=True)
    resource.set_defaults(handler=_seed_resource)

    attach = commands.add_parser("attach-resource")
    attach.add_argument("--plan", required=True)
    attach.add_argument("--resource", required=True)
    attach.set_defaults(handler=_attach_resource)
    return parser


def run() -> None:
    args = _parser().parse_args()
    asyncio.run(args.handler(args))


if __name__ == "__main__":
    run()
