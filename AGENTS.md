# HackEvents — Agent Rules

## Ground rules
- Read /docs before changing behaviour. The PRD is the source of truth for scope.
- Evidence first: a change is done only after tests, type-checks and lint pass locally. Show the command output.
- Never claim something works without running it.
- Smallest change that solves the ticket. No speculative abstractions.

## Never
- Never commit secrets, .env, cookies, tokens, or real user data.
- Never log secrets, cookies, Authorization headers, OAuth tokens, or FCM tokens.
- Never ask the user for social-media passwords; social access is cookie upload by Admin only.
- Never add tools/function-calling to the extraction LLM.
- Never render scraped HTML as HTML.
- Never bypass the LLM cost-cap check.
- Never disable the SSRF guard, CSRF, row-ownership checks, or rate limits to make a test pass.
- Never run heavy jobs on any shared production server.
- Never edit an applied Alembic migration.
- Never build anti-ban or detection-evasion tooling for social networks.

## Always
- Store times in UTC; convert at the edge using the user's timezone (default Asia/Kolkata).
- Keep registration_deadline and event dates separate.
- Every new endpoint: role dependency + ownership filter + a cross-user access test.
- Every new source adapter: a saved fixture page + test.
- Every LLM prompt/schema change: bump schema_version and run the golden set for each provider.
- Every reminder change: keep the unique key (user, event, kind, offset, channel).
- Reference the domain only via APP_DOMAIN.

## Commands
- Backend: `uv run pytest`, `uv run ruff check`, `uv run mypy`
- Frontend: `pnpm test`, `pnpm lint`, `pnpm typecheck`, `pnpm build`
- E2E: `pnpm e2e`
- Full stack: `docker compose up -d && ./infra/smoke.sh`
