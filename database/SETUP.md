# FlowSync — Database Setup Guide

## Files in this directory

| File | Purpose |
|---|---|
| `schema.sql` / `schema.txt` | Creates all tables, enums, triggers, RLS policies, indexes, and seed data |
| `views.sql` / `views.txt` | Creates read-only views used by the ML orchestrator |

The `.txt` versions are identical copies — use them for pasting directly into the AWS RDS Query Editor (which can be picky about `.sql` file extensions).

---

## What schema.sql creates

- **Enums** — `action_type_enum`, `approval_tier_enum`, `movement_type_enum`, etc.
- **28 tables** — clients, depots, users, products, batches, stock_movements, invoices, payments, ML output tables, agent_actions, audit_trail, temperature_logs, and more
- **2 roles** — `flowsync_app` (regular app, RLS enforced) and `flowsync_admin` (ML pipeline, bypasses RLS)
- **Triggers** — blocks any UPDATE or DELETE on `audit_trail` and `temperature_logs` (IT Act 2000 + WHO GDP compliance)
- **Row Level Security** — every tenant-scoped table is RLS-enabled; data is isolated per depot
- **Indexes** — on all high-traffic columns (depot_id, run_date, created_at, etc.)
- **Seed data** — 19 rows in `compliance_config` with all ML thresholds and config values

## What views.sql creates

4 views that enrich narrow ML output tables with joined fields from `products`, `batches`, and `demand_predictions`. These are queried by `ml/inference/orchestrator.py` in `_build_agent_state()`:

| View | Built from | Adds |
|---|---|---|
| `v_expiry_predictions` | `expiry_predictions` + `batches` + `products` | `remaining_qty`, `ptr`, `product_name` |
| `v_stockout_risks` | `stockout_calculations` + `products` + `demand_predictions` | `product_name`, `predicted_units_14d` |
| `v_fefo_rankings` | `fefo_rankings` + `batches` + `products` + `demand_predictions` | `units_available`, `expiry_date`, `product_name`, `predicted_units_14d` |
| `v_anomaly_flags` | `anomaly_flags` + `products` | `product_name` |

No data is stored in views — they are virtual queries only.

---

## Applying to AWS RDS (step by step)

### 1. Create the RDS instance
- Engine: **PostgreSQL 14+**
- Template: Free Tier is fine for dev; Production for prod
- DB name: `flowsync`
- Note down the **endpoint URL**, **port** (5432), and **master password**

### 2. Connect
You can use any of:
- **AWS RDS Query Editor** in the console (paste the `.txt` files directly)
- **psql** from your terminal:
  ```bash
  psql -h <rds-endpoint> -U postgres -d flowsync
  ```
- **pgAdmin** or **DBeaver** with the RDS endpoint

### 3. Run schema first
Paste the full contents of `schema.txt` and execute.

Expected output: no errors. You should see `CREATE TABLE`, `CREATE INDEX`, `CREATE TRIGGER`, etc.

### 4. Run views second
Paste the full contents of `views.txt` and execute.

Views depend on the tables created in step 3 — running views first will fail.

### 5. Verify
```sql
-- Should return 28 tables
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;

-- Should return 19 rows (all ML thresholds and config values)
SELECT key, value FROM compliance_config WHERE depot_id IS NULL;

-- Should return 4 views
SELECT table_name
FROM information_schema.views
WHERE table_schema = 'public';
```

---

## Environment variables

After running the schema, set these in your `.env`:

```
DATABASE_URL=postgresql://flowsync_app:change_me_app@<rds-endpoint>:5432/flowsync
ADMIN_DB_URL=postgresql://flowsync_admin:change_me_admin@<rds-endpoint>:5432/flowsync
```

**Change the passwords** from the defaults in the schema before going to production. You can do this in psql:
```sql
ALTER ROLE flowsync_app    WITH PASSWORD 'your-strong-password';
ALTER ROLE flowsync_admin  WITH PASSWORD 'your-strong-password';
```

---

## Role explanation

| Role | Used by | RLS | Why |
|---|---|---|---|
| `flowsync_app` | FastAPI backend (all HTTP routes) | Enforced — can only see rows matching `app.current_depot_id` | Tenant isolation |
| `flowsync_admin` | ML pipeline (`ADMIN_DB_URL`) | Bypassed | Orchestrator needs cross-depot access to run models for all depots |

The FastAPI middleware sets the session variable before every query:
```sql
SET LOCAL app.current_depot_id = '<depot-uuid>';
```

The ML admin connection never goes through any HTTP route — this is a hard rule enforced in `backend-rules.md`.

---

## Re-running the schema

The schema is safe to re-run — roles are created with `IF NOT EXISTS` and seed data uses `ON CONFLICT DO NOTHING`. Views use `CREATE OR REPLACE`.

If you need a clean slate during development:
```sql
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```
Then re-run `schema.txt` and `views.txt`.
