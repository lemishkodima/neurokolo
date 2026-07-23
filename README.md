# Telegram Subscription Club

Production-oriented backend for a paid Telegram club. It uses Python 3.12+, aiogram 3,
FastAPI, PostgreSQL and WayForPay recurring payments.

## What is implemented

- Telegram personal cabinet with persistent menu.
- External WayForPay checkout-session API and signed payment callbacks.
- Monthly recurring payment configuration.
- Cancellation at period end through WayForPay `SUSPEND`.
- Personal, expiring invite links for every channel or forum included in a plan.
- Automatic post-payment confirmation with one-member invite buttons immediately after the
  Telegram account and successful checkout are matched.
- Automatic removal from all plan resources after expiration and an optional grace period.
- Multiple plans and arbitrary plan-to-resource mappings.
- Telegram admin panel for composing plans from channels/groups; the single-plan user flow stays
  hidden automatically.
- Automatic channel and supergroup registration when the bot is added or removed, with a
  notification to every active administrator after a new resource is connected.
- Rich-text and media-group broadcasts with previews, optional URL buttons, recipient snapshots,
  delivery retries, and per-campaign counters.
- Downloadable styled HTML statistics report.
- Runtime editing of the club description and user-menu button labels, plus configurable rich
  text, media albums, and URL buttons for every main menu action.
- Database-backed administrator access, anchored by immutable bootstrap administrator IDs.
- Referral attribution, qualification after the first payment, and a reward ledger ready for
  bonus-month, discount, or credit rules.
- Idempotent payment event processing and audit-friendly payment history.
- Immutable checkout/subscription amount and currency snapshots; a signed callback with mismatched
  payment terms is recorded for audit but never grants access.
- Entitlement reconciliation across overlapping subscriptions, so expiration of one subscription
  cannot remove access still covered by another.
- Separate API and worker processes so horizontal API scaling cannot run expiration work twice.

## Architecture

```text
src/club_bot/
├── api.py                  FastAPI endpoints and webhook authentication
├── bot/                    aiogram user/admin routers, states and keyboards
├── domain/                 enums and pure business rules
├── integrations/           WayForPay signing and API adapter
├── services/               users, subscriptions, access, admin, broadcasts, reports
├── models.py               SQLAlchemy persistence model
├── repositories.py         reusable database queries
├── container.py            dependency composition
├── worker.py               expiration/revocation process
└── cli.py                  plan and resource administration
```

The payment provider never directly changes Telegram access. A verified callback changes the
subscription state; the access service derives permitted Telegram resources from the selected
plan. This keeps provider, entitlement, and Telegram concerns separate.

The initial interface does not display or ask users to choose a tariff. `DEFAULT_PLAN_CODE`
selects the single active club offer. Multi-plan support stays internal and can be exposed later
without changing subscription or access storage.

## Local setup

1. Create the bot in `@BotFather`.
2. Copy `.env.example` to `.env` and fill in all secrets.
3. Add the bot as an administrator to every private channel and supergroup. It needs permission
   to invite and ban users. Enable Topics in the community supergroup if forum threads are needed.
4. Start PostgreSQL, apply the migration, and run the services:

```bash
docker compose up db -d
docker compose run --rm migrate
docker compose up api worker
```

For a Telegram test environment without a public webhook, run the dedicated polling service:

```bash
docker compose up -d bot worker
docker compose logs -f bot
```

Do not run the polling bot and Telegram webhook API against the same bot token at the same time.

For local bot development without a public Telegram webhook:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
PYTHONPATH=src .venv/bin/club-polling
```

WayForPay callbacks still require a public HTTPS URL even when Telegram uses polling.

## Telegram admin panel

Set the permanent owner IDs in `ADMIN_TELEGRAM_IDS` (JSON list) and restart the bot. The default is
`[402152266]`. Then send `/admin` to the bot.

The panel supports:

- creating tariffs and selecting their channel/group resources;
- reviewing resources captured automatically when the bot is added as an administrator;
- creating rich-text, media, and album broadcasts for all users or active subscribers;
- adding URL buttons to a broadcast and previewing it before queueing;
- downloading an HTML statistics report;
- editing the club description and menu labels;
- assigning a formatted post, photo/video, media album, and optional URL buttons to each main
  user-menu action;
- adding and revoking additional administrator IDs.

For Telegram access management, add the bot as an administrator to every private channel and
supergroup. It needs permissions to create invite links and restrict members. If the bot was
already present before this version was started, remove and re-add it once so Telegram emits the
membership update used for automatic registration.

When the database has one active tariff, users are never asked to select it; the configured
`DEFAULT_PLAN_CODE` is used. The tariff editor remains visible only to administrators.

## CLI configuration (alternative)

Create a plan:

```bash
club-admin seed-plan \
  --code base \
  --name "Клуб — базовий" \
  --price 990 \
  --currency UAH
```

Register the lesson channel and forum group using their numeric Telegram chat IDs:

```bash
club-admin seed-resource --code lessons --name "Уроки" \
  --chat-id -1001234567890 --type channel
club-admin seed-resource --code community --name "Спільнота" \
  --chat-id -1009876543210 --type supergroup
club-admin attach-resource --plan base --resource lessons
club-admin attach-resource --plan base --resource community
```

Run `club-admin seed-plan` again with the same code to update the plan. Additional tariffs are
created with different codes and linked to any desired resource set.

## Website checkout integration

For the built-in public checkout, direct the club button to:

```text
https://api.example.com/checkout
```

The endpoint creates a checkout server-side and renders a form that posts the signed
fields directly to WayForPay. After payment, `/checkout/complete` provides the
personal Telegram claim link. `INTERNAL_API_KEY` is never exposed to the browser.

For a separate website frontend, its backend can instead call:

The website backend calls:

```http
POST /api/v1/checkout-sessions
X-Internal-API-Key: <INTERNAL_API_KEY>
Content-Type: application/json

{
  "plan_code": null,
  "email": "member@example.com",
  "phone": "+380501234567",
  "referral_code": null,
  "return_url": "https://example.com/payment-complete"
}
```

The response contains:

- `gateway_url`: WayForPay form action;
- `gateway_fields`: signed fields to POST to the WayForPay checkout page;
- `bot_claim_url`: personal Telegram deep link shown after payment;
- `order_reference`: internal/provider correlation ID.

When `plan_code` is omitted or `null`, the API uses `DEFAULT_PLAN_CODE`. The website therefore
does not need to show tariff selection while the club has one offer.

Array fields `productName`, `productCount`, and `productPrice` must be submitted by the website as
WayForPay form array fields (`productName[]`, etc.). Never expose `INTERNAL_API_KEY` in browser
JavaScript.

Configure WayForPay to call `/webhooks/wayforpay`; the checkout payload already includes this
`serviceUrl`. Payment callbacks are accepted only after HMAC-MD5 signature verification.

## Subscription lifecycle

```text
checkout created → paid → claimed in bot → active
                                     ↘ failed renewal → past_due
cancel button → WayForPay suspended → active until period end
period end + grace period → expired → removed from all plan resources
```

WayForPay is the source of payment events. The database is the source of entitlement state.
Telegram membership is reconciled from that state by the worker.

## Production notes

- Use `compose.production.yml`; it runs Caddy, PostgreSQL, migrations, API, one worker, verified
  backups, and Prometheus. Detailed server instructions are in `ops/README.md`.
- Copy `.env.production.example` to `.env.production` and create the file-based Docker secrets
  documented in `secrets/README.md`. The application rejects placeholder domains, HTTP URLs,
  development database credentials, and short internal/webhook secrets in production mode.
- Caddy terminates HTTPS automatically. Only ports 80/443 are published; PostgreSQL and the
  application port stay inside the Compose network.
- The backup service writes a custom PostgreSQL archive and restores it into a temporary database
  before considering each backup successful.
- Prometheus is bound to localhost port 9090 and alerts on API downtime, elevated 5xx responses,
  and unmatched approved payments.
- Put the API behind HTTPS and restrict the internal checkout endpoint to the website backend.
- Use long random values for both webhook and internal API secrets.
- Set `PAYMENT_GRACE_PERIOD_HOURS=0` for strict removal at the exact paid-through time. A 24-hour
  grace period better accommodates WayForPay's retry on the following day.
- Back up PostgreSQL and monitor unmatched approved payments in the `payments` table.
- Use one worker replica unless a distributed scheduler and explicit job-claiming state are added.

Production startup:

```bash
docker compose --env-file .env.production -f compose.production.yml config --quiet
docker compose --env-file .env.production -f compose.production.yml up -d --build
docker compose --env-file .env.production -f compose.production.yml ps
```
