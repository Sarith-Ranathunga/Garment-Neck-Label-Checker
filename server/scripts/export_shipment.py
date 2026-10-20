#!/usr/bin/env python3
"""
Per-shipment Excel export.

ONE WRITER ONLY — the host. That is the whole design.

An .xlsx is a single zip archive with no partial write and no merge. If the
host and several phones each uploaded their own version, the last upload
would silently replace every other. Separate sheets do not help, because a
sheet lives inside the one file. Making the host the only writer removes the
problem rather than managing it.

Eight sheets, identical for every shipment so one parser reads them all:

  1  Shipment        header, allocation, status
  2  Scans           one row per scan, every stage
  3  Mismatches      O2 evidence, and what the supervisor acts on
  4  Exceptions      fallbacks — with the bypass column (DP3)
  5  Cartons         carton contents
  6  PackingList     the generated list
  7  Reconciliation  allocation -> S1 -> S5 -> S6
  8  Operators       per-operator summary, feeding §5.8

Operators get a COLUMN in sheet 2, not a sheet each: the structure then stays
identical whatever the shipment, Excel's filter gives per-operator views in
two clicks, and sheet 8 already carries the summary.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

from openpyxl import Workbook                     # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter      # noqa: E402

HDR_FILL = PatternFill("solid", fgColor="D9D9D9")
HDR_FONT = Font(bold=True)


def _connect():
    import psycopg
    return psycopg.connect(settings.database_url)


SHEETS: dict[str, tuple[list[str], str]] = {
    "Scans": (
        ["Scan ID", "Captured", "Stage", "Operator", "Device", "Checklist",
         "Carton", "Hangtag size", "Label size read", "Verdict", "Entry method",
         "Confidence", "Rotation", "Engine ms", "End-to-end ms",
         "Sync state", "Queued at", "Synced at"],
        """
        SELECT e.id, e.captured_at, e.stage, u.display_name, e.device_id,
               e.checklist_id, c.carton_no,
               hs.canonical, ps.canonical, e.verdict, e.entry_method,
               r.confidence, r.rotation_used, r.duration_ms,
               ROUND(EXTRACT(EPOCH FROM (e.displayed_at - e.captured_at))*1000),
               e.sync_state, e.queued_at, e.synced_at
        FROM scan_event e
        JOIN app_user u ON u.id = e.operator_id
        LEFT JOIN checklist cl ON cl.id = e.checklist_id
        LEFT JOIN carton c ON c.id = e.carton_id
        LEFT JOIN size_code hs ON hs.id = e.hangtag_size_id
        LEFT JOIN recognition_result r ON r.scan_event_id = e.id
        LEFT JOIN size_code ps ON ps.id = r.parsed_size_id
        WHERE cl.shipment_id = %(sid)s OR c.shipment_id = %(sid)s
        ORDER BY e.captured_at
        """,
    ),
    "Mismatches": (
        ["Mismatch ID", "Detected", "Stage", "Operator", "Neck label size",
         "Hangtag size", "Carton", "Resolved", "Note"],
        """
        SELECT m.id, m.detected_at, e.stage, u.display_name,
               ns.canonical, hs.canonical, c.carton_no,
               m.resolved_at, m.resolution_note
        FROM mismatch m
        JOIN scan_event e ON e.id = m.scan_event_id
        JOIN app_user u ON u.id = e.operator_id
        LEFT JOIN checklist cl ON cl.id = e.checklist_id
        LEFT JOIN size_code ns ON ns.id = m.neck_size_id
        LEFT JOIN size_code hs ON hs.id = m.hangtag_size_id
        LEFT JOIN carton c ON c.id = m.carton_id
        WHERE cl.shipment_id = %(sid)s OR c.shipment_id = %(sid)s
        ORDER BY m.detected_at
        """,
    ),
    "Exceptions": (
        # The bypass column is the reason this sheet exists.
        ["Exception ID", "Created", "Stage", "Operator", "Entered size",
         "Preceded by a no read?", "BYPASS (DP3)", "Reason"],
        """
        SELECT x.id, x.created_at, e.stage, u.display_name, s.canonical,
               CASE WHEN x.recognition_result_id IS NULL THEN 'no' ELSE 'yes' END,
               CASE WHEN x.recognition_result_id IS NULL THEN 'YES' ELSE '' END,
               x.reason
        FROM exception_event x
        JOIN scan_event e ON e.id = x.scan_event_id
        JOIN app_user u ON u.id = e.operator_id
        LEFT JOIN checklist cl ON cl.id = e.checklist_id
        LEFT JOIN carton c ON c.id = e.carton_id
        LEFT JOIN size_code s ON s.id = x.entered_size_id
        WHERE cl.shipment_id = %(sid)s OR c.shipment_id = %(sid)s
        ORDER BY x.created_at
        """,
    ),
    "Cartons": (
        ["Carton", "Size", "Qty", "Opened", "Closed"],
        """
        SELECT c.carton_no, s.canonical, ci.qty, c.opened_at, c.closed_at
        FROM carton c
        JOIN carton_item ci ON ci.carton_id = c.id
        JOIN size_code s ON s.id = ci.size_code_id
        WHERE c.shipment_id = %(sid)s
        ORDER BY c.carton_no, s.sort_order
        """,
    ),
    "PackingList": (
        ["Generated", "Carton", "Size", "Qty", "Net kg", "Gross kg"],
        """
        SELECT p.generated_at, c.carton_no, s.canonical, l.qty,
               l.net_weight_kg, l.gross_weight_kg
        FROM packing_list p
        JOIN packing_list_line l ON l.packing_list_id = p.id
        JOIN carton c ON c.id = l.carton_id
        JOIN size_code s ON s.id = l.size_code_id
        WHERE p.shipment_id = %(sid)s
        ORDER BY p.generated_at DESC, c.carton_no, s.sort_order
        """,
    ),
    "Reconciliation": (
        ["Shipment", "Size", "Allocated", "Stage 1 verified",
         "Stage 5 verified", "Stage 6 packed"],
        """
        SELECT reference, size, allocated, stage1_verified,
               stage5_verified, stage6_packed
        FROM v_reconciliation WHERE shipment_id = %(sid)s
        """,
    ),
    "Operators": (
        ["Operator", "Scans", "Mismatches found", "Fallback entries",
         "Bypass entries", "Bypass rate", "Assistance events"],
        """
        SELECT u.display_name,
               count(DISTINCT e.id),
               count(DISTINCT m.id),
               count(DISTINCT x.id),
               count(DISTINCT x.id) FILTER (WHERE x.recognition_result_id IS NULL),
               ROUND(
                 count(DISTINCT x.id) FILTER (WHERE x.recognition_result_id IS NULL)::numeric
                 / NULLIF(count(DISTINCT x.id), 0), 4),
               (SELECT count(*) FROM assistance_event a
                 WHERE a.operator_id = u.id AND a.shipment_id = %(sid)s)
        FROM app_user u
        JOIN scan_event e ON e.operator_id = u.id
        LEFT JOIN checklist cl ON cl.id = e.checklist_id
        LEFT JOIN carton c ON c.id = e.carton_id
        LEFT JOIN mismatch m ON m.scan_event_id = e.id
        LEFT JOIN exception_event x ON x.scan_event_id = e.id
        WHERE cl.shipment_id = %(sid)s OR c.shipment_id = %(sid)s
        GROUP BY u.id, u.display_name
        ORDER BY u.display_name
        """,
    ),
}


def _style(ws, ncols: int) -> None:
    for i in range(1, ncols + 1):
        cell = ws.cell(row=1, column=i)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = max(
            12, min(28, len(str(cell.value or "")) + 4))
    ws.freeze_panes = "A2"


def build(shipment_id: int) -> tuple[Path, dict[str, int]]:
    wb = Workbook()
    counts: dict[str, int] = {}

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT s.reference, s.ship_date, s.declared_qty, s.status,
                      p.po_number, b.name, st.style_no
               FROM shipment s
               JOIN purchase_order p ON p.id = s.po_id
               JOIN buyer b ON b.id = p.buyer_id
               JOIN style st ON st.id = p.style_id
               WHERE s.id = %s""", (shipment_id,))
        head = cur.fetchone()
        if head is None:
            raise SystemExit(f"no shipment with id {shipment_id}")
        reference, ship_date, declared, status, po, buyer, style_no = head

        # ---- sheet 1: Shipment -------------------------------------------
        ws = wb.active
        ws.title = "Shipment"
        for r, (k, v) in enumerate([
            ("Shipment reference", reference), ("Purchase order", po),
            ("Buyer", buyer), ("Style", style_no), ("Ship date", ship_date),
            ("Declared quantity", declared), ("Status", status),
            ("Exported", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ], start=1):
            ws.cell(row=r, column=1, value=k).font = HDR_FONT
            ws.cell(row=r, column=2, value=v)
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 30

        ws.cell(row=10, column=1, value="SIZE ALLOCATION").font = HDR_FONT
        for i, h in enumerate(["Size", "Colour", "Quantity"], start=1):
            c = ws.cell(row=11, column=i, value=h)
            c.fill, c.font = HDR_FILL, HDR_FONT
        cur.execute(
            """SELECT sc.canonical, co.name, a.qty
               FROM shipment_allocation a
               JOIN size_code sc ON sc.id = a.size_code_id
               JOIN colour co ON co.id = a.colour_id
               WHERE a.shipment_id = %s ORDER BY sc.sort_order""",
            (shipment_id,))
        row = 12
        total = 0
        for size, colour, qty in cur.fetchall():
            ws.cell(row=row, column=1, value=size)
            ws.cell(row=row, column=2, value=colour)
            ws.cell(row=row, column=3, value=qty)
            total += qty
            row += 1
        ws.cell(row=row, column=1, value="Total").font = HDR_FONT
        ws.cell(row=row, column=3, value=total).font = HDR_FONT
        ws.cell(row=row + 1, column=1,
                value=("Allocation agrees with the declared total"
                       if total == declared
                       else f"MISMATCH: declared {declared}, allocated {total}"))
        counts["Shipment"] = row - 11

        # ---- sheets 2-8 ---------------------------------------------------
        for name, (headers, sql) in SHEETS.items():
            sheet = wb.create_sheet(name)
            sheet.append(headers)
            _style(sheet, len(headers))
            cur.execute(sql, {"sid": shipment_id})
            n = 0
            for rec in cur.fetchall():
                sheet.append([
                    v.replace(tzinfo=None) if isinstance(v, datetime) else v
                    for v in rec
                ])
                n += 1
            counts[name] = n

    out_dir = Path(settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in reference)
    path = out_dir / f"SHIPMENT_{safe}_{datetime.now():%Y-%m-%d}.xlsx"

    tmp = path.with_suffix(".xlsx.part")      # same atomic-rename rule
    wb.save(tmp)
    tmp.replace(path)
    return path, counts


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: export_shipment.py <shipment_id>", file=sys.stderr)
        return 2
    sid = int(sys.argv[1])
    try:
        path, counts = build(sid)
    except Exception as exc:                        # noqa: BLE001
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO export_log (shipment_id, file_path, succeeded, error_text)"
                " VALUES (%s, %s, FALSE, %s)", (sid, "", str(exc)[:2000]))
            conn.commit()
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO export_log (shipment_id, file_path, row_counts, succeeded)"
            " VALUES (%s, %s, %s, TRUE)", (sid, str(path), json.dumps(counts)))
        conn.commit()
    print(f"OK  {path}  {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
