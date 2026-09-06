# Luxe Beauty Hub

HTML/CSS storefront with a Python backend, JSON seed data, and MySQL schema.

## Run locally

```powershell
python -m pip install -r requirements.txt
python server.py
```

Open `http://localhost:8000`.

## New backend foundation

The existing storefront remains available on port `8000`. The incremental FastAPI backend is under `backend/app` and runs separately while features are migrated:

```powershell
python -m pip install -r backend/requirements.txt
alembic upgrade head
pytest
python -m uvicorn backend.app.main:app --reload --port 8010
```

Health check: `http://localhost:8010/health`

The FastAPI service currently exposes backend/API routes only. The existing customer storefront remains at `http://localhost:8000` until the storefront migration is complete; `GET /` on port `8010` is expected to return 404 at this stage.

Run the backend tests:

```powershell
& ".venv/Scripts/python.exe" -m pytest backend/app/tests -q
```

Phase status:

- Phase 1: FastAPI configuration, CORS, health endpoint, SQLAlchemy session, Alembic foundation, and test runner.
- Phase 2: SQLAlchemy users, roles, permissions, categories, brands, products, variants, orders, order items, and payments models.
- Phase 3: scrypt password hashing, JWT tokens, and `/api/auth/register` and `/api/auth/login` foundations.
- Phase 4: protected FastAPI product CRUD routes with role checks and product schemas.
- Phase 5: public database-backed catalogue and category reads with pagination, search, filters, sorting, and product details.
- The legacy storefront remains the production-facing application until migrated routes have equivalent coverage.

Set `LUXE_ADMIN_EMAIL` and `LUXE_ADMIN_PASSWORD` before deployment. The legacy storefront bootstraps that account on startup: an existing matching customer is promoted to `SUPER_ADMIN`, otherwise a new admin account is created. MySQL settings are documented in `.env.example`; apply `schema.sql` to the `luxe` database before starting the app. The server reads `HOST` and `PORT` from the hosting platform and binds to `0.0.0.0` by default.

## Production release checklist

The relational backend requires a dedicated MySQL user, HTTPS, and real environment secrets. Copy `.env.example` to `.env`, replace every `generate-` and `your-` value, and never commit `.env`. Create the database user with only the privileges needed for the `luxe` database, then run `alembic upgrade head` from the repository root before releasing.

M-Pesa uses Safaricom Daraja STK Push. Configure the production shortcode, consumer credentials, passkey, and an HTTPS callback URL. The callback is idempotent and orders remain pending until Safaricom confirms payment. Do not mark M-Pesa payments as paid manually in the database.

The newer relational API is served by `backend.app.main:app`; the legacy storefront remains on `server.py` until its templates and checkout are migrated to the transactional API. Do not run both checkout implementations against the same production traffic without completing that migration.

The legacy server uses bounded concurrency to prevent thread exhaustion under traffic spikes. Set `MAX_CONCURRENT_REQUESTS` to match the host capacity; requests above the limit receive `503 Service Unavailable` with `Retry-After: 1`. Reaching very high throughput such as 100,000 requests per second requires a reverse proxy/load balancer, multiple application workers, connection-pooled database infrastructure, caching/static asset delivery, and asynchronous payment/email queues. It is not a single-process target.

## Deploy

The included `Procfile` starts the web process with `python server.py`. Configure the environment variables from `.env.example` in the hosting provider. Use HTTPS in production so secure session cookies are enabled with `APP_ENV=production`.
