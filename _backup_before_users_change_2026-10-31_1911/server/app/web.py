"""
The supervisor web interface — increment 4, O5.

⚠️ Monitoring and setup only. **Nothing here scans.** The Android client is
the only scanning device, and that is a design decision rather than an
omission: the phone is at the packing table where the garment is, and a
verification the supervisor performs at a desk is not the control the research
is testing.

O5 is DEMONSTRATED, not evaluated (§3.09). It produces no findings of its own
and admits no accuracy measure — it displays data the verification components
produced. So the standard this interface is held to is not accuracy but
legibility: a supervisor glancing at it should see where a count went wrong
without reading a manual.

That is why the reconciliation table shows the three *deltas* rather than only
the four counts. The research claim in O3 is that a divergence is
**attributable to the stage where it occurred**, and four raw numbers leave
the reader to do that subtraction. The deltas are the claim, displayed.

Three roles use it:

  * **manager**   — enters the purchase order, the shipment and the size
    allocation. The allocation total is validated against the declared total
    and refused if it disagrees, because a typo there poisons every count
    downstream.
  * **supervisor** — opens and closes checklists, watches the packing flow,
    resolves mismatches, generates the packing list.
  * **nobody**    — there is no scanning here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import cursor

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates"))

router = APIRouter(prefix="/ui", tags=["supervisor interface"])


# ----------------------------------------------------------------- helpers --

def _fmt_int(value) -> str:
    """Thousands separators. 56000 is unreadable; 56,000 is not."""
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_delta(value) -> str:
    """A signed delta, because the sign is the whole information."""
    if value is None:
        return "—"
    v = int(value)
    if v == 0:
        return "0"
    return f"{v:+,}"


def _fmt_pct(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def _fmt_ms(value) -> str:
    if value is None:
        return "—"
    ms = float(value)
    return f"{ms:,.0f} ms" if ms < 1000 else f"{ms / 1000:,.2f} s"


TEMPLATES.env.filters["n"] = _fmt_int
TEMPLATES.env.filters["delta"] = _fmt_delta
TEMPLATES.env.filters["pct"] = _fmt_pct
TEMPLATES.env.filters["ms"] = _fmt_ms


def _page(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("now", datetime.now().astimezone())
    return TEMPLATES.TemplateResponse(request, template, ctx)


# ================================================================ shipments ==

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def shipments(request: Request) -> HTMLResponse:
    """Every shipment, newest first, with enough to choose between them."""
    with cursor() as cur:
        cur.execute(
            """SELECT s.id, s.reference, s.ship_date, s.declared_qty,
                      s.status::text AS status, s.closed_at,
                      p.po_number, b.code AS buyer_code, b.name AS buyer_name,
                      COALESCE(a.allocated, 0) AS allocated,
                      COALESCE(sc.scans, 0)    AS scans,
                      COALESCE(ct.cartons, 0)  AS cartons,
                      COALESCE(mm.mismatches, 0) AS mismatches
                 FROM shipment s
                 JOIN purchase_order p ON p.id = s.po_id
                 JOIN buyer b          ON b.id = p.buyer_id
                 LEFT JOIN (SELECT shipment_id, SUM(qty) AS allocated
                              FROM shipment_allocation GROUP BY 1) a
                        ON a.shipment_id = s.id
                 LEFT JOIN (SELECT c.shipment_id, COUNT(*) AS scans
                              FROM scan_event e
                              JOIN checklist c ON c.id = e.checklist_id
                             GROUP BY 1) sc
                        ON sc.shipment_id = s.id
                 LEFT JOIN (SELECT shipment_id, COUNT(*) AS cartons
                              FROM carton GROUP BY 1) ct
                        ON ct.shipment_id = s.id
                 LEFT JOIN (SELECT c.shipment_id, COUNT(*) AS mismatches
                              FROM mismatch m
                              JOIN scan_event e ON e.id = m.scan_event_id
                              JOIN checklist c  ON c.id = e.checklist_id
                             GROUP BY 1) mm
                        ON mm.shipment_id = s.id
                ORDER BY s.status, s.ship_date DESC, s.id DESC""")
        rows = cur.fetchall()
    return _page(request, "shipments.html", shipments=rows)


# ================================================================ dashboard ==

@router.get("/shipment/{shipment_id}", response_class=HTMLResponse)
def dashboard(request: Request, shipment_id: int) -> HTMLResponse:
    """
    The packing-flow monitor. This is what O5 is.

    Everything on it is derived from scans. Nothing is typed in, which is why
    the carton contents and the verified contents are the same figure — the
    identity O3's reconciliation rests on.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT s.id, s.reference, s.ship_date, s.declared_qty,
                      s.status::text AS status, p.po_number,
                      b.code AS buyer_code, b.name AS buyer_name
                 FROM shipment s
                 JOIN purchase_order p ON p.id = s.po_id
                 JOIN buyer b          ON b.id = p.buyer_id
                WHERE s.id = %s""", (shipment_id,))
        ship = cur.fetchone()
        if ship is None:
            raise HTTPException(404, f"no shipment {shipment_id}")

        # --- the four-point reconciliation, with the deltas ---------------
        cur.execute(
            """SELECT r.*, sc.sort_order
                 FROM v_reconciliation r
                 JOIN size_code sc ON sc.canonical = r.size
                WHERE r.shipment_id = %s
                ORDER BY sc.sort_order""", (shipment_id,))
        recon = [dict(r) for r in cur.fetchall()]

        for row in recon:
            row["d1"] = row["stage1_verified"] - row["allocated"]
            row["d5"] = row["stage5_verified"] - row["stage1_verified"]
            row["d6"] = row["stage6_packed"] - row["stage5_verified"]

        totals = {
            "allocated": sum(r["allocated"] for r in recon),
            "stage1_verified": sum(r["stage1_verified"] for r in recon),
            "stage5_verified": sum(r["stage5_verified"] for r in recon),
            "stage6_packed": sum(r["stage6_packed"] for r in recon),
        }
        totals["d1"] = totals["stage1_verified"] - totals["allocated"]
        totals["d5"] = totals["stage5_verified"] - totals["stage1_verified"]
        totals["d6"] = totals["stage6_packed"] - totals["stage5_verified"]

        # --- verdicts by stage --------------------------------------------
        #
        # Kept apart by stage because the stages do different things. S1
        # ESTABLISHES a size and S5 VERIFIES one; a match at S1 is not
        # evidence about O2, because no comparison happened there.
        cur.execute(
            """SELECT e.stage::text AS stage,
                      COALESCE(e.verdict::text, 'pending') AS verdict,
                      COUNT(*) AS n
                 FROM scan_event e
                 LEFT JOIN checklist c ON c.id = e.checklist_id
                 LEFT JOIN carton ct   ON ct.id = e.carton_id
                WHERE c.shipment_id = %s OR ct.shipment_id = %s
                GROUP BY 1, 2 ORDER BY 1, 2""", (shipment_id, shipment_id))
        verdict_rows = cur.fetchall()

        stages: dict[str, dict] = {}
        for r in verdict_rows:
            st = stages.setdefault(
                r["stage"], {"stage": r["stage"], "total": 0, "match": 0,
                             "mismatch": 0, "no_read": 0,
                             "out_of_context": 0, "pending": 0})
            st[r["verdict"]] = st.get(r["verdict"], 0) + r["n"]
            st["total"] += r["n"]

        # --- open mismatches ----------------------------------------------
        cur.execute(
            """SELECT m.id, m.detected_at, m.resolved_at, m.resolution_note,
                      ns.canonical AS neck_size, hs.canonical AS hangtag_size,
                      ct.carton_no, ct.closed_at AS carton_closed_at,
                      u.display_name AS operator, e.stage::text AS stage,
                      e.hangtag_barcode
                 FROM mismatch m
                 JOIN scan_event e      ON e.id = m.scan_event_id
                 JOIN app_user u         ON u.id = e.operator_id
                 LEFT JOIN size_code ns  ON ns.id = m.neck_size_id
                 LEFT JOIN size_code hs  ON hs.id = m.hangtag_size_id
                 LEFT JOIN carton ct     ON ct.id = m.carton_id
                 LEFT JOIN checklist c   ON c.id = e.checklist_id
                WHERE c.shipment_id = %s OR ct.shipment_id = %s
                ORDER BY m.resolved_at NULLS FIRST, m.detected_at DESC
                LIMIT 100""", (shipment_id, shipment_id))
        mismatches = cur.fetchall()

        # --- cartons -------------------------------------------------------
        cur.execute(
            """SELECT ct.id, ct.carton_no, ct.opened_at, ct.closed_at,
                      ct.net_weight_kg, ct.gross_weight_kg,
                      u.display_name AS opened_by,
                      COALESCE(ci.packed, 0) AS packed
                 FROM carton ct
                 JOIN app_user u ON u.id = ct.opened_by
                 LEFT JOIN (SELECT carton_id, SUM(qty) AS packed
                              FROM carton_item GROUP BY 1) ci
                        ON ci.carton_id = ct.id
                WHERE ct.shipment_id = %s
                ORDER BY ct.closed_at NULLS FIRST, ct.opened_at DESC
                LIMIT 60""", (shipment_id,))
        cartons = cur.fetchall()

        # --- verdicts that arrived after the scan was queued ---------------
        #
        # §5.5. The error was still DETECTED, but possibly after the carton
        # closed — and an escape prevented is not the same outcome as an
        # escape found afterwards, because the second one means someone has
        # to find and reopen a carton. Reported separately for that reason.
        cur.execute(
            """SELECT COUNT(*) AS queued,
                      COUNT(*) FILTER (WHERE e.verdict = 'mismatch') AS late_mismatch,
                      ROUND(AVG(EXTRACT(EPOCH FROM (e.synced_at - e.queued_at))
                                )::numeric, 1) AS mean_delay_s
                 FROM scan_event e
                 LEFT JOIN checklist c ON c.id = e.checklist_id
                 LEFT JOIN carton ct   ON ct.id = e.carton_id
                WHERE (c.shipment_id = %s OR ct.shipment_id = %s)
                  AND e.synced_at IS NOT NULL AND e.queued_at IS NOT NULL
                  AND e.synced_at > e.queued_at + INTERVAL '2 seconds'""",
            (shipment_id, shipment_id))
        late = cur.fetchone()

        cur.execute(
            """SELECT id, stage, style_id, colour_id, opened_at, closed_at
                 FROM checklist WHERE shipment_id = %s
                ORDER BY closed_at NULLS FIRST, opened_at DESC""",
            (shipment_id,))
        checklist_count = len(cur.fetchall())

    return _page(
        request, "dashboard.html",
        ship=ship, recon=recon, totals=totals,
        stages=[stages[k] for k in sorted(stages)],
        mismatches=mismatches, cartons=cartons, late=late,
        checklist_count=checklist_count,
        unresolved=sum(1 for m in mismatches if m["resolved_at"] is None),
    )


# =============================================================== checklists ==

@router.get("/shipment/{shipment_id}/checklists", response_class=HTMLResponse)
def checklists(request: Request, shipment_id: int) -> HTMLResponse:
    """
    The supervisor's own job.

    Checklists are opened here and not by the scanning worker. The supervisor
    inspects incoming sets, is not under per-garment throughput pressure, and
    holds accountability for the declaration — so the declaration error is
    reduced by who is allowed to make it, not by technology.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT s.id, s.reference, s.status::text AS status, p.buyer_id
                 FROM shipment s JOIN purchase_order p ON p.id = s.po_id
                WHERE s.id = %s""", (shipment_id,))
        ship = cur.fetchone()
        if ship is None:
            raise HTTPException(404, f"no shipment {shipment_id}")

        cur.execute(
            """SELECT c.id, c.stage::text AS stage, c.opened_at, c.closed_at,
                      st.style_no, co.name AS colour,
                      u.display_name AS opened_by,
                      (SELECT COUNT(*) FROM scan_event e
                        WHERE e.checklist_id = c.id) AS scans
                 FROM checklist c
                 JOIN style st  ON st.id = c.style_id
                 JOIN colour co ON co.id = c.colour_id
                 JOIN app_user u ON u.id = c.opened_by
                WHERE c.shipment_id = %s
                ORDER BY c.closed_at NULLS FIRST, c.opened_at DESC""",
            (shipment_id,))
        rows = cur.fetchall()

        cur.execute("SELECT id, style_no FROM style WHERE buyer_id = %s "
                    "ORDER BY style_no", (ship["buyer_id"],))
        styles = cur.fetchall()
        cur.execute("SELECT id, name FROM colour ORDER BY name")
        colours = cur.fetchall()
        cur.execute(
            """SELECT u.id, u.display_name FROM app_user u
                 JOIN role r ON r.id = u.role_id
                WHERE u.active AND r.code IN ('supervisor', 'manager')
                ORDER BY u.display_name""")
        supervisors = cur.fetchall()

    return _page(request, "checklists.html", ship=ship, checklists=rows,
                 styles=styles, colours=colours, supervisors=supervisors)


@router.post("/shipment/{shipment_id}/checklists")
def open_checklist(
    shipment_id: int,
    stage: str = Form(...),
    style_id: int = Form(...),
    colour_id: int = Form(...),
    opened_by: int = Form(...),
) -> RedirectResponse:
    if stage not in ("S1", "S5", "S6"):
        raise HTTPException(422, "stage must be S1, S5 or S6")
    with cursor(commit=True) as cur:
        cur.execute("SELECT status FROM shipment WHERE id = %s", (shipment_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"no shipment {shipment_id}")
        if row["status"] != "active":
            raise HTTPException(409, f"shipment is {row['status']}")
        cur.execute(
            """INSERT INTO checklist
                 (shipment_id, style_id, colour_id, stage, opened_by)
               VALUES (%s, %s, %s, %s, %s)""",
            (shipment_id, style_id, colour_id, stage, opened_by))
    return RedirectResponse(
        f"/ui/shipment/{shipment_id}/checklists", status_code=303)


@router.post("/checklist/{checklist_id}/close")
def close_checklist(checklist_id: int) -> RedirectResponse:
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE checklist SET closed_at = now() "
            "WHERE id = %s AND closed_at IS NULL RETURNING shipment_id",
            (checklist_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(409, "already closed, or no such checklist")
    return RedirectResponse(
        f"/ui/shipment/{row['shipment_id']}/checklists", status_code=303)


# ==================================================== mismatch resolution ===

@router.post("/mismatch/{mismatch_id}/resolve")
def resolve_mismatch(
    mismatch_id: int,
    note: str = Form(""),
    shipment_id: int = Form(...),
) -> RedirectResponse:
    """
    Close out a detected mismatch.

    The note is kept rather than a status flag, because the useful question
    afterwards is not *whether* it was resolved but *what was found* — a
    swapped hangtag, a misprinted label, or a correct garment the system
    called wrong. The third case is a false positive and belongs in the O2
    figure; a boolean would lose it.
    """
    with cursor(commit=True) as cur:
        cur.execute(
            """UPDATE mismatch
                  SET resolved_at = now(), resolution_note = NULLIF(%s, '')
                WHERE id = %s RETURNING id""", (note.strip(), mismatch_id))
        if cur.fetchone() is None:
            raise HTTPException(404, f"no mismatch {mismatch_id}")
    return RedirectResponse(f"/ui/shipment/{shipment_id}", status_code=303)


# ==================================================================== setup ==

@router.get("/setup", response_class=HTMLResponse)
def setup(request: Request, shipment_id: int | None = None) -> HTMLResponse:
    """The manager's screen: purchase order, shipment, size allocation."""
    with cursor() as cur:
        cur.execute("SELECT id, code, name FROM buyer ORDER BY code")
        buyers = cur.fetchall()
        cur.execute(
            """SELECT st.id, st.style_no, st.buyer_id, b.code AS buyer_code
                 FROM style st JOIN buyer b ON b.id = st.buyer_id
                ORDER BY b.code, st.style_no""")
        styles = cur.fetchall()
        cur.execute("SELECT id, name FROM colour ORDER BY name")
        colours = cur.fetchall()
        cur.execute("SELECT canonical, sort_order FROM size_code "
                    "ORDER BY sort_order")
        sizes = cur.fetchall()
        cur.execute(
            """SELECT u.id, u.display_name FROM app_user u
                 JOIN role r ON r.id = u.role_id
                WHERE u.active AND r.code = 'manager' ORDER BY u.display_name""")
        managers = cur.fetchall()
        cur.execute(
            """SELECT s.id, s.reference, s.declared_qty, s.status::text AS status,
                      COALESCE(a.allocated, 0) AS allocated, p.buyer_id
                 FROM shipment s
                 JOIN purchase_order p ON p.id = s.po_id
                 LEFT JOIN (SELECT shipment_id, SUM(qty) AS allocated
                              FROM shipment_allocation GROUP BY 1) a
                        ON a.shipment_id = s.id
                ORDER BY s.id DESC LIMIT 40""")
        shipments_ = cur.fetchall()

        selected = None
        allocation: list[dict] = []
        if shipment_id is not None:
            selected = next(
                (s for s in shipments_ if s["id"] == shipment_id), None)
            if selected is not None:
                cur.execute(
                    """SELECT sc.canonical AS size, co.name AS colour, a.qty
                         FROM shipment_allocation a
                         JOIN size_code sc ON sc.id = a.size_code_id
                         JOIN colour co    ON co.id = a.colour_id
                        WHERE a.shipment_id = %s
                        ORDER BY co.name, sc.sort_order""", (shipment_id,))
                allocation = cur.fetchall()

    return _page(request, "setup.html", buyers=buyers, styles=styles,
                 colours=colours, sizes=sizes, managers=managers,
                 shipments=shipments_, selected=selected,
                 allocation=allocation)


@router.post("/setup/shipment")
def create_shipment(
    po_number: str = Form(...),
    buyer_id: int = Form(...),
    style_id: int = Form(...),
    reference: str = Form(...),
    ship_date: str = Form(...),
    declared_qty: int = Form(...),
    created_by: int = Form(...),
) -> RedirectResponse:
    """
    One purchase order, one shipment.

    The PO is created if its number is new and reused if it is not, because a
    purchase order is released as several shipments and the manager should not
    have to remember whether this is the first one.
    """
    if declared_qty <= 0:
        raise HTTPException(422, "declared quantity must be positive")
    with cursor(commit=True) as cur:
        cur.execute("SELECT id, total_qty FROM purchase_order WHERE po_number = %s",
                    (po_number.strip(),))
        po = cur.fetchone()
        if po is None:
            cur.execute(
                """INSERT INTO purchase_order
                     (po_number, buyer_id, style_id, total_qty, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (po_number.strip(), buyer_id, style_id, declared_qty,
                 created_by))
            po_id = cur.fetchone()["id"]
        else:
            po_id = po["id"]

        cur.execute("SELECT id FROM shipment WHERE reference = %s",
                    (reference.strip(),))
        if cur.fetchone() is not None:
            raise HTTPException(409, f"shipment {reference!r} already exists")

        cur.execute(
            """INSERT INTO shipment
                 (po_id, reference, ship_date, declared_qty, status, created_by)
               VALUES (%s, %s, %s, %s, 'draft', %s) RETURNING id""",
            (po_id, reference.strip(), ship_date, declared_qty, created_by))
        new_id = cur.fetchone()["id"]
    return RedirectResponse(f"/ui/setup?shipment_id={new_id}", status_code=303)


@router.post("/setup/allocation")
async def set_allocation(request: Request) -> RedirectResponse:
    """
    The size allocation, with O3's one unforgiving validation.

    ⭐ The allocated total must equal the declared total, and a mismatch is
    **refused rather than warned about**. This is the single point where a
    typo poisons everything downstream — the valid size set the recogniser
    parses against, every per-size count, every reconciliation delta — and a
    warning that can be clicked past is not a control.

    Saving also activates the shipment, because an allocation that balances is
    exactly the condition for the shipment to be scannable.
    """
    form = await request.form()
    shipment_id = int(str(form.get("shipment_id", "0")))
    colour_id = int(str(form.get("colour_id", "0")))

    rows: list[tuple[str, int]] = []
    for key, value in form.multi_items():
        if not key.startswith("qty_"):
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            qty = int(text)
        except ValueError:
            raise HTTPException(422, f"{key[4:]}: {text!r} is not a number")
        if qty < 0:
            raise HTTPException(422, f"{key[4:]}: quantity cannot be negative")
        if qty:
            rows.append((key[4:], qty))

    with cursor(commit=True) as cur:
        cur.execute("SELECT declared_qty, status FROM shipment WHERE id = %s",
                    (shipment_id,))
        ship = cur.fetchone()
        if ship is None:
            raise HTTPException(404, f"no shipment {shipment_id}")
        if ship["status"] == "closed":
            raise HTTPException(409, "shipment is closed")

        total = sum(q for _, q in rows)
        if total != ship["declared_qty"]:
            raise HTTPException(
                422,
                f"the allocation totals {total:,} but the shipment declares "
                f"{ship['declared_qty']:,} — a difference of "
                f"{total - ship['declared_qty']:+,}. Nothing was saved.",
            )

        cur.execute("DELETE FROM shipment_allocation WHERE shipment_id = %s",
                    (shipment_id,))
        for size, qty in rows:
            cur.execute(
                """INSERT INTO shipment_allocation
                     (shipment_id, size_code_id, colour_id, qty)
                   VALUES (%s,
                     (SELECT id FROM size_code WHERE canonical = %s),
                     %s, %s)""", (shipment_id, size, colour_id, qty))
        cur.execute(
            "UPDATE shipment SET status = 'active' WHERE id = %s "
            "AND status = 'draft'", (shipment_id,))
    return RedirectResponse(f"/ui/shipment/{shipment_id}", status_code=303)


# =============================================================== documents ===

@router.post("/shipment/{shipment_id}/packing-list")
def generate_packing_list(shipment_id: int) -> RedirectResponse:
    """
    Generate the buyer's packing list from carton-level data — O3, answering
    P4 (hand transcription).

    The host is the only writer. An .xlsx is a single zip archive with no
    partial write and no merge, so if several devices produced their own
    versions the last would silently replace the rest.
    """
    from scripts.packing_list import build
    try:
        build(shipment_id)
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(500, f"packing list failed: {exc}") from exc
    return RedirectResponse(f"/ui/shipment/{shipment_id}?done=packing-list",
                            status_code=303)


@router.post("/shipment/{shipment_id}/export")
def export_workbook(shipment_id: int) -> RedirectResponse:
    """The eight-sheet evidence workbook for this shipment."""
    from scripts.export_shipment import build
    try:
        build(shipment_id)
    except Exception as exc:                        # noqa: BLE001
        raise HTTPException(500, f"export failed: {exc}") from exc
    return RedirectResponse(f"/ui/shipment/{shipment_id}?done=export",
                            status_code=303)


@router.post("/shipment/{shipment_id}/close")
def close_shipment(shipment_id: int) -> RedirectResponse:
    with cursor(commit=True) as cur:
        cur.execute(
            """UPDATE shipment SET status = 'closed', closed_at = now()
                WHERE id = %s AND status = 'active' RETURNING id""",
            (shipment_id,))
        if cur.fetchone() is None:
            raise HTTPException(409, "shipment is not active")
    return RedirectResponse(f"/ui/shipment/{shipment_id}", status_code=303)


# ============================================================== evaluation ===

@router.get("/evaluation", response_class=HTMLResponse)
def evaluation(request: Request) -> HTMLResponse:
    """
    The figures Chapter 5 reports, read straight from the database.

    ⚠️ This page is for the researcher, not the supervisor, and it is the one
    screen that could embarrass the artefact — which is the point. The bypass
    rate is a measure of the design failing to be worth using, and the
    artefact has to be able to display it against itself or DP3 is untestable.

    Per-operator rows are shown here because §5.8 needs them, but §3.12
    commits this research to reporting no individual-level result: these are
    for the researcher's own analysis and are aggregated before anything is
    written down.
    """
    with cursor() as cur:
        # §5.8 — DP3, the bypass rate.
        cur.execute("SELECT * FROM v_bypass ORDER BY bypass_rate DESC NULLS LAST")
        bypass = cur.fetchall()

        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE recognition_result_id IS NULL)
                        AS bypasses,
                      COUNT(*) AS entries
                 FROM exception_event""")
        bypass_total = cur.fetchone()

        # §5.7 — latency as the operator experiences it.
        cur.execute(
            """SELECT stage,
                      COUNT(*) AS n,
                      ROUND(AVG(engine_ms)::numeric, 0)      AS engine_mean,
                      ROUND(AVG(round_trip_ms)::numeric, 0)  AS trip_mean,
                      ROUND(AVG(end_to_end_ms)::numeric, 0)  AS e2e_mean,
                      ROUND(STDDEV_SAMP(end_to_end_ms)::numeric, 0) AS e2e_sd,
                      MAX(end_to_end_ms)::numeric            AS e2e_max,
                      COUNT(*) FILTER (WHERE end_to_end_ms IS NULL)
                        AS missing_e2e
                 FROM v_latency GROUP BY stage ORDER BY stage""")
        latency = cur.fetchall()

        # §5.5 — the three recognition outcomes, never combined.
        cur.execute(
            """SELECT outcome::text AS outcome, COUNT(*) AS n,
                      ROUND(AVG(confidence)::numeric, 4) AS mean_confidence,
                      ROUND(AVG(duration_ms)::numeric, 0) AS mean_ms
                 FROM recognition_result GROUP BY 1 ORDER BY 1""")
        outcomes = cur.fetchall()

        # How the hangtag resolved. A lookup is only as good as the table.
        cur.execute(
            """SELECT CASE
                        WHEN hangtag_barcode IS NULL      THEN 'no hangtag (S1)'
                        WHEN barcode_map_id IS NOT NULL   THEN 'resolved'
                        ELSE 'unresolved — data gap'
                      END AS path,
                      COUNT(*) AS n
                 FROM scan_event GROUP BY 1 ORDER BY 2 DESC""")
        barcode_paths = cur.fetchall()

        # §5.6 — assistance requested. Self-reported, and reported as such.
        cur.execute(
            """SELECT a.cause, a.stage::text AS stage, COUNT(*) AS n
                 FROM assistance_event a GROUP BY 1, 2 ORDER BY 3 DESC""")
        assistance = cur.fetchall()

        # §5.5 — detected, but detected late.
        cur.execute(
            """SELECT COUNT(*) AS queued,
                      COUNT(*) FILTER (WHERE verdict = 'mismatch') AS late_mismatch,
                      ROUND(AVG(EXTRACT(EPOCH FROM (synced_at - queued_at)))::numeric, 1)
                        AS mean_delay_s
                 FROM scan_event
                WHERE synced_at IS NOT NULL AND queued_at IS NOT NULL
                  AND synced_at > queued_at + INTERVAL '2 seconds'""")
        late = cur.fetchone()

        cur.execute("SELECT COUNT(*) AS n FROM scan_event")
        scans = cur.fetchone()["n"]

    return _page(request, "evaluation.html", bypass=bypass,
                 bypass_total=bypass_total, latency=latency,
                 outcomes=outcomes, barcode_paths=barcode_paths,
                 assistance=assistance, late=late, scans=scans)
