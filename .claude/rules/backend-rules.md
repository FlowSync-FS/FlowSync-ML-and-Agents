# Backend Rules — Non-Negotiable

## Multi-Tenancy
Every tenant-scoped table has RLS enabled.
FastAPI middleware sets: SET app.current_depot_id = '{depot_id}'
ML pipeline uses admin connection that bypasses RLS.
Never expose the admin connection through any HTTP route.

## Immutable Tables
audit_trail → INSERT only (IT Act 2000)
temperature_logs → INSERT only (WHO GDP Guidelines)
Any trigger that removes this protection is a legal compliance violation.

## Reconciliation Rules (4 rules, first match wins)
1. Exact match: amount == invoice ±1% AND same retailer → SETTLED
2. Partial: amount < invoice, same retailer, ≥10% → PARTIAL
3. Consolidated: amount == sum of 2-5 open invoices ±2% → SETTLED all
4. Advance: no invoice yet, retailer known → ADVANCE
5. None → UNLINKED → manual review

## Router Pattern
Routers: validate input, call service, return response. No business logic.
Services: pure business logic, no HTTP knowledge.
Never put a database query directly in a router.

## Celery
celery_beat: exactly ONE replica. Never scale this service.
celery_worker: scale horizontally, max_concurrency=4 per worker.

## Environment Variables Required
DATABASE_URL, ADMIN_DB_URL, REDIS_URL, S3_BUCKET,
AWS_ACCESS_KEY, AWS_SECRET_KEY, JWT_SECRET,
WHATSAPP_TOKEN, SENDGRID_KEY, FIREBASE_KEY, ENV