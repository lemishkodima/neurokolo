from __future__ import annotations

import secrets
import time
from base64 import b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from aiogram import Bot
from aiogram.types import Update
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi import Path as ApiPath
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest
from sqlalchemy import func, select, text

from club_bot.bot.setup import configure_bot
from club_bot.config import Settings, get_settings
from club_bot.container import Container, build_container
from club_bot.domain.enums import PaymentStatus
from club_bot.integrations.wayforpay import InvalidWayForPaySignature
from club_bot.models import Payment
from club_bot.schemas import CheckoutCreate, CheckoutResponse
from club_bot.services.checkout_links import (
    InvalidPersonalCheckoutToken,
    verify_personal_checkout_token,
)
from club_bot.services.landing_templates import LandingTemplateService
from club_bot.services.subscriptions import CheckoutOwnerNotFoundError, PlanNotFoundError

FALLBACK_BOT_AVATAR = (
    Path(__file__).parent / "assets" / "prelanding-logo.svg"
).read_bytes()


def _checkout_response_headers(
    form_action: str | None = None,
    script_nonce: str | None = None,
) -> dict[str, str]:
    form_policy = f" form-action {form_action};" if form_action else " form-action 'none';"
    script_policy = (
        f" script-src 'nonce-{script_nonce}';" if script_nonce else " script-src 'none';"
    )
    return {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src 'none';"
            f"{script_policy}{form_policy} base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _gateway_form_inputs(fields: dict[str, Any]) -> str:
    inputs: list[str] = []
    for name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        field_name = f"{name}[]" if isinstance(value, list) else name
        for item in values:
            inputs.append(
                f'<input type="hidden" name="{escape(field_name, quote=True)}" '
                f'value="{escape(str(item), quote=True)}">'
            )
    return "\n".join(inputs)


def _landing_response_headers() -> dict[str, str]:
    return {
        "Cache-Control": "public, max-age=60",
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:;"
            " script-src 'none'; connect-src 'none'; font-src 'none'; media-src 'none';"
            " object-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


async def _bot_avatar(bot: Bot) -> tuple[bytes, str]:
    try:
        me = await bot.get_me()
        photos = await bot.get_user_profile_photos(me.id, limit=1)
        if not photos.photos:
            return FALLBACK_BOT_AVATAR, "image/svg+xml"
        destination = BytesIO()
        await bot.download(photos.photos[0][-1], destination=destination)
        content = destination.getvalue()
        if content:
            return content, "image/jpeg"
    except Exception:
        pass
    return FALLBACK_BOT_AVATAR, "image/svg+xml"


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    metrics_registry = CollectorRegistry()
    request_count = Counter(
        "neurokolo_http_requests_total",
        "HTTP requests handled by the API",
        ("method", "route", "status"),
        registry=metrics_registry,
    )
    request_latency = Histogram(
        "neurokolo_http_request_duration_seconds",
        "HTTP request latency",
        ("method", "route"),
        registry=metrics_registry,
    )
    unmatched_payment_count = Gauge(
        "neurokolo_unmatched_approved_payments",
        "Approved production payments without a linked subscription",
        registry=metrics_registry,
    )
    unmatched_test_payment_count = Gauge(
        "neurokolo_unmatched_test_approved_payments",
        "Approved test payments without a linked subscription",
        registry=metrics_registry,
    )
    avatar_cache: tuple[float, bytes, str] | None = None

    async def cached_bot_avatar(container: Container) -> tuple[bytes, str]:
        nonlocal avatar_cache
        now = time.monotonic()
        if avatar_cache is None or avatar_cache[0] <= now:
            content, media_type = await _bot_avatar(container.bot)
            avatar_cache = (now + 3600, content, media_type)
        return avatar_cache[1], avatar_cache[2]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(resolved_settings)
        app.state.container = container
        admin_ids = [telegram_id for telegram_id, _ in await container.admin_service.list_admins()]
        await configure_bot(container.bot, admin_ids)
        await container.bot.set_webhook(
            url=resolved_settings.telegram_webhook_url,
            secret_token=resolved_settings.bot_webhook_secret.get_secret_value(),
            allowed_updates=container.dispatcher.resolve_used_update_types(),
        )
        yield
        await container.close()

    app = FastAPI(title="Telegram Subscription Club", version="0.8.0-rc9", lifespan=lifespan)

    @app.middleware("http")
    async def observe_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        route_name = str(getattr(route, "path", "unmatched"))
        request_count.labels(request.method, route_name, str(response.status_code)).inc()
        request_latency.labels(request.method, route_name).observe(time.perf_counter() - started)
        return response

    def container_from_request(request: Request) -> Container:
        return request.app.state.container  # type: ignore[no-any-return]

    async def require_internal_api_key(
        request: Request,
        x_internal_api_key: str = Header(alias="X-Internal-API-Key"),
    ) -> None:
        configured = request.app.state.container.settings.internal_api_key.get_secret_value()
        if not secrets.compare_digest(configured, x_internal_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def readiness(
        container: Container = Depends(container_from_request),
    ) -> dict[str, str]:
        try:
            async with container.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from error
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics(
        container: Container = Depends(container_from_request),
    ) -> Response:
        try:
            async with container.session_factory() as session:
                count = await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.status == PaymentStatus.APPROVED,
                        Payment.subscription_id.is_(None),
                        ~Payment.order_reference.startswith("TEST-"),
                    )
                )
                test_count = await session.scalar(
                    select(func.count(Payment.id)).where(
                        Payment.status == PaymentStatus.APPROVED,
                        Payment.subscription_id.is_(None),
                        Payment.order_reference.startswith("TEST-"),
                    )
                )
            unmatched_payment_count.set(int(count or 0))
            unmatched_test_payment_count.set(int(test_count or 0))
        except Exception:
            unmatched_payment_count.set(-1)
            unmatched_test_payment_count.set(-1)
        return Response(
            content=generate_latest(metrics_registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/join/{slug}", response_class=HTMLResponse, include_in_schema=False)
    async def public_landing(
        slug: str = ApiPath(
            min_length=1,
            max_length=56,
            pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        ),
        container: Container = Depends(container_from_request),
    ) -> HTMLResponse:
        template = await container.landing_template_service.get_by_slug(slug)
        if template is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Landing not found")
        avatar_content, avatar_media_type = await cached_bot_avatar(container)
        avatar_url = (
            f"data:{avatar_media_type};base64,"
            f"{b64encode(avatar_content).decode('ascii')}"
        )
        open_url = (
            f"https://t.me/{quote(container.settings.bot_username, safe='')}"
            f"?start=landing_{quote(slug, safe='')}"
        )
        content = LandingTemplateService.render(
            template,
            avatar_url=avatar_url,
            open_url=open_url,
        )
        return HTMLResponse(content=content, headers=_landing_response_headers())

    @app.get("/landing-assets/bot-avatar", include_in_schema=False)
    async def public_bot_avatar(
        container: Container = Depends(container_from_request),
    ) -> Response:
        content, media_type = await cached_bot_avatar(container)
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/webhooks/telegram", include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str = Header(alias="X-Telegram-Bot-Api-Secret-Token"),
        container: Container = Depends(container_from_request),
    ) -> dict[str, bool]:
        expected = container.settings.bot_webhook_secret.get_secret_value()
        if not secrets.compare_digest(expected, x_telegram_bot_api_secret_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        update = Update.model_validate(await request.json(), context={"bot": container.bot})
        await container.dispatcher.feed_update(
            container.bot,
            update,
            **container.workflow_data(),
        )
        return {"ok": True}

    @app.post("/webhooks/wayforpay", include_in_schema=False)
    async def wayforpay_webhook(
        request: Request,
        container: Container = Depends(container_from_request),
    ) -> dict[str, Any]:
        payload = await request.json()
        order_reference = str(payload.get("orderReference", ""))
        try:
            container.subscription_service.verify_callback(payload)
            initial_checkout = await container.subscription_service.is_initial_checkout_callback(
                order_reference
            )
            processed = await container.subscription_service.process_callback(payload)
        except InvalidWayForPaySignature as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(error)
            ) from error
        approved = str(payload.get("transactionStatus", "")).casefold() == "approved"
        if processed and approved and initial_checkout:
            telegram_id = await container.subscription_service.checkout_owner_telegram_id(
                order_reference
            )
            if telegram_id is not None:
                await container.subscription_notification_service.send_activated(telegram_id)
        elif processed and not approved:
            rec_token = str(payload.get("recToken") or "") or None
            await container.subscription_notification_service.send_payment_failed(
                order_reference,
                rec_token,
            )
        return container.subscription_service.callback_response(order_reference)

    @app.post(
        "/api/v1/checkout-sessions",
        response_model=CheckoutResponse,
        dependencies=[Depends(require_internal_api_key)],
    )
    async def create_checkout(
        data: CheckoutCreate,
        container: Container = Depends(container_from_request),
    ) -> CheckoutResponse:
        try:
            test_mode = await container.settings_service.payment_test_mode_active()
            return await container.subscription_service.create_checkout(
                plan_code=data.plan_code or container.settings.default_plan_code,
                email=str(data.email) if data.email else None,
                phone=data.phone,
                referral_code=data.referral_code,
                return_url=data.return_url,
                test_mode=test_mode,
            )
        except PlanNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found"
            ) from error

    @app.get("/checkout", response_class=HTMLResponse, include_in_schema=False)
    async def public_checkout(
        referral_code: str | None = Query(default=None, max_length=32),
        plan_code: str | None = Query(
            default=None,
            min_length=1,
            max_length=64,
            pattern=r"^[A-Za-z0-9_-]+$",
        ),
        owner: str | None = Query(
            default=None,
            min_length=20,
            max_length=240,
            pattern=r"^[A-Za-z0-9._-]+$",
        ),
        container: Container = Depends(container_from_request),
    ) -> HTMLResponse:
        telegram_id = None
        if owner is not None:
            try:
                telegram_id = verify_personal_checkout_token(
                    owner,
                    container.settings.internal_api_key.get_secret_value(),
                )
            except InvalidPersonalCheckoutToken as error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Personal checkout link is invalid or expired",
                ) from error
        try:
            test_mode = await container.settings_service.payment_test_mode_active()
            checkout = await container.subscription_service.create_checkout(
                plan_code=plan_code or container.settings.default_plan_code,
                email=None,
                phone=None,
                referral_code=referral_code,
                return_url=None,
                test_mode=test_mode,
                telegram_id=telegram_id,
            )
        except PlanNotFoundError as error:
            raise HTTPException(
                status_code=(
                    status.HTTP_404_NOT_FOUND
                    if plan_code is not None
                    else status.HTTP_503_SERVICE_UNAVAILABLE
                ),
                detail="Payment plan is unavailable",
            ) from error
        except CheckoutOwnerNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Telegram account must start the bot before checkout",
            ) from error

        complete_url = (
            f"{container.settings.public_base_url}/checkout/complete"
            f"?token={quote(checkout.checkout_token, safe='')}"
        )
        gateway_fields = dict(checkout.gateway_fields)
        gateway_fields["returnUrl"] = complete_url
        amount = escape(str(gateway_fields.get("amount", "")))
        currency = escape(str(gateway_fields.get("currency", "")))
        gateway_url = escape(checkout.gateway_url, quote=True)
        gateway_parts = urlsplit(checkout.gateway_url)
        form_action = f"{gateway_parts.scheme}://{gateway_parts.netloc}"
        script_nonce = secrets.token_urlsafe(18)
        escaped_script_nonce = escape(script_nonce, quote=True)
        test_notice = (
            "<p><b>Тестовий режим:</b> реального списання коштів не буде.</p>"
            if test_mode
            else ""
        )
        body = f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Оплата підписки Neurokolo</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, -apple-system, sans-serif;
      background: #0d1517; }}
    body {{ visibility: hidden; margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #0d1517; color: #f4f7f8; }}
    main {{ width: min(32rem, calc(100% - 3rem)); padding: 2.5rem;
      border: 1px solid #29464d; border-radius: 1.5rem; background: #142126; }}
    p {{ color: #b9c9cd; line-height: 1.6; }}
    button {{ width: 100%; margin-top: 1rem; padding: 1rem; border: 0;
      border-radius: .8rem; background: #8bc7d3; color: #0d1517;
      font: inherit; font-weight: 700; cursor: pointer; }}
    small {{ display: block; margin-top: 1rem; color: #789096; }}
  </style>
  <noscript><style>body {{ visibility: visible; }}</style></noscript>
</head>
<body>
  <main>
    <h1>Підписка Neurokolo</h1>
    {test_notice}
    <p>Переходимо на захищену платіжну сторінку WayForPay…</p>
    <form id="wayforpay-checkout" method="post" action="{gateway_url}">
      {_gateway_form_inputs(gateway_fields)}
      <button type="submit">Продовжити до оплати {amount} {currency}</button>
    </form>
    <small>Якщо перехід не відбувся автоматично, натисніть кнопку. Дані картки
      вводяться лише на стороні WayForPay.</small>
  </main>
  <script nonce="{escaped_script_nonce}">
    document.getElementById("wayforpay-checkout").submit();
  </script>
</body>
</html>"""
        return HTMLResponse(
            content=body,
            headers=_checkout_response_headers(form_action, script_nonce),
        )

    @app.api_route(
        "/checkout/complete",
        methods=["GET", "POST"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def public_checkout_complete(
        token: str = Query(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
        container: Container = Depends(container_from_request),
    ) -> HTMLResponse:
        telegram_id = (
            await container.subscription_service.checkout_owner_telegram_id_by_token(token)
        )
        bot_url = f"https://t.me/{quote(container.settings.bot_username, safe='')}"
        if telegram_id is not None:
            title = "Оплату передано на перевірку"
            message = (
                "Після підписаного підтвердження WayForPay бот автоматично активує "
                "підписку та надішле персональні посилання доступу. Додаткове "
                "підтвердження в боті не потрібне."
            )
            button = "Відкрити бота"
            destination_url = bot_url
        else:
            title = "Завершіть активацію"
            message = "Поверніться до Telegram-бота, щоб прив’язати оплату та отримати доступ."
            button = "Повернутися до бота"
            destination_url = f"{bot_url}?start=claim_{quote(token, safe='')}"
        escaped_destination_url = escape(destination_url, quote=True)
        body = f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Завершення оплати Neurokolo</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #0d1517; color: #f4f7f8; }}
    main {{ width: min(32rem, calc(100% - 3rem)); padding: 2.5rem;
      border: 1px solid #29464d; border-radius: 1.5rem; background: #142126; }}
    p {{ color: #b9c9cd; line-height: 1.6; }}
    a {{ display: block; margin-top: 1rem; padding: 1rem; border-radius: .8rem;
      background: #8bc7d3; color: #0d1517; text-align: center;
      text-decoration: none; font-weight: 700; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="{escaped_destination_url}">{button}</a>
  </main>
</body>
</html>"""
        return HTMLResponse(
            content=body,
            headers=_checkout_response_headers(),
        )

    return app
