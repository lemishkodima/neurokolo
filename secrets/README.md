# Production secret files

Create the following extensionless files on the server. Each file contains only the
value and a trailing newline is optional:

- `postgres_password`
- `database_url`
- `bot_token`
- `bot_webhook_secret`
- `internal_api_key`
- `wayforpay_secret_key`
- `wayforpay_merchant_password`

`database_url` must use the same PostgreSQL password, for example in the form
`postgresql+asyncpg://club:<password>@db:5432/club`.

Generate new `postgres_password`, `bot_webhook_secret`, and `internal_api_key`
values for production. Rotate provider secrets that may have previously been shared.
Never copy the local `.env` file into Git or an image.
