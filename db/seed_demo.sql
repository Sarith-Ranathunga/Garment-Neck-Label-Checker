-- Demo data built from the real HUEL LIMITED packing list and real hangtags
-- supplied by the case company, 11 October 2026.
--
-- Buyer notation here is the REAL notation from that packing list:
--   XXS  XS  S  M  L  XL  XXL  XXXL  XXXXL
-- which maps to canonical 2XL / 3XL / 4XL. No numeric sizes are in use.

BEGIN;

INSERT INTO role (code, description) VALUES
 ('manager','Enters orders and shipments'),
 ('supervisor','Opens checklists, monitors packing flow'),
 ('worker','Scans at stages 1, 5 and 6'),
 ('admin','System administration');

INSERT INTO app_user (username, display_name, role_id, password_hash) VALUES
 ('mgr','A. Manager',1,'x'),
 ('sup','B. Supervisor',2,'x'),
 ('w1','C. Fernando',3,'x'),
 ('w2','D. Perera',3,'x');

INSERT INTO buyer (code, name) VALUES ('HUEL','HUEL LIMITED');

INSERT INTO style (buyer_id, style_no, description) VALUES
 (1,'HUEL (women''s)','Women''s black T-shirt'),
 (1,'HUEL (men''s)','Men''s black T-shirt');

INSERT INTO colour (name) VALUES ('BLACK'), ('WHITE');

-- Canonical sizes. Sort order follows the packing list column order.
INSERT INTO size_code (canonical, sort_order) VALUES
 ('XXS',1),('XS',2),('S',3),('M',4),('L',5),('XL',6),
 ('2XL',7),('3XL',8),('4XL',9);

-- ⭐ The buyer writes XXL / XXXL / XXXXL where canonical is 2XL / 3XL / 4XL.
--   Without these three rows every such garment is a false positive.
INSERT INTO buyer_size_code_map (buyer_id, size_code_id, buyer_notation) VALUES
 (1,1,'XXS'),(1,2,'XS'),(1,3,'S'),(1,4,'M'),(1,5,'L'),(1,6,'XL'),
 (1,7,'XXL'),(1,8,'XXXL'),(1,9,'XXXXL');

-- Also accept the canonical spellings, since the neck label may use either.
INSERT INTO buyer_size_code_map (buyer_id, size_code_id, buyer_notation) VALUES
 (1,7,'2XL'),(1,8,'3XL'),(1,9,'4XL');

INSERT INTO purchase_order (po_number, buyer_id, style_id, total_qty, created_by)
 VALUES ('130022', 1, 1, 56000, 1);

INSERT INTO shipment (po_id, reference, ship_date, declared_qty, status, created_by)
 VALUES (1,'SHP-130022-01', DATE '2026-10-28', 56000, 'active', 1);

-- Allocation taken straight from the real packing list.
-- Women's 14,400 + Men's 41,600 = 56,000.
INSERT INTO shipment_allocation (shipment_id, size_code_id, colour_id, qty) VALUES
 (1,3,1,2750+2600),      -- S
 (1,4,1,4650+9950),      -- M
 (1,5,1,3450+13400),     -- L
 (1,6,1,1950+9000),      -- XL
 (1,7,1,1600+6650);      -- 2XL (buyer writes XXL)

-- Barcodes. Both real forms are represented.
INSERT INTO barcode_map (barcode, symbology, buyer_id, style_id, colour_id, size_code_id, sku) VALUES
 ('HUEL-BLACK-S',   'CODE_128',1,1,1,3,'HU-W-BLK-S'),
 ('HUEL-BLACK-M',   'CODE_128',1,1,1,4,'HU-W-BLK-M'),
 ('HUEL-BLACK-L',   'CODE_128',1,1,1,5,'HU-W-BLK-L'),
 ('HUEL-BLACK-XL',  'CODE_128',1,1,1,6,'HU-W-BLK-XL'),
 ('HUEL-BLACK-XXL', 'CODE_128',1,1,1,7,'HU-W-BLK-XXL'),
 -- the opaque UPC-A form: nothing can be read from the string itself
 ('840395619520','UPC_A',1,2,1,4,'HU-M-BLK-M'),
 ('840395619521','UPC_A',1,2,1,5,'HU-M-BLK-L'),
 ('840395619522','UPC_A',1,2,1,7,'HU-M-BLK-XXL');

INSERT INTO checklist (shipment_id, style_id, colour_id, stage, opened_by)
 VALUES (1,1,1,'S1',2),(1,1,1,'S5',2);

INSERT INTO carton (shipment_id, carton_no, opened_by, closed_at, net_weight_kg, gross_weight_kg)
 VALUES (1,'1',3, now(), 9.800, 11.000), (1,'2',4, now(), 10.000, 11.200);
INSERT INTO carton_item (carton_id, size_code_id, qty) VALUES
 (1,3,50),(2,4,50);

INSERT INTO scan_event
 (local_id, device_id, operator_id, checklist_id, carton_id, stage,
  hangtag_barcode, hangtag_size_raw, hangtag_size_id,
  captured_at, responded_at, displayed_at, sync_state, synced_at,
  verdict, entry_method)
VALUES
 -- clean match, delimited barcode
 (gen_random_uuid(),'PH-01',3,2,1,'S5','HUEL-BLACK-L','L',5,
  now()-interval '40 min', now()-interval '40 min' + interval '820 ms',
  now()-interval '40 min' + interval '960 ms','acknowledged', now()-interval '40 min',
  'match','recognised'),
 -- ⭐ notation case: hangtag says XXL, canonical 2XL — MUST be a match
 (gen_random_uuid(),'PH-01',3,2,2,'S5','HUEL-BLACK-XXL','XXL',7,
  now()-interval '35 min', now()-interval '35 min' + interval '910 ms',
  now()-interval '35 min' + interval '1050 ms','acknowledged', now()-interval '35 min',
  'match','recognised'),
 -- genuine adjacent-size error, opaque barcode resolved by lookup
 (gen_random_uuid(),'PH-02',4,2,1,'S5','840395619521','L',5,
  now()-interval '30 min', now()-interval '30 min' + interval '780 ms',
  now()-interval '30 min' + interval '930 ms','acknowledged', now()-interval '30 min',
  'mismatch','recognised'),
 -- no read, went to fallback
 (gen_random_uuid(),'PH-02',4,2,2,'S5','HUEL-BLACK-M','M',4,
  now()-interval '25 min', now()-interval '25 min' + interval '3100 ms',
  now()-interval '25 min' + interval '3260 ms','acknowledged', now()-interval '25 min',
  'no_read','manual');

INSERT INTO recognition_result
 (scan_event_id, engine, engine_version, rotation_used, rotations_tried,
  raw_text, parsed_size_id, confidence, outcome, duration_ms)
VALUES
 (1,'easyocr','1.7.2',0,1,'HUSTLE SIZE - L RELAX FIT MADE IN SRI LANKA',5,0.9310,'correct',742),
 (2,'easyocr','1.7.2',90,2,'HUSTLE SIZE - XXL OVERSIZE MADE IN SRI LANKA',7,0.8870,'correct',868),
 (3,'easyocr','1.7.2',0,1,'HUSTLE SIZE - M RELAX FIT MADE IN SRI LANKA',4,0.9020,'correct',715),
 (4,'easyocr','1.7.2',NULL,4,'|| ~ .',NULL,0.1900,'no_read',3015);

INSERT INTO mismatch (scan_event_id, neck_size_id, hangtag_size_id, carton_id)
 VALUES (3, 4, 5, 1);

-- One legitimate fallback, one BYPASS (NULL recognition_result_id).
INSERT INTO exception_event (scan_event_id, recognition_result_id, entered_size_id, reason)
VALUES
 (4, 4, 4, 'Label faded, four rotations returned nothing'),
 (4, NULL, 4, 'Operator entered manually without attempting a read');

INSERT INTO assistance_event (operator_id, shipment_id, stage, cause)
 VALUES (4, 1, 'S5', 'Unsure how to clear a mismatch alert');

COMMIT;
