-- ============================================================================
--  Garment Neck Label Checker (GNLC)
--  PostgreSQL schema
--
--  S. J. Ranathunga · COBSCCOMP242P-011 · NIBM School of Computing & Engineering
--
--  Design notes that matter for the research:
--
--  * There is no `garment` table. Garments are not individually identified,
--    and that absence IS the research problem (P1/P2). Adding one would
--    contradict Chapter 1.
--
--  * `buyer_size_code_map` is what prevents a hangtag reading 2XL against a
--    neck label reading XXL being counted as an identification error. Without
--    it the O2 false-positive threshold is unreachable.
--
--  * `shipment_allocation` defines the valid size set for a shipment. That set
--    is what constrains the OCR parse, which is the operational form of the
--    Kralicek et al. (2019) vocabulary-constraint argument behind the O1
--    threshold.
--
--  * `barcode_map` exists because the hangtag barcode is a plain 12-digit
--    UPC-A (e.g. 8 40395 61952 0). It carries NO size, style or colour — it
--    is only an identifier. The size must therefore be looked up, not parsed.
--    Confirmed from a real hangtag at the case company, 11 Oct 2026.
--
--  * `exception_event.recognition_result_id` is NULLABLE ON PURPOSE. A manual
--    entry with a NULL here is a manual entry made when no recognition was
--    attempted or none failed — i.e. a bypass. That single nullable column is
--    the direct test of DP3 (§5.8).
--
--  * `scan_event` carries three timestamps and a sync state so that end-to-end
--    latency (§5.7) and deferred-verdict incidents can both be measured after
--    the fact rather than estimated.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------- enums ----
CREATE TYPE stage_t        AS ENUM ('S1', 'S5', 'S6');
CREATE TYPE read_outcome_t AS ENUM ('correct', 'wrong_read', 'no_read');
CREATE TYPE verdict_t      AS ENUM ('match', 'mismatch', 'no_read', 'out_of_context');
CREATE TYPE sync_state_t   AS ENUM ('queued', 'sent', 'acknowledged', 'failed');
CREATE TYPE entry_method_t AS ENUM ('recognised', 'manual');
CREATE TYPE shipment_st_t  AS ENUM ('draft', 'active', 'closed');

-- ======================================================== reference data ====

CREATE TABLE role (
    id          SMALLSERIAL PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,           -- manager | supervisor | worker | admin
    description TEXT NOT NULL
);

CREATE TABLE app_user (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role_id       SMALLINT NOT NULL REFERENCES role(id),
    password_hash TEXT NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE buyer (
    id         SERIAL PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE style (
    id          SERIAL PRIMARY KEY,
    buyer_id    INT NOT NULL REFERENCES buyer(id),
    style_no    TEXT NOT NULL,
    description TEXT,
    UNIQUE (buyer_id, style_no)
);

CREATE TABLE colour (
    id   SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- Canonical sizes. Buyer-specific notation is resolved against this.
CREATE TABLE size_code (
    id        SERIAL PRIMARY KEY,
    canonical TEXT NOT NULL UNIQUE,             -- S, M, L, XL, 2XL, 110, 5Y ...
    sort_order SMALLINT NOT NULL
);

-- The entity that stops notation differences being read as defects.
CREATE TABLE buyer_size_code_map (
    id            SERIAL PRIMARY KEY,
    buyer_id      INT  NOT NULL REFERENCES buyer(id) ON DELETE CASCADE,
    size_code_id  INT  NOT NULL REFERENCES size_code(id),
    buyer_notation TEXT NOT NULL,               -- 'XXL' where canonical is '2XL'
    UNIQUE (buyer_id, buyer_notation)
);
CREATE INDEX ix_bscm_buyer ON buyer_size_code_map (buyer_id);

-- ---------------------------------------------------------------------------
--  The hangtag barcode is a plain numeric UPC-A / EAN. It encodes nothing:
--  not the size, not the style, not the colour. Scanning it yields only an
--  identifier, so every attribute the artefact compares against must be
--  resolved through this table.
--
--  This is why O2 is a LOOKUP problem on the hangtag side and a RECOGNITION
--  problem on the neck-label side. The two identifiers fail in different
--  ways: a barcode is either read correctly or not read at all, while a
--  printed label can be read WRONGLY. That asymmetry is the reason the neck
--  label is the hard half of the comparison.
-- ---------------------------------------------------------------------------
CREATE TABLE barcode_map (
    id           SERIAL PRIMARY KEY,
    barcode      TEXT NOT NULL UNIQUE,          -- as decoded, digits only
    symbology    TEXT,                          -- UPC_A, EAN_13, CODE_128 ...
    buyer_id     INT NOT NULL REFERENCES buyer(id),
    style_id     INT NOT NULL REFERENCES style(id),
    colour_id    INT NOT NULL REFERENCES colour(id),
    size_code_id INT NOT NULL REFERENCES size_code(id),
    sku          TEXT,                          -- buyer SKU, appears on the packing list
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_barcode_buyer ON barcode_map (buyer_id);
CREATE INDEX ix_barcode_lookup ON barcode_map (style_id, colour_id, size_code_id);

-- ==================================================== order and shipment ====

CREATE TABLE purchase_order (
    id          SERIAL PRIMARY KEY,
    po_number   TEXT NOT NULL UNIQUE,
    buyer_id    INT  NOT NULL REFERENCES buyer(id),
    style_id    INT  NOT NULL REFERENCES style(id),
    total_qty   INT  NOT NULL CHECK (total_qty > 0),
    created_by  INT  NOT NULL REFERENCES app_user(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shipment (
    id          SERIAL PRIMARY KEY,
    po_id       INT  NOT NULL REFERENCES purchase_order(id) ON DELETE CASCADE,
    reference   TEXT NOT NULL UNIQUE,
    ship_date   DATE NOT NULL,
    declared_qty INT NOT NULL CHECK (declared_qty > 0),  -- the stated total
    status      shipment_st_t NOT NULL DEFAULT 'draft',
    created_by  INT  NOT NULL REFERENCES app_user(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
CREATE INDEX ix_shipment_status ON shipment (status);

-- Per-size quantities. The sum is validated against shipment.declared_qty in
-- the service layer before a shipment may become 'active' (O3).
-- This table also defines the valid size set that constrains the OCR parse.
CREATE TABLE shipment_allocation (
    id           SERIAL PRIMARY KEY,
    shipment_id  INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    size_code_id INT NOT NULL REFERENCES size_code(id),
    colour_id    INT NOT NULL REFERENCES colour(id),
    qty          INT NOT NULL CHECK (qty >= 0),
    UNIQUE (shipment_id, size_code_id, colour_id)
);
CREATE INDEX ix_alloc_shipment ON shipment_allocation (shipment_id);

-- ============================================================ operational ===

-- One checklist per style+colour set arriving at a stage.
CREATE TABLE checklist (
    id          SERIAL PRIMARY KEY,
    shipment_id INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    style_id    INT NOT NULL REFERENCES style(id),
    colour_id   INT NOT NULL REFERENCES colour(id),
    stage       stage_t NOT NULL,
    opened_by   INT NOT NULL REFERENCES app_user(id),
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
CREATE INDEX ix_checklist_shipment ON checklist (shipment_id, stage);

CREATE TABLE checklist_item (
    id           SERIAL PRIMARY KEY,
    checklist_id INT NOT NULL REFERENCES checklist(id) ON DELETE CASCADE,
    size_code_id INT NOT NULL REFERENCES size_code(id),
    expected_qty INT NOT NULL CHECK (expected_qty >= 0),
    verified_qty INT NOT NULL DEFAULT 0,
    UNIQUE (checklist_id, size_code_id)
);

CREATE TABLE carton (
    id           SERIAL PRIMARY KEY,
    shipment_id  INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    carton_no    TEXT NOT NULL,
    -- Weights appear on the buyer's packing list. They are DECLARED by the
    -- operator, not captured by the artefact, and are reported as a
    -- field-map gap in O3.
    net_weight_kg   NUMERIC(8,3),
    gross_weight_kg NUMERIC(8,3),
    opened_by    INT NOT NULL REFERENCES app_user(id),
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,
    UNIQUE (shipment_id, carton_no)
);

CREATE TABLE carton_item (
    id           SERIAL PRIMARY KEY,
    carton_id    INT NOT NULL REFERENCES carton(id) ON DELETE CASCADE,
    size_code_id INT NOT NULL REFERENCES size_code(id),
    qty          INT NOT NULL CHECK (qty >= 0),
    UNIQUE (carton_id, size_code_id)
);

-- ============================================================ verification ==

-- One row per scan, at any stage. Carries the sync state and the three
-- timestamps needed for end-to-end latency (§5.7).
CREATE TABLE scan_event (
    id                 BIGSERIAL PRIMARY KEY,
    local_id           UUID NOT NULL,            -- generated on the device
    device_id          TEXT NOT NULL,
    operator_id        INT  NOT NULL REFERENCES app_user(id),
    checklist_id       INT  REFERENCES checklist(id),
    carton_id          INT  REFERENCES carton(id),
    stage              stage_t NOT NULL,

    hangtag_barcode    TEXT,                     -- NULL at S1: no hangtag yet
    barcode_map_id     INT REFERENCES barcode_map(id),   -- NULL = barcode unknown
    hangtag_size_raw   TEXT,                     -- buyer notation, from the lookup
    hangtag_size_id    INT REFERENCES size_code(id),

    -- three timestamps: capture -> response -> displayed
    captured_at        TIMESTAMPTZ NOT NULL,
    responded_at       TIMESTAMPTZ,
    displayed_at       TIMESTAMPTZ,

    -- offline queue
    sync_state         sync_state_t NOT NULL DEFAULT 'queued',
    queued_at          TIMESTAMPTZ,
    synced_at          TIMESTAMPTZ,

    verdict            verdict_t,
    entry_method       entry_method_t NOT NULL DEFAULT 'recognised',
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id, local_id)
);
CREATE INDEX ix_scan_checklist ON scan_event (checklist_id);
CREATE INDEX ix_scan_stage_time ON scan_event (stage, captured_at);
CREATE INDEX ix_scan_operator ON scan_event (operator_id);
CREATE INDEX ix_scan_verdict ON scan_event (verdict);

-- What the recognition engine returned. One row per scan that reached OCR.
CREATE TABLE recognition_result (
    id               BIGSERIAL PRIMARY KEY,
    scan_event_id    BIGINT NOT NULL UNIQUE REFERENCES scan_event(id) ON DELETE CASCADE,
    engine           TEXT NOT NULL,              -- 'easyocr'
    engine_version   TEXT,
    rotation_used    SMALLINT,                   -- 0 | 90 | 180 | 270, NULL if none succeeded
    rotations_tried  SMALLINT NOT NULL DEFAULT 1,
    raw_text         TEXT,
    parsed_size_id   INT REFERENCES size_code(id),
    confidence       NUMERIC(5,4),               -- captured this time
    outcome          read_outcome_t NOT NULL,
    duration_ms      INT,
    image_path       TEXT                        -- retained only for wrong_read / no_read / mismatch
);
CREATE INDEX ix_recog_outcome ON recognition_result (outcome);

CREATE TABLE mismatch (
    id             BIGSERIAL PRIMARY KEY,
    scan_event_id  BIGINT NOT NULL REFERENCES scan_event(id) ON DELETE CASCADE,
    neck_size_id   INT REFERENCES size_code(id),
    hangtag_size_id INT REFERENCES size_code(id),
    carton_id      INT REFERENCES carton(id),
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ,
    resolution_note TEXT
);
CREATE INDEX ix_mismatch_carton ON mismatch (carton_id);

-- Manual entries and fallbacks.
--
-- ⭐ recognition_result_id IS NULLABLE BY DESIGN.
--    NOT NULL  -> recognition was attempted and returned no_read: a legitimate
--                 fallback.
--    NULL      -> a manual entry made with no failed recognition behind it:
--                 a BYPASS of the control. This is the direct measure of DP3
--                 reported in §5.8.
CREATE TABLE exception_event (
    id                    BIGSERIAL PRIMARY KEY,
    scan_event_id         BIGINT NOT NULL REFERENCES scan_event(id) ON DELETE CASCADE,
    recognition_result_id BIGINT REFERENCES recognition_result(id),
    entered_size_id       INT REFERENCES size_code(id),
    reason                TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_exception_recog ON exception_event (recognition_result_id);

-- ============================================================== packing =====

CREATE TABLE packing_list (
    id           SERIAL PRIMARY KEY,
    shipment_id  INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_by INT NOT NULL REFERENCES app_user(id),
    UNIQUE (shipment_id, generated_at)
);

CREATE TABLE packing_list_line (
    id              SERIAL PRIMARY KEY,
    packing_list_id INT NOT NULL REFERENCES packing_list(id) ON DELETE CASCADE,
    carton_id       INT NOT NULL REFERENCES carton(id),
    size_code_id    INT NOT NULL REFERENCES size_code(id),
    qty             INT NOT NULL CHECK (qty >= 0),
    net_weight_kg   NUMERIC(8,3),
    gross_weight_kg NUMERIC(8,3)     -- declared as a field-map gap; see future work
);
CREATE INDEX ix_pll_list ON packing_list_line (packing_list_id);

-- ============================================================ evaluation ====

-- Operator asked the supervisor for help. Feeds §5.8 usability.
CREATE TABLE assistance_event (
    id          BIGSERIAL PRIMARY KEY,
    operator_id INT  NOT NULL REFERENCES app_user(id),
    shipment_id INT  REFERENCES shipment(id),
    stage       stage_t NOT NULL,
    cause       TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note        TEXT
);
CREATE INDEX ix_assist_operator ON assistance_event (operator_id, occurred_at);

-- ======================================================== operations log ====

-- Per-shipment Excel export. Host is the only writer: no concurrency.
CREATE TABLE export_log (
    id           BIGSERIAL PRIMARY KEY,
    shipment_id  INT NOT NULL REFERENCES shipment(id) ON DELETE CASCADE,
    file_path    TEXT NOT NULL,
    row_counts   JSONB,
    succeeded    BOOLEAN NOT NULL,
    error_text   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_export_shipment ON export_log (shipment_id, created_at DESC);

-- 15-minute USB backup. verified_at proves the restore test was performed.
CREATE TABLE backup_log (
    id          BIGSERIAL PRIMARY KEY,
    file_path   TEXT NOT NULL,
    size_bytes  BIGINT,
    row_counts  JSONB,
    succeeded   BOOLEAN NOT NULL,
    error_text  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    verify_note TEXT
);
CREATE INDEX ix_backup_created ON backup_log (created_at DESC);

-- ================================================================ views =====

-- O3: four-point reconciliation, allocation -> S1 -> S5 -> S6.
CREATE VIEW v_reconciliation AS
SELECT
    s.id   AS shipment_id,
    s.reference,
    sc.canonical AS size,
    COALESCE(a.qty, 0)  AS allocated,
    COALESCE(s1.n, 0)   AS stage1_verified,
    COALESCE(s5.n, 0)   AS stage5_verified,
    COALESCE(s6.n, 0)   AS stage6_packed
FROM shipment s
CROSS JOIN size_code sc
LEFT JOIN (
    SELECT shipment_id, size_code_id, SUM(qty) qty
    FROM shipment_allocation GROUP BY 1,2
) a  ON a.shipment_id = s.id AND a.size_code_id = sc.id
LEFT JOIN (
    SELECT c.shipment_id, r.parsed_size_id sid, COUNT(*) n
    FROM scan_event e
    JOIN checklist c ON c.id = e.checklist_id
    LEFT JOIN recognition_result r ON r.scan_event_id = e.id
    WHERE e.stage = 'S1' AND e.verdict IS DISTINCT FROM 'no_read'
    GROUP BY 1,2
) s1 ON s1.shipment_id = s.id AND s1.sid = sc.id
LEFT JOIN (
    SELECT c.shipment_id, e.hangtag_size_id sid, COUNT(*) n
    FROM scan_event e
    JOIN checklist c ON c.id = e.checklist_id
    WHERE e.stage = 'S5' AND e.verdict = 'match'
    GROUP BY 1,2
) s5 ON s5.shipment_id = s.id AND s5.sid = sc.id
LEFT JOIN (
    SELECT ct.shipment_id, ci.size_code_id sid, SUM(ci.qty) n
    FROM carton ct JOIN carton_item ci ON ci.carton_id = ct.id
    GROUP BY 1,2
) s6 ON s6.shipment_id = s.id AND s6.sid = sc.id
WHERE COALESCE(a.qty,0) + COALESCE(s1.n,0) + COALESCE(s5.n,0) + COALESCE(s6.n,0) > 0;

-- §5.8: bypass behaviour. A manual entry with no failed recognition behind it.
CREATE VIEW v_bypass AS
SELECT
    e.operator_id,
    u.display_name,
    COUNT(*) FILTER (WHERE x.recognition_result_id IS NULL)     AS bypass_entries,
    COUNT(*)                                                     AS fallback_entries,
    ROUND(
      COUNT(*) FILTER (WHERE x.recognition_result_id IS NULL)::numeric
      / NULLIF(COUNT(*), 0), 4)                                  AS bypass_rate
FROM exception_event x
JOIN scan_event e ON e.id = x.scan_event_id
JOIN app_user  u ON u.id = e.operator_id
GROUP BY 1, 2;

-- §5.7: end-to-end latency as the operator experiences it, not engine time.
CREATE VIEW v_latency AS
SELECT
    e.id,
    e.stage,
    r.duration_ms                                                    AS engine_ms,
    EXTRACT(MILLISECOND FROM (e.responded_at - e.captured_at))
      + 1000 * EXTRACT(SECOND FROM (e.responded_at - e.captured_at)) AS round_trip_ms,
    EXTRACT(MILLISECOND FROM (e.displayed_at - e.captured_at))
      + 1000 * EXTRACT(SECOND FROM (e.displayed_at - e.captured_at)) AS end_to_end_ms,
    e.sync_state,
    e.queued_at,
    e.synced_at
FROM scan_event e
LEFT JOIN recognition_result r ON r.scan_event_id = e.id;

COMMIT;
