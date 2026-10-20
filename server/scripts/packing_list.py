#!/usr/bin/env python3
"""
Packing list generation — O3.

Reproduces the buyer's own format, taken from a real completed packing list
supplied by the case company (HUEL LIMITED, PO 130022, 11 Oct 2026). The
format is set by the buyer's manual, so the artefact matches it rather than
inventing one.

Layout, column by column:

    STYLE NO | COLOR NAME | PO NO | SKU
    | XXS XS S M L XL XXL XXXL XXXXL          <- buyer notation, not canonical
    | TOTAL | Per Carton | TOTAL Pcs/CTN
    | CTN NO (from, to) | CTN QTY | TOTAL QTY
    | N.W IN KGS | G.W IN KGS | TTL.N.W IN KGS | TTL.G.W IN KGS

then a subtotal row per style, a grand total, the origin certification, the
MEASUREMENT block and the REX declaration.

⚠️ SIZE COLUMNS USE THE BUYER'S NOTATION.
   The database stores canonical sizes (2XL). The buyer's packing list says
   XXL. Writing canonical sizes into this document would produce a file the
   buyer rejects, so every size is translated back through
   buyer_size_code_map on the way out. The map runs in both directions:
   inward for comparison, outward for the document.

⚠️ TWO FIELDS ARE DECLARED, NOT CAPTURED — the field map for O3.
   Net and gross weight per carton are weighed by the operator and entered;
   the artefact does not capture them. They are reported as a field-map gap
   rather than silently left blank or invented.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

from openpyxl import Workbook                                  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, Side       # noqa: E402
from openpyxl.utils import get_column_letter                    # noqa: E402

THIN = Side(style="thin")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
BOLD = Font(bold=True, name="Calibri", size=9)
BODY = Font(name="Calibri", size=9)
CENTRE = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)

# Column order as the buyer prints it.
SIZE_COLUMNS = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL"]

# Fields the artefact cannot capture. Declared, never invented.
FIELD_MAP_GAPS = [
    ("N.W IN KGS", "weighed and entered by the operator at carton close"),
    ("G.W IN KGS", "weighed and entered by the operator at carton close"),
    ("SKU", "buyer SKU, taken from barcode_map where registered"),
    ("MEASUREMENT", "carton dimensions, from the buyer manual"),
]


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def outward_map(cur, buyer_id: int) -> dict[str, str]:
    """
    canonical -> buyer notation, for writing the document.

    Where a buyer has several notations for one size (we register both 'XXL'
    and '2XL'), prefer the one that appears in the buyer's own column list.
    """
    cur.execute(
        """SELECT s.canonical, m.buyer_notation
           FROM buyer_size_code_map m
           JOIN size_code s ON s.id = m.size_code_id
           WHERE m.buyer_id = %s""", (buyer_id,))
    out: dict[str, str] = {}
    for r in cur.fetchall():
        canonical, notation = r["canonical"], r["buyer_notation"]
        if notation in SIZE_COLUMNS:
            out[canonical] = notation          # buyer's own spelling wins
        else:
            out.setdefault(canonical, notation)
    return out


def collect(cur, shipment_id: int) -> dict:
    cur.execute(
        """SELECT s.reference, s.ship_date, s.declared_qty,
                  p.po_number, p.total_qty, b.id AS buyer_id, b.name AS buyer
           FROM shipment s
           JOIN purchase_order p ON p.id = s.po_id
           JOIN buyer b ON b.id = p.buyer_id
           WHERE s.id = %s""", (shipment_id,))
    head = cur.fetchone()
    if head is None:
        raise SystemExit(f"no shipment with id {shipment_id}")

    # One line per style + colour + size, with the carton range that holds it.
    cur.execute(
        """SELECT st.style_no, co.name AS colour, sc.canonical AS size,
                  sc.sort_order,
                  MIN(c.carton_no::int) AS ctn_from,
                  MAX(c.carton_no::int) AS ctn_to,
                  COUNT(DISTINCT c.id)  AS ctn_qty,
                  SUM(ci.qty)           AS total_qty,
                  MIN(ci.qty)           AS per_carton,
                  AVG(c.net_weight_kg)  AS nw,
                  AVG(c.gross_weight_kg) AS gw,
                  MIN(bm.sku)           AS sku
           FROM carton c
           JOIN carton_item ci ON ci.carton_id = c.id
           JOIN size_code sc   ON sc.id = ci.size_code_id
           JOIN shipment sh    ON sh.id = c.shipment_id
           JOIN purchase_order po ON po.id = sh.po_id
           JOIN style st       ON st.id = po.style_id
           JOIN colour co      ON co.id = (
                SELECT colour_id FROM shipment_allocation
                WHERE shipment_id = c.shipment_id LIMIT 1)
           LEFT JOIN barcode_map bm
                  ON bm.style_id = st.id AND bm.size_code_id = sc.id
           WHERE c.shipment_id = %s
           GROUP BY st.style_no, co.name, sc.canonical, sc.sort_order
           ORDER BY st.style_no, sc.sort_order""", (shipment_id,))
    lines = cur.fetchall()
    return {"head": head, "lines": lines}


def build(shipment_id: int) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Packing List"

    with _connect() as conn, conn.cursor() as cur:
        data = collect(cur, shipment_id)
        head = data["head"]
        out_map = outward_map(cur, head["buyer_id"])

        cur.execute("SELECT name FROM buyer WHERE id = %s", (head["buyer_id"],))
        consignee = cur.fetchone()["name"]

    # ---- title and consignee ---------------------------------------------
    ws.cell(row=1, column=1, value="Details Packing List").font = Font(bold=True, size=11)
    ws.cell(row=2, column=1, value="APPLICANT / CONSIGNEE :").font = BOLD
    ws.cell(row=3, column=1, value=consignee).font = BODY
    ws.cell(row=2, column=16, value=f"ORDER QTY : {head['total_qty']} PCS").font = BOLD
    ws.cell(row=3, column=16, value=f"OFFER QTY : {head['declared_qty']} PCS").font = BOLD
    ws.cell(row=5, column=5, value="SHIPPING MARKS   : AS PER INVOICE").font = BOLD
    ws.cell(row=6, column=1, value=f"SHIPMENT: {head['reference']}    "
                                   f"SHIP DATE: {head['ship_date']}").font = BODY

    # ---- header rows ------------------------------------------------------
    r1, r2 = 8, 9
    fixed_left = ["STYLE NO", "COLOR NAME", "PO NO", "SKU"]
    for i, h in enumerate(fixed_left, start=1):
        ws.cell(row=r1, column=i, value=h)
        ws.merge_cells(start_row=r1, start_column=i, end_row=r2, end_column=i)

    size_start = len(fixed_left) + 1
    for j, sz in enumerate(SIZE_COLUMNS):
        ws.cell(row=r2, column=size_start + j, value=sz)

    after = size_start + len(SIZE_COLUMNS)
    tail = ["TOTAL", "Per Carton", "TOTAL Pcs /CTN"]
    for k, h in enumerate(tail):
        ws.cell(row=r1, column=after + k, value=h)
        ws.merge_cells(start_row=r1, start_column=after + k,
                       end_row=r2, end_column=after + k)

    ctn_col = after + len(tail)
    ws.cell(row=r1, column=ctn_col, value="CTN NO")
    ws.merge_cells(start_row=r1, start_column=ctn_col, end_row=r1, end_column=ctn_col + 1)
    ws.cell(row=r2, column=ctn_col, value="from")
    ws.cell(row=r2, column=ctn_col + 1, value="to")

    rest = ["CTN QTY", "TOTAL QTY.", "N.W IN KGS", "G.W IN KGS",
            "TTL.N.W IN KGS", "TTL.G.W IN KGS"]
    rest_col = ctn_col + 2
    for k, h in enumerate(rest):
        ws.cell(row=r1, column=rest_col + k, value=h)
        ws.merge_cells(start_row=r1, start_column=rest_col + k,
                       end_row=r2, end_column=rest_col + k)

    last_col = rest_col + len(rest) - 1
    for rr in (r1, r2):
        for cc in range(1, last_col + 1):
            c = ws.cell(row=rr, column=cc)
            c.font, c.alignment, c.border = BOLD, CENTRE, BOX

    # ---- body -------------------------------------------------------------
    row = r2 + 1
    style_totals: dict[str, dict] = {}
    grand = {"ctn": 0, "qty": 0, "nw": 0.0, "gw": 0.0}

    current_style = None
    for ln in data["lines"]:
        if current_style is not None and ln["style_no"] != current_style:
            row = _subtotal(ws, row, current_style, style_totals[current_style],
                            after, ctn_col, rest_col, last_col)
            current_style = None
        if current_style is None:
            current_style = ln["style_no"]
            style_totals.setdefault(current_style,
                                    {"ctn": 0, "qty": 0, "nw": 0.0, "gw": 0.0})

        ws.cell(row=row, column=1, value=ln["style_no"])
        ws.cell(row=row, column=2, value=ln["colour"])
        ws.cell(row=row, column=3, value=head["po_number"])
        ws.cell(row=row, column=4, value=ln["sku"] or "")

        # ⭐ translate canonical -> buyer notation for the column
        notation = out_map.get(ln["size"], ln["size"])
        if notation in SIZE_COLUMNS:
            ws.cell(row=row, column=size_start + SIZE_COLUMNS.index(notation),
                    value=ln["per_carton"])

        ws.cell(row=row, column=after,     value=ln["per_carton"])
        ws.cell(row=row, column=after + 1, value="")      # pack config, buyer text
        ws.cell(row=row, column=after + 2, value=ln["per_carton"])
        ws.cell(row=row, column=ctn_col,     value=ln["ctn_from"])
        ws.cell(row=row, column=ctn_col + 1, value=ln["ctn_to"])
        ws.cell(row=row, column=rest_col,     value=ln["ctn_qty"])
        ws.cell(row=row, column=rest_col + 1, value=ln["total_qty"])

        nw = float(ln["nw"] or 0)
        gw = float(ln["gw"] or 0)
        ttl_nw = round(nw * ln["ctn_qty"], 2)
        ttl_gw = round(gw * ln["ctn_qty"], 2)
        ws.cell(row=row, column=rest_col + 2, value=nw or None)
        ws.cell(row=row, column=rest_col + 3, value=gw or None)
        ws.cell(row=row, column=rest_col + 4, value=ttl_nw or None)
        ws.cell(row=row, column=rest_col + 5, value=ttl_gw or None)

        t = style_totals[current_style]
        t["ctn"] += ln["ctn_qty"]; t["qty"] += int(ln["total_qty"] or 0)
        t["nw"] += ttl_nw;         t["gw"] += ttl_gw
        grand["ctn"] += ln["ctn_qty"]; grand["qty"] += int(ln["total_qty"] or 0)
        grand["nw"] += ttl_nw;         grand["gw"] += ttl_gw

        for cc in range(1, last_col + 1):
            c = ws.cell(row=row, column=cc)
            c.font, c.border = BODY, BOX
            c.alignment = LEFT if cc <= 4 else CENTRE
        row += 1

    if current_style is not None:
        row = _subtotal(ws, row, current_style, style_totals[current_style],
                        after, ctn_col, rest_col, last_col)

    # grand total
    ws.cell(row=row, column=2, value="TOTAL").font = BOLD
    ws.cell(row=row, column=rest_col,     value=grand["ctn"]).font = BOLD
    ws.cell(row=row, column=rest_col + 1, value=grand["qty"]).font = BOLD
    ws.cell(row=row, column=rest_col + 4, value=round(grand["nw"], 2)).font = BOLD
    ws.cell(row=row, column=rest_col + 5, value=round(grand["gw"], 2)).font = BOLD
    for cc in range(1, last_col + 1):
        ws.cell(row=row, column=cc).border = BOX
    row += 2

    # ---- origin certification, measurement block, REX ---------------------
    ws.cell(row=row, column=1,
            value="CERTIFYING THAT THE GOODS ARE OF SRI LANKA ORIGIN.").font = BODY
    row += 2

    ws.cell(row=row, column=1, value="MEASUREMENT").font = BOLD
    ws.cell(row=row, column=3, value="⬜ carton dimensions — field-map gap, "
                                     "taken from the buyer manual").font = BODY
    row += 2

    ws.cell(row=row, column=1, value="REX DECLARATION").font = BOLD
    row += 1
    ws.cell(row=row, column=1, value="⬜ exporter REX number — field-map gap").font = BODY
    row += 2

    # ---- the field map, printed with the document -------------------------
    ws.cell(row=row, column=1, value="FIELD MAP — fields the artefact does not capture").font = BOLD
    row += 1
    for name, why in FIELD_MAP_GAPS:
        ws.cell(row=row, column=1, value=name).font = BODY
        ws.cell(row=row, column=3, value=why).font = BODY
        row += 1

    # widths
    for i in range(1, last_col + 1):
        ws.column_dimensions[get_column_letter(i)].width = 14 if i <= 4 else 9

    out_dir = Path(settings.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in head["reference"])
    path = out_dir / f"PACKING_LIST_{safe}_{datetime.now():%Y-%m-%d}.xlsx"
    tmp = path.with_suffix(".xlsx.part")
    wb.save(tmp)
    tmp.replace(path)
    return path


def _subtotal(ws, row, style_no, t, after, ctn_col, rest_col, last_col):
    ws.cell(row=row, column=after - 1, value="TOTAL").font = BOLD
    ws.cell(row=row, column=rest_col,     value=t["ctn"]).font = BOLD
    ws.cell(row=row, column=rest_col + 1, value=t["qty"]).font = BOLD
    ws.cell(row=row, column=rest_col + 4, value=round(t["nw"], 2)).font = BOLD
    ws.cell(row=row, column=rest_col + 5, value=round(t["gw"], 2)).font = BOLD
    for cc in range(1, last_col + 1):
        ws.cell(row=row, column=cc).border = BOX
    return row + 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: packing_list.py <shipment_id>", file=sys.stderr)
        return 2
    print(f"OK  {build(int(sys.argv[1]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
