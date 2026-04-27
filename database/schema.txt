-- =============================================================================
-- FlowSync Pharma SaaS — PostgreSQL Schema
-- Compatible: PostgreSQL 14+  |  AWS RDS PostgreSQL
-- Run as superuser (postgres) or a role with CREATEROLE + BYPASSRLS
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. EXTENSIONS
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- -----------------------------------------------------------------------------
-- 1. ROLES
--    app_user   → tenant-aware, RLS enforced  (use DATABASE_URL)
--    admin_user → ML pipeline, bypasses RLS   (use ADMIN_DB_URL)
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flowsync_app') THEN
        CREATE ROLE flowsync_app LOGIN PASSWORD 'change_me_app';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'flowsync_admin') THEN
        CREATE ROLE flowsync_admin LOGIN PASSWORD 'change_me_admin' BYPASSRLS;
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- 2. ENUMS
-- -----------------------------------------------------------------------------
CREATE TYPE action_type_enum AS ENUM (
    'ANOMALY_HOLD',
    'SUGGEST_LIQUIDATION',
    'FLAG_PRIORITY_DISPATCH',
    'SUGGEST_REORDER',
    'DISPATCH_PLAN',
    'ANOMALY_ALERT'
);

CREATE TYPE approval_tier_enum AS ENUM (
    'AUTO',
    'NOTIFY',
    'APPROVE'
);

CREATE TYPE movement_type_enum AS ENUM (
    'IN',
    'OUT',
    'RETURN',
    'WRITE_OFF',
    'TRANSFER'
);

CREATE TYPE run_status_enum AS ENUM (
    'SUCCESS',
    'PARTIAL',
    'FAILED'
);

CREATE TYPE payment_mode_enum AS ENUM (
    'CASH',
    'UPI',
    'CHEQUE',
    'NEFT',
    'RTGS'
);

CREATE TYPE reading_type_enum AS ENUM (
    'PHOTO',
    'SENSOR'
);

CREATE TYPE outcome_enum AS ENUM (
    'PENDING_APPROVAL',
    'APPROVED',
    'REJECTED',
    'EXECUTED'
);

CREATE TYPE notification_channel_enum AS ENUM (
    'WHATSAPP',
    'FIREBASE',
    'EMAIL',
    'SMS'
);

CREATE TYPE reconciliation_status_enum AS ENUM (
    'UNLINKED',
    'LINKED',
    'DISPUTED',
    'SETTLED',
    'PARTIAL',
    'ADVANCE'
);

-- -----------------------------------------------------------------------------
-- 3. CORE TENANT & USER TABLES  (no RLS — top-level entities)
-- -----------------------------------------------------------------------------

CREATE TABLE clients (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            VARCHAR(200)  NOT NULL,
    gstin           VARCHAR(15)   UNIQUE,
    plan_tier       VARCHAR(20)   NOT NULL DEFAULT 'starter',
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE TABLE depots (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id           UUID          NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name                VARCHAR(200)  NOT NULL,
    gstin               VARCHAR(15),
    address             TEXT,
    region              VARCHAR(50),
    license_number      VARCHAR(50),
    max_capacity_units  INTEGER       NOT NULL DEFAULT 10000,
    is_active           BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_depots_client ON depots(client_id);

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id        UUID          REFERENCES depots(id) ON DELETE SET NULL,
    client_id       UUID          NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name            VARCHAR(100)  NOT NULL,
    phone           VARCHAR(15),
    email           VARCHAR(100)  UNIQUE,
    role            VARCHAR(20)   NOT NULL,
    password_hash   VARCHAR(200)  NOT NULL,
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_users_depot  ON users(depot_id);
CREATE INDEX idx_users_client ON users(client_id);

-- Key-value config store — depot_id NULL means global default
CREATE TABLE compliance_config (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id    UUID          REFERENCES depots(id) ON DELETE CASCADE,
    key         VARCHAR(100)  NOT NULL,
    value       TEXT          NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, key)
);
CREATE INDEX idx_compliance_config_depot ON compliance_config(depot_id);

-- -----------------------------------------------------------------------------
-- 4. PRODUCT MASTER  (global — no RLS, shared across all tenants)
-- -----------------------------------------------------------------------------

CREATE TABLE products (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    canonical_name          VARCHAR(200)  NOT NULL,
    aliases                 TEXT[],
    gtin                    VARCHAR(14)   UNIQUE,
    hsn_code                VARCHAR(8),
    manufacturer            VARCHAR(100),
    product_category        VARCHAR(50),
    mrp                     NUMERIC(10,2),
    ptr                     NUMERIC(10,2),
    pts                     NUMERIC(10,2),
    is_cold_chain           BOOLEAN       NOT NULL DEFAULT FALSE,
    storage_temp_min        FLOAT,
    storage_temp_max        FLOAT,
    default_shelf_life_days INTEGER       NOT NULL DEFAULT 365,
    schedule_type           VARCHAR(10),
    lead_time_days          INTEGER       NOT NULL DEFAULT 7,
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_products_category ON products(product_category);
CREATE INDEX idx_products_name_trgm ON products USING GIN (canonical_name gin_trgm_ops);

-- -----------------------------------------------------------------------------
-- 5. OPERATIONAL TABLES  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE batches (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID          NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    product_id              UUID          NOT NULL REFERENCES products(id),
    batch_number            VARCHAR(50),
    expiry_date             DATE          NOT NULL,
    manufacturer            VARCHAR(100),
    quantity_received       INTEGER       NOT NULL,
    quantity_remaining      INTEGER       NOT NULL,
    invoice_id              UUID,         -- FK added after invoices table
    is_cold_chain           BOOLEAN       NOT NULL DEFAULT FALSE,
    default_shelf_life_days INTEGER       NOT NULL DEFAULT 365,
    product_category        VARCHAR(50),
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_batches_depot        ON batches(depot_id);
CREATE INDEX idx_batches_depot_prod   ON batches(depot_id, product_id);
CREATE INDEX idx_batches_expiry       ON batches(depot_id, expiry_date);

CREATE TABLE stock_movements (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id        UUID                NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    product_id      UUID                NOT NULL REFERENCES products(id),
    batch_id        UUID                NOT NULL REFERENCES batches(id),
    movement_type   movement_type_enum  NOT NULL,
    quantity        FLOAT               NOT NULL,
    performed_by    UUID                REFERENCES users(id) ON DELETE SET NULL,
    reference_id    UUID,
    notes           TEXT,
    created_at      TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_stock_mvmt_depot     ON stock_movements(depot_id);
CREATE INDEX idx_stock_mvmt_batch     ON stock_movements(batch_id);
CREATE INDEX idx_stock_mvmt_depot_ts  ON stock_movements(depot_id, created_at DESC);

CREATE TABLE iot_devices (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID          NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    device_id           VARCHAR(50)   NOT NULL UNIQUE,
    fridge_label        VARCHAR(50),
    is_active           BOOLEAN       NOT NULL DEFAULT TRUE,
    last_seen_at        TIMESTAMPTZ,
    firmware_version    VARCHAR(20),
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_iot_devices_depot ON iot_devices(depot_id);

-- -----------------------------------------------------------------------------
-- 6. FINANCIAL TABLES  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE retailers (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID           NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    name                VARCHAR(200)   NOT NULL,
    gstin               VARCHAR(15),
    address             TEXT,
    phone               VARCHAR(15),
    credit_limit        NUMERIC(12,2)  NOT NULL DEFAULT 50000,
    current_outstanding NUMERIC(12,2)  NOT NULL DEFAULT 0,
    dso_days            INTEGER        NOT NULL DEFAULT 0,
    credit_risk_score   FLOAT          NOT NULL DEFAULT 5.0,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_retailers_depot ON retailers(depot_id);

CREATE TABLE invoices (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID           NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    retailer_id             UUID           REFERENCES retailers(id) ON DELETE SET NULL,
    manufacturer_name       VARCHAR(100),
    invoice_number          VARCHAR(100),
    invoice_date            DATE,
    total_amount            NUMERIC(12,2),
    gst_amount              NUMERIC(12,2),
    status                  VARCHAR(20)    NOT NULL DEFAULT 'PENDING',
    ocr_confidence_score    FLOAT,
    original_image_url      VARCHAR(500),
    is_duplicate_flagged    BOOLEAN        NOT NULL DEFAULT FALSE,
    created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_invoices_depot    ON invoices(depot_id);
CREATE INDEX idx_invoices_retailer ON invoices(depot_id, retailer_id);

-- Back-fill FK on batches
ALTER TABLE batches ADD CONSTRAINT fk_batches_invoice
    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE SET NULL;

CREATE TABLE invoice_line_items (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    invoice_id              UUID           NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    product_id              UUID           REFERENCES products(id),
    batch_id                UUID           REFERENCES batches(id),
    quantity                INTEGER,
    mrp                     NUMERIC(10,2),
    ptr                     NUMERIC(10,2),
    pts                     NUMERIC(10,2),
    gst_percent             FLOAT,
    expiry_date_from_invoice DATE
);
CREATE INDEX idx_invoice_lines_invoice ON invoice_line_items(invoice_id);

CREATE TABLE payments (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID                        NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    invoice_id              UUID                        REFERENCES invoices(id) ON DELETE SET NULL,
    retailer_id             UUID                        REFERENCES retailers(id) ON DELETE SET NULL,
    amount_paid             NUMERIC(12,2)               NOT NULL,
    payment_mode            payment_mode_enum           NOT NULL DEFAULT 'CASH',
    collected_by            UUID                        REFERENCES users(id) ON DELETE SET NULL,
    gps_lat                 FLOAT,
    gps_lng                 FLOAT,
    photo_url               VARCHAR(500),
    reconciliation_status   reconciliation_status_enum  NOT NULL DEFAULT 'UNLINKED',
    collected_at            TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_payments_depot    ON payments(depot_id);
CREATE INDEX idx_payments_invoice  ON payments(invoice_id);
CREATE INDEX idx_payments_retailer ON payments(depot_id, retailer_id);

-- Cashflow transaction ledger — used by ReorderAgent to check cashflow_negative
CREATE TABLE payment_transactions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID           NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    payment_id          UUID           REFERENCES payments(id) ON DELETE SET NULL,
    amount              NUMERIC(12,2)  NOT NULL,
    transaction_type    VARCHAR(20)    NOT NULL,  -- INFLOW, OUTFLOW
    transaction_date    DATE           NOT NULL DEFAULT CURRENT_DATE,
    notes               TEXT,
    created_at          TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_pay_txn_depot_date ON payment_transactions(depot_id, transaction_date DESC);

CREATE TABLE returns (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID           NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    original_invoice_id     UUID           REFERENCES invoices(id) ON DELETE SET NULL,
    batch_id                UUID           REFERENCES batches(id) ON DELETE SET NULL,
    product_id              UUID           REFERENCES products(id),
    quantity_returned       INTEGER,
    return_reason           TEXT,
    photo_proof_url         VARCHAR(500),
    is_fake_return_flagged  BOOLEAN        NOT NULL DEFAULT FALSE,
    credit_note_id          UUID,          -- FK added after credit_notes
    created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_returns_depot ON returns(depot_id);

CREATE TABLE credit_notes (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id    UUID           NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    return_id   UUID           REFERENCES returns(id) ON DELETE SET NULL,
    invoice_id  UUID           REFERENCES invoices(id) ON DELETE SET NULL,
    amount      NUMERIC(12,2)  NOT NULL,
    status      VARCHAR(20)    NOT NULL DEFAULT 'ISSUED',
    issued_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_credit_notes_depot ON credit_notes(depot_id);

ALTER TABLE returns ADD CONSTRAINT fk_returns_credit_note
    FOREIGN KEY (credit_note_id) REFERENCES credit_notes(id) ON DELETE SET NULL;

-- -----------------------------------------------------------------------------
-- 7. ML OUTPUT TABLES  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE demand_predictions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID    NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    product_id          UUID    NOT NULL REFERENCES products(id),
    run_date            DATE    NOT NULL,
    predicted_units_14d FLOAT   NOT NULL,
    predicted_daily_rate FLOAT  NOT NULL,
    demand_trend_slope  FLOAT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, product_id, run_date)
);
CREATE INDEX idx_demand_pred_depot_date ON demand_predictions(depot_id, run_date DESC);

CREATE TABLE expiry_predictions (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                    UUID    NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    batch_id                    UUID    NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    run_date                    DATE    NOT NULL,
    expiry_risk_score           FLOAT   NOT NULL,
    recommended_liquidation_date DATE,
    method                      VARCHAR(10) NOT NULL DEFAULT 'model', -- 'model' or 'formula'
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, batch_id, run_date)
);
CREATE INDEX idx_expiry_pred_depot_date ON expiry_predictions(depot_id, run_date DESC);
CREATE INDEX idx_expiry_pred_batch      ON expiry_predictions(batch_id);

CREATE TABLE fefo_rankings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID    NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    batch_id            UUID    NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    run_date            DATE    NOT NULL,
    priority_rank       INTEGER NOT NULL,
    priority_score      FLOAT   NOT NULL,
    ml_override         BOOLEAN NOT NULL DEFAULT FALSE,
    expiry_risk_score   FLOAT,
    days_till_expiry    INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, batch_id, run_date)
);
CREATE INDEX idx_fefo_depot_date ON fefo_rankings(depot_id, run_date DESC);

CREATE TABLE stockout_calculations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID    NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    product_id          UUID    NOT NULL REFERENCES products(id),
    run_date            DATE    NOT NULL,
    current_stock       FLOAT   NOT NULL,
    days_until_stockout FLOAT,
    lead_time_days      FLOAT   NOT NULL,
    will_stockout       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, product_id, run_date)
);
CREATE INDEX idx_stockout_depot_date ON stockout_calculations(depot_id, run_date DESC);

CREATE TABLE anomaly_flags (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id        UUID                NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    movement_id     UUID                REFERENCES stock_movements(id) ON DELETE SET NULL,
    batch_id        UUID                REFERENCES batches(id) ON DELETE SET NULL,
    product_id      UUID                REFERENCES products(id),
    run_date        DATE                NOT NULL,
    z_score         FLOAT               NOT NULL,
    action          action_type_enum    NOT NULL,
    quantity        FLOAT,
    movement_type   movement_type_enum,
    product_name    VARCHAR(200),
    created_at      TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_anomaly_flags_depot_date ON anomaly_flags(depot_id, run_date DESC);

-- -----------------------------------------------------------------------------
-- 8. AGENT DECISION TABLE  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE agent_actions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id        UUID                NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    agent           VARCHAR(50)         NOT NULL,
    action_type     action_type_enum    NOT NULL,
    approval_tier   approval_tier_enum  NOT NULL,
    batch_id        UUID                REFERENCES batches(id) ON DELETE SET NULL,
    product_id      UUID                REFERENCES products(id),
    conflict_key    VARCHAR(200)        NOT NULL,
    payload         JSONB               NOT NULL DEFAULT '{}',
    extra_metadata  JSONB               NOT NULL DEFAULT '{}',
    outcome         outcome_enum        NOT NULL DEFAULT 'PENDING_APPROVAL',
    decided_by      UUID                REFERENCES users(id) ON DELETE SET NULL,
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    -- Prevents duplicate agent actions for the same conflict_key on the same calendar day
    UNIQUE (depot_id, conflict_key, (created_at::date))
);
CREATE INDEX idx_agent_actions_depot      ON agent_actions(depot_id);
CREATE INDEX idx_agent_actions_depot_ts   ON agent_actions(depot_id, created_at DESC);
CREATE INDEX idx_agent_actions_outcome    ON agent_actions(depot_id, outcome);
CREATE INDEX idx_agent_actions_payload    ON agent_actions USING GIN (payload);

-- -----------------------------------------------------------------------------
-- 9. PIPELINE & MONITORING TABLES  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE pipeline_run_logs (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id    UUID            NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    run_date    DATE            NOT NULL,
    status      run_status_enum NOT NULL,
    stages      JSONB           NOT NULL DEFAULT '{}',
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    UNIQUE (depot_id, run_date)
);
CREATE INDEX idx_pipeline_runs_depot ON pipeline_run_logs(depot_id, run_date DESC);

-- Global — no RLS (model versions are system-wide)
CREATE TABLE model_registry (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_name      VARCHAR(100)    NOT NULL,
    version         VARCHAR(30)     NOT NULL,
    s3_key          VARCHAR(300)    NOT NULL,
    trained_at      TIMESTAMPTZ     NOT NULL,
    is_active       BOOLEAN         NOT NULL DEFAULT TRUE,
    extra_metadata  JSONB           NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_model_registry_name ON model_registry(model_name, is_active);

-- Global — no RLS
CREATE TABLE drift_logs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model_name          VARCHAR(100)    NOT NULL,
    psi_score           FLOAT           NOT NULL,
    retraining_needed   BOOLEAN         NOT NULL DEFAULT FALSE,
    checked_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_drift_logs_model ON drift_logs(model_name, checked_at DESC);

CREATE TABLE system_alerts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id    UUID            REFERENCES depots(id) ON DELETE CASCADE,
    alert_type  VARCHAR(50)     NOT NULL,
    message     TEXT            NOT NULL,
    resolved    BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_system_alerts_depot ON system_alerts(depot_id, resolved);

CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID                        REFERENCES depots(id) ON DELETE CASCADE,
    recipient_user_id   UUID                        REFERENCES users(id) ON DELETE SET NULL,
    channel             notification_channel_enum   NOT NULL,
    notification_type   VARCHAR(50)                 NOT NULL,
    message_template_id VARCHAR(50),
    payload             JSONB                       NOT NULL DEFAULT '{}',
    status              VARCHAR(20)                 NOT NULL DEFAULT 'QUEUED',
    sent_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ                 NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_notifications_depot  ON notifications(depot_id);
CREATE INDEX idx_notifications_status ON notifications(status, created_at);

-- -----------------------------------------------------------------------------
-- 10. COMPLIANCE TABLES  (INSERT-ONLY — protected by triggers below)
-- -----------------------------------------------------------------------------

-- IT Act 2000 § 4 — immutable audit log
CREATE TABLE audit_trail (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_type      VARCHAR(50)     NOT NULL,
    entity_table    VARCHAR(50)     NOT NULL,
    entity_id       UUID,
    performed_by    UUID            REFERENCES users(id) ON DELETE SET NULL,
    old_value       JSONB,
    new_value       JSONB,
    ip_address      VARCHAR(45),
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_trail_entity ON audit_trail(entity_table, entity_id);
CREATE INDEX idx_audit_trail_ts     ON audit_trail(created_at DESC);

-- WHO GDP Guidelines — immutable cold-chain temperature log
CREATE TABLE temperature_logs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID                NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    batch_id                UUID                REFERENCES batches(id) ON DELETE SET NULL,
    device_id               VARCHAR(50),
    reading_value           FLOAT               NOT NULL,
    reading_type            reading_type_enum   NOT NULL DEFAULT 'SENSOR',
    photo_url               VARCHAR(500),
    is_excursion            BOOLEAN             NOT NULL DEFAULT FALSE,
    excursion_threshold_min FLOAT,
    excursion_threshold_max FLOAT,
    gps_lat                 FLOAT,
    gps_lng                 FLOAT,
    logged_by               UUID                REFERENCES users(id) ON DELETE SET NULL,
    logged_at               TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_temp_logs_depot    ON temperature_logs(depot_id, logged_at DESC);
CREATE INDEX idx_temp_logs_batch    ON temperature_logs(batch_id);
CREATE INDEX idx_temp_logs_device   ON temperature_logs(device_id, logged_at DESC);

-- -----------------------------------------------------------------------------
-- 11. REGULATORY TABLES  (tenant-scoped — RLS on depot_id)
-- -----------------------------------------------------------------------------

CREATE TABLE recalls (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id                UUID          NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    batch_id                UUID          NOT NULL REFERENCES batches(id),
    product_id              UUID          NOT NULL REFERENCES products(id),
    recall_issued_by        VARCHAR(100),
    cdsco_reference_number  VARCHAR(50),
    recall_date             DATE,
    affected_quantity       INTEGER,
    status                  VARCHAR(20)   NOT NULL DEFAULT 'INITIATED',
    completion_report_url   VARCHAR(500),
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_recalls_depot ON recalls(depot_id);

CREATE TABLE discount_schemes (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    depot_id            UUID          NOT NULL REFERENCES depots(id) ON DELETE CASCADE,
    manufacturer_name   VARCHAR(100),
    scheme_name         VARCHAR(100),
    scheme_type         VARCHAR(50),
    terms               JSONB         NOT NULL DEFAULT '{}',
    valid_from          DATE,
    valid_to            DATE,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_discount_schemes_depot ON discount_schemes(depot_id);

-- -----------------------------------------------------------------------------
-- 12. TRIGGERS — Enforce INSERT-ONLY on audit_trail & temperature_logs
-- -----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_block_update_delete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Immutable table: UPDATE and DELETE are not permitted on % (IT Act 2000 / WHO GDP)',
        TG_TABLE_NAME;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_audit_trail_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_trail
    FOR EACH ROW EXECUTE FUNCTION fn_block_update_delete();

CREATE TRIGGER trg_temperature_logs_no_update_delete
    BEFORE UPDATE OR DELETE ON temperature_logs
    FOR EACH ROW EXECUTE FUNCTION fn_block_update_delete();

-- -----------------------------------------------------------------------------
-- 13. ROW LEVEL SECURITY (RLS)
--     The FastAPI middleware sets: SET LOCAL app.current_depot_id = '<uuid>'
--     The ML admin connection uses flowsync_admin role (BYPASSRLS).
-- -----------------------------------------------------------------------------

-- Helper: safely read the session variable (returns NULL if not set)
CREATE OR REPLACE FUNCTION current_depot_id() RETURNS UUID LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_depot_id', TRUE), '')::UUID;
$$;

-- Tables that are RLS-scoped to depot_id
DO $$
DECLARE
    tbl TEXT;
    tbls TEXT[] := ARRAY[
        'batches', 'stock_movements', 'iot_devices',
        'retailers', 'invoices', 'payments', 'payment_transactions',
        'returns', 'credit_notes',
        'demand_predictions', 'expiry_predictions', 'fefo_rankings',
        'stockout_calculations', 'anomaly_flags',
        'agent_actions', 'pipeline_run_logs',
        'temperature_logs', 'recalls', 'discount_schemes',
        'notifications'
    ];
BEGIN
    FOREACH tbl IN ARRAY tbls LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        -- SELECT/INSERT/UPDATE/DELETE all require depot_id to match session variable
        EXECUTE format(
            'CREATE POLICY depot_isolation ON %I
             USING (depot_id = current_depot_id())', tbl);
    END LOOP;
END $$;

-- system_alerts: depot_id is nullable (global alerts visible to all)
ALTER TABLE system_alerts ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_alerts_isolation ON system_alerts
    USING (depot_id IS NULL OR depot_id = current_depot_id());

-- audit_trail: visible to all authenticated users (no depot filter — cross-depot audit)
ALTER TABLE audit_trail ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_trail_read_all ON audit_trail USING (TRUE);

-- Grant app role usage
GRANT USAGE ON SCHEMA public TO flowsync_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO flowsync_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO flowsync_admin;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO flowsync_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO flowsync_admin;

-- -----------------------------------------------------------------------------
-- 14. SEED DATA — compliance_config (global defaults, depot_id = NULL)
-- -----------------------------------------------------------------------------

INSERT INTO compliance_config (depot_id, key, value, description) VALUES
    (NULL, 'fefo_override_threshold',      '0.6',   'Expiry risk above this pushes batch to top of FEFO (Schedule M)'),
    (NULL, 'expiry_critical_threshold',    '0.85',  'Risk >= this triggers SUGGEST_LIQUIDATION'),
    (NULL, 'expiry_warning_threshold',     '0.60',  'Risk >= this triggers FLAG_PRIORITY_DISPATCH'),
    (NULL, 'anomaly_hold_threshold',       '2.5',   'Z-score >= this triggers ANOMALY_HOLD (APPROVE tier)'),
    (NULL, 'anomaly_alert_threshold',      '2.0',   'Z-score >= this triggers ANOMALY_ALERT (NOTIFY tier)'),
    (NULL, 'default_lead_time_days',       '7',     'Days before stockout to issue reorder'),
    (NULL, 'approval_expiry_hours',        '24',    'Hours until APPROVE-tier action auto-expires'),
    (NULL, 'dso_warning_threshold',        '60',    'Days Sales Outstanding above which credit_risk_score penalised'),
    (NULL, 'temp_cold_chain_min',          '2.0',   'Cold-chain lower bound °C (WHO GDP)'),
    (NULL, 'temp_cold_chain_max',          '8.0',   'Cold-chain upper bound °C (WHO GDP)'),
    (NULL, 'temp_deep_freeze',             '-20.0', 'Deep-freeze target °C'),
    (NULL, 'temp_general_min',             '15.0',  'Ambient storage lower bound °C'),
    (NULL, 'temp_general_max',             '25.0',  'Ambient storage upper bound °C'),
    (NULL, 'mape_target',                  '0.15',  'Demand model MAPE pass threshold'),
    (NULL, 'auc_target',                   '0.80',  'Expiry model AUC pass threshold'),
    (NULL, 'recall_target',                '0.85',  'Anomaly detection recall pass threshold'),
    (NULL, 'psi_alert_threshold',          '0.20',  'PSI above this triggers auto-retraining'),
    (NULL, 'cold_start_demand_days',       '90',    'Client data needed before switching from global model'),
    (NULL, 'cold_start_expiry_days',       '180',   'Client data + 50 labels needed for expiry model')
ON CONFLICT (depot_id, key) DO NOTHING;
