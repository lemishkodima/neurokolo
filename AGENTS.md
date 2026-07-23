# Neurokolo agent instructions

Before analyzing or changing this project:

1. Read `PROJECT_CONTEXT.md` completely.
2. Read the relevant sections of `README.md`.
3. Inspect the current Docker status, migrations, and uncommitted files instead of
   assuming the last recorded state is still current.
4. Never print, commit, or copy values from `.env`.
5. Preserve the subscription entitlement rules, WayForPay callback verification,
   payment idempotency, and access-until-period-end cancellation behavior described
   in `PROJECT_CONTEXT.md`.
6. Run pytest, Ruff, and mypy after implementation changes.

