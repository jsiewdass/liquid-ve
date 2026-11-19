# liquidVE

P2P automation engine for USDT/VES operations. It connects **Binance C2C**,
**Cachicamo Cash** (verification of incoming mobile payments) and an
**Android SMS gateway over Tailscale** (outgoing mobile payments and inbound
SMS confirmation).

## Stack

- FastAPI + Uvicorn (Python 3.11+), layered `core / domain / infrastructure / services / api`
- SQLAlchemy 2.0 async (SQLite for development, PostgreSQL-ready) + Alembic
- Pydantic v2 and `pydantic-settings` for strict, validated configuration
- `httpx` (retries, timeouts), `binance-connector`, `playwright`
- `loguru` structured JSON logs with secret/PII masking

## Safety invariants

- `order_number` and `bank_reference` are unique in the database and are
  persisted **before** any money movement, so retries cannot double-pay or
  double-release.
- Strict order state machine (`PENDING`, `PAYMENT_DETECTED`,
  `VERIFYING_CACHICAMO`, `SENDING_SMS`, `COMPLETED`, `FAILED`,
  `MANUAL_REVIEW`) with every transition written to an audit log.
- Per-transaction (`MAX_TX_AMOUNT_VES`) and daily (`DAILY_LIMIT_VES`) limits
  abort automation and park the order in `MANUAL_REVIEW`.
- Cachicamo webhooks are validated with HMAC-SHA256 plus timestamp tolerance
  (replay protection); the SMS gateway uses bearer authentication.

## Layout

```text
app/
  core/            configuration, security, structured logging, exceptions
  domain/          enums, Pydantic contracts
  infrastructure/  async database, ORM models, external clients
  services/        sell and buy orchestration
  api/v1/          health and webhook endpoints
  main.py          FastAPI app factory and lifespan
alembic/           async migrations
tests/             pytest + pytest-asyncio suite
```

## Quickstart

```bash
cp .env.example .env          # then fill in secrets and limits
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Run the checks:

```bash
pytest -q
ruff check app tests
```

The application fails fast at startup when required settings are missing or
insecure. Never commit a real `.env`.

## Status

Base scaffold with configuration, persistence, idempotency models, state
machine, risk limits, external client adapters, webhooks and health checks.
Binance C2C order release for standard (non-merchant) accounts still requires
a Playwright adapter, which is the next increment.
