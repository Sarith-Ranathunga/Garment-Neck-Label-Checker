-- Migration 002 — operator accounts and shipment assignment.
--
-- Run once against an existing database. schema.sql only executes when the
-- Postgres data directory is empty, so an existing deployment never sees it:
--
--     docker compose exec -T db psql -U gnlc -d gnlc \
--       < db/migration_002_users_and_assignment.sql
--
-- Two changes:
--
--  * `shipment_operator` records which Android users may scan a shipment.
--    The assignment is ENFORCED, not advisory: client_api refuses a
--    checklist and main.py refuses a scan when the operator is not on the
--    list. That is why the table has a unique constraint rather than being
--    a free-form note.
--
--  * Seeded accounts carry the placeholder hash 'x', which matches no
--    password. They are given a real bcrypt hash so the existing demo
--    accounts can still sign in after this migration.

BEGIN;

CREATE TABLE IF NOT EXISTS shipment_operator (
    id          SERIAL PRIMARY KEY,
    shipment_id INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    user_id     INT NOT NULL REFERENCES app_user(id),
    assigned_by INT REFERENCES app_user(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (shipment_id, user_id)
);

CREATE INDEX IF NOT EXISTS ix_shipment_operator_user
    ON shipment_operator (user_id);

-- bcrypt, cost 12, of the password 'gnlc1234'.
-- ⚠️ Change it from Users on the supervisor interface before the line runs.
UPDATE app_user
   SET password_hash = '$2b$12$EKp/r/xlGSEdSNaxat09aOgx2ttXkBY6iTaNQwJzGXmhtNS6CWEyG'
 WHERE password_hash = 'x';

-- Every existing worker and supervisor is assigned to every active shipment,
-- so the enforcement introduced here does not stop a line that is mid-run.
INSERT INTO shipment_operator (shipment_id, user_id)
SELECT s.id, u.id
  FROM shipment s
  CROSS JOIN app_user u
  JOIN role r ON r.id = u.role_id
 WHERE u.active
   AND r.code IN ('worker', 'supervisor')
   AND s.status <> 'closed'
ON CONFLICT (shipment_id, user_id) DO NOTHING;

COMMIT;
