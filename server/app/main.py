"""
Garment Neck Label Checker — FastAPI service.

Runs on an existing supervisor PC. Phones on the factory WiFi post scans to
it; the supervisor's browser reads from it. Nothing leaves the building.

The endpoints are deliberately thin. All the reasoning lives in
app/recognition.py, which is the part that is a research contribution.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import barcode as bc
from .client_api import router as client_router
from .web import router as web_router
from .config import settings
from .db import cursor
from .recognition import (
    LabelRecogniser, Outcome, SizeCodeMap, Verdict,
    check_context, compare, recognise_size,
)

app = FastAPI(
    title="Garment Neck Label Checker",
    version="0.1.0",
    description="Neck-label verification for apparel packing departments.",
)

_recogniser = LabelRecogniser(gpu=settings.easyocr_gpu)

# ----------------------------------------------------------------- models --

class ScanRequest(BaseModel):
    local_id: uuid.UUID
    device_id: str
    operator_id: int
    checklist_id: int | None = None
    carton_id: int | None = None
    stage: str = Field(pattern="^(S1|S5|S6)$")
    hangtag_barcode: str | None = None
    hangtag_size_raw: str | None = None
    scanned_style: str | None = None
    scanned_colour: str | None = None
    captured_at: datetime
    queued_at: datetime | None = None


class ScanResponse(BaseModel):
    scan_event_id: int
    # ⭐ Returned so the client never has to invent it. When a no-read sends
    #   the operator to manual entry, the phone posts THIS id back; when there
    #   was no recognition at all it uses /api/bypass, which writes NULL
    #   server-side. Between them the client has no way to fabricate a
    #   recognition id, and the DP3 measurement in §5.8 stays honest.
    recognition_result_id: int | None = None
    verdict: str
    outcome: str
    canonical_size: str | None
    rotation_used: int | None
    confidence: float | None
    engine_ms: int
    hangtag_size_raw: str | None = None
    hangtag_source: str | None = None
    message: str


# ------------------------------------------------------------- data access --

def load_size_map(buyer_id: int) -> SizeCodeMap:
    with cursor() as cur:
        cur.execute("SELECT code FROM buyer WHERE id = %s", (buyer_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"no buyer {buyer_id}")
        cur.execute(
            """SELECT m.buyer_notation, s.canonical
               FROM buyer_size_code_map m
               JOIN size_code s ON s.id = m.size_code_id
               WHERE m.buyer_id = %s""", (buyer_id,))
        mapping = {r["buyer_notation"].strip().upper(): r["canonical"]
                   for r in cur.fetchall()}
    return SizeCodeMap(buyer_code=row["code"], notation_to_canonical=mapping)


def lookup_barcode(raw: str | None) -> dict | None:
    """The `barcode_map` row for a scanned hangtag, or None."""
    if not raw:
        return None
    with cursor() as cur:
        cur.execute(
            """SELECT bm.id, bm.sku, st.style_no, co.name AS colour,
                      m.buyer_notation AS size_notation, sc.canonical
               FROM barcode_map bm
               JOIN style st     ON st.id = bm.style_id
               JOIN colour co    ON co.id = bm.colour_id
               JOIN size_code sc ON sc.id = bm.size_code_id
               LEFT JOIN buyer_size_code_map m
                      ON m.buyer_id = bm.buyer_id
                     AND m.size_code_id = bm.size_code_id
               WHERE bm.barcode = %s
               ORDER BY length(m.buyer_notation) DESC
               LIMIT 1""", (bc.normalise(raw),))
        return cur.fetchone()


def checklist_context(checklist_id: int) -> dict:
    """
    The active shipment's size set comes from shipment_allocation.

    This is the constraint that makes recognition tractable — see
    recognition.find_candidates and Chapter 4 §4.6.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT c.id, c.shipment_id, c.stage,
                      st.style_no, co.name AS colour, p.buyer_id
               FROM checklist c
               JOIN style st ON st.id = c.style_id
               JOIN colour co ON co.id = c.colour_id
               JOIN shipment s ON s.id = c.shipment_id
               JOIN purchase_order p ON p.id = s.po_id
               WHERE c.id = %s""", (checklist_id,))
        ctx = cur.fetchone()
        if ctx is None:
            raise HTTPException(404, f"no checklist {checklist_id}")
        cur.execute(
            """SELECT DISTINCT sc.canonical
               FROM shipment_allocation a
               JOIN size_code sc ON sc.id = a.size_code_id
               WHERE a.shipment_id = %s AND a.qty > 0""", (ctx["shipment_id"],))
        ctx["valid_sizes"] = [r["canonical"] for r in cur.fetchall()]
    return ctx


# ------------------------------------------------------------- endpoints ----

_INDEX = Path(__file__).resolve().parents[1] / "templates" / "index.html"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Landing page, so opening the address in a browser shows something."""
    return (_INDEX.read_text(encoding="utf-8") if _INDEX.exists()
            else "<h1>GNLC</h1><p>Service running. See <a href=/docs>/docs</a>.</p>")


@app.get("/health")
def health() -> dict:
    """Checked each morning before packing starts. See §4.9."""
    with cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/scan", response_model=ScanResponse)
async def scan(
    payload: str = Form(..., description="ScanRequest as JSON"),
    image: UploadFile | None = File(None),
) -> ScanResponse:
    """
    One scan. The phone sends the neck-label image plus everything already
    known on the device; the service returns a single verdict.

    The hangtag barcode is NOT sent for decoding — the phone decodes it with
    ML Kit, on device, instantly. Only the neck label needs the server,
    because only the neck label needs OCR.
    """
    req = ScanRequest.model_validate_json(payload)

    if req.checklist_id is None:
        raise HTTPException(400, "checklist_id is required")
    ctx = checklist_context(req.checklist_id)
    size_map = load_size_map(ctx["buyer_id"])

    # --- resolve the hangtag ------------------------------------------------
    # The phone resolves the barcode from its cached map, which is what keeps
    # S5 working through a WiFi outage. The server resolves it again here
    # rather than trusting the device: a phone holding a stale cache would
    # otherwise compare against an expected size that no longer applies, and
    # that is a false positive — the failure mode O2 is measuring.
    tag = bc.resolve(req.hangtag_barcode or "",
                     lookup_barcode(req.hangtag_barcode))
    if tag.size_notation:
        req = req.model_copy(update={"hangtag_size_raw": tag.size_notation})
    if tag.style_no and not req.scanned_style:
        req = req.model_copy(update={"scanned_style": tag.style_no})
    if tag.colour and not req.scanned_colour:
        req = req.model_copy(update={"scanned_colour": tag.colour})

    # --- O2 context check, before anything expensive -----------------------
    #
    # ⚠️ Only run when the hangtag actually resolved.
    #
    # `check_context` answers "can I confirm this garment belongs here?", and
    # returns False both when the answer is no and when there is nothing to
    # check. Those are different findings and must not be merged:
    #
    #   * resolved, and the attributes disagree -> OUT_OF_CONTEXT. The wrong
    #     garment is at this station. A real finding about the floor.
    #   * not resolved at all                   -> a DATA GAP. The system does
    #     not know what this garment should be, which says nothing about the
    #     garment.
    #
    # Reporting the second as the first produces a false positive counted
    # against O2 and sends the operator to pull a garment that is very likely
    # correct — the precise failure this research measures. The data gap is
    # handled below, after recognition, so the label reading is still recorded.
    if req.stage == "S5" and tag.source is not bc.BarcodeSource.UNRESOLVED:
        if not check_context(req.scanned_style, req.scanned_colour,
                             ctx["style_no"], ctx["colour"]):
            sid, _ = _record(req, None, Verdict.OUT_OF_CONTEXT, None, tag)
            return ScanResponse(
                scan_event_id=sid, verdict=Verdict.OUT_OF_CONTEXT.value,
                outcome=Outcome.NO_READ.value, canonical_size=None,
                rotation_used=None, confidence=None, engine_ms=0,
                hangtag_size_raw=req.hangtag_size_raw,
                hangtag_source=tag.source.value,
                message="Out of context — this garment is not on the active checklist.",
            )

    if image is None:
        raise HTTPException(400, "image is required for a recognised scan")

    raw = await image.read()
    if len(raw) > settings.max_image_bytes:
        raise HTTPException(
            413,
            f"image is {len(raw)} bytes; crop and compress on the device "
            f"(limit {settings.max_image_bytes})",
        )

    import cv2
    import numpy as np
    arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise HTTPException(400, "image could not be decoded")

    result = recognise_size(arr, ctx["valid_sizes"], size_map, _recogniser)

    if req.stage == "S1":
        # Nothing to verify against: no hangtag exists yet. The size is
        # ESTABLISHED here, not verified. Keeping the two apart is a wording
        # rule in this research, and the code follows it.
        verdict = Verdict.NO_READ if result.canonical_size is None else Verdict.MATCH
    elif tag.source is bc.BarcodeSource.UNRESOLVED:
        # Either no hangtag was scanned, or the one scanned is not in the
        # system. Both are data gaps: there is nothing to compare against, so
        # no claim about this garment's size can be made either way. The
        # label reading is still recorded, because it is the only thing known
        # about the garment and the supervisor will want it.
        verdict = Verdict.NO_READ
    else:
        verdict = compare(result.canonical_size, req.hangtag_size_raw, size_map)

    scan_id, recog_id = _record(req, result, verdict, size_map, tag)

    return ScanResponse(
        scan_event_id=scan_id,
        recognition_result_id=recog_id,
        verdict=verdict.value,
        outcome=result.outcome.value,
        canonical_size=result.canonical_size,
        rotation_used=result.rotation_used,
        confidence=result.confidence,
        engine_ms=result.duration_ms,
        hangtag_size_raw=req.hangtag_size_raw,
        hangtag_source=tag.source.value,
        message=_message(verdict, result, tag),
    )


def _message(verdict: Verdict, result, tag=None) -> str:
    if verdict is Verdict.MISMATCH:
        return "MISMATCH — neck label and hangtag disagree. Set this garment aside."
    if verdict is Verdict.NO_READ:
        if tag is not None and tag.source is bc.BarcodeSource.UNRESOLVED:
            if tag.raw:
                return ("Hangtag not in the system. There is nothing to "
                        "compare against, so this is a data gap, not a size "
                        "error — call the supervisor rather than setting the "
                        "garment aside.")
            return ("No hangtag was scanned, so there was nothing to compare "
                    "against. Scan the hangtag and photograph the label again.")
        return "No read. Enter the size manually; the exception is logged."
    if verdict is Verdict.OUT_OF_CONTEXT:
        return "Out of context — not on the active checklist."
    return f"Match — {result.canonical_size}."


def _record(req: ScanRequest, result, verdict: Verdict, size_map,
            tag=None) -> tuple[int, int | None]:
    """
    Write the scan and, when recognition ran, its result.

    Returns `(scan_event_id, recognition_result_id)`. The second value goes
    back to the client so a manual entry following a no-read can point at the
    recognition that actually failed, instead of the client inventing one.
    """
    hangtag_canonical = (
        size_map.resolve(req.hangtag_size_raw) if size_map else None
    )
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM size_code WHERE canonical = %s",
                    (hangtag_canonical,))
        row = cur.fetchone()
        hangtag_size_id = row["id"] if row else None

        cur.execute(
            """INSERT INTO scan_event
                 (local_id, device_id, operator_id, checklist_id, carton_id,
                  stage, hangtag_barcode, barcode_map_id, hangtag_size_raw,
                  hangtag_size_id, captured_at, responded_at, sync_state,
                  queued_at, synced_at, verdict, entry_method)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),'acknowledged',
                       %s,now(),%s,%s)
               ON CONFLICT (device_id, local_id) DO UPDATE
                 SET responded_at = now()
               RETURNING id""",
            (str(req.local_id), req.device_id, req.operator_id,
             req.checklist_id, req.carton_id, req.stage, req.hangtag_barcode,
             tag.barcode_map_id if tag else None,
             req.hangtag_size_raw, hangtag_size_id, req.captured_at,
             req.queued_at, verdict.value,
             "recognised" if result else "manual"),
        )
        scan_id = cur.fetchone()["id"]
        recog_id: int | None = None

        if result is not None:
            cur.execute("SELECT id FROM size_code WHERE canonical = %s",
                        (result.canonical_size,))
            prow = cur.fetchone()
            cur.execute(
                """INSERT INTO recognition_result
                     (scan_event_id, engine, engine_version, rotation_used,
                      rotations_tried, raw_text, parsed_size_id, confidence,
                      outcome, duration_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (scan_event_id) DO UPDATE
                     SET duration_ms = EXCLUDED.duration_ms
                   RETURNING id""",
                (scan_id, result.engine, result.engine_version,
                 result.rotation_used, result.rotations_tried, result.raw_text,
                 prow["id"] if prow else None, result.confidence,
                 result.outcome.value, result.duration_ms),
            )
            rrow = cur.fetchone()
            recog_id = rrow["id"] if rrow else None

        if verdict is Verdict.MISMATCH:
            cur.execute(
                """INSERT INTO mismatch
                     (scan_event_id, neck_size_id, hangtag_size_id, carton_id)
                   SELECT %s, (SELECT id FROM size_code WHERE canonical = %s),
                          %s, %s""",
                (scan_id, result.canonical_size if result else None,
                 hangtag_size_id, req.carton_id),
            )
    return scan_id, recog_id


class ManualEntry(BaseModel):
    scan_event_id: int
    entered_size: str
    recognition_result_id: int | None = None
    reason: str | None = None


@app.post("/api/manual-entry")
def manual_entry(entry: ManualEntry) -> dict:
    """
    The O1 fallback path: no garment can be blocked indefinitely.

    ⚠️ recognition_result_id is the DP3 measurement. The phone sends the id of
    the failed recognition when there was one, and NULL when there was not.
    A NULL means the operator entered a size manually without a recognition
    failure behind it — a bypass of the control. §5.8 reports that proportion.

    The client must not invent a value here. An honest NULL is the point.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM size_code WHERE canonical = %s",
                    (entry.entered_size,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(400, f"unknown size {entry.entered_size}")
        cur.execute(
            """INSERT INTO exception_event
                 (scan_event_id, recognition_result_id, entered_size_id, reason)
               VALUES (%s,%s,%s,%s) RETURNING id""",
            (entry.scan_event_id, entry.recognition_result_id, row["id"],
             entry.reason),
        )
        exc_id = cur.fetchone()["id"]
        cur.execute(
            "UPDATE scan_event SET entry_method = 'manual' WHERE id = %s",
            (entry.scan_event_id,))
    return {"exception_id": exc_id,
            "bypass": entry.recognition_result_id is None}


@app.get("/api/shipment/{shipment_id}/reconciliation")
def reconciliation(shipment_id: int) -> list[dict]:
    """O3: the four-point check, allocation -> S1 -> S5 -> S6."""
    with cursor() as cur:
        cur.execute("SELECT * FROM v_reconciliation WHERE shipment_id = %s",
                    (shipment_id,))
        return cur.fetchall()


@app.get("/api/shipment/{shipment_id}/flow")
def packing_flow(shipment_id: int) -> dict:
    """
    What the supervisor interface displays: packing-flow monitoring.

    O5 is DEMONSTRATED, not evaluated — it produces no findings of its own
    and admits no accuracy measure, so there is nothing to evaluate against.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT e.stage, e.verdict, count(*) AS n
               FROM scan_event e
               LEFT JOIN checklist c ON c.id = e.checklist_id
               LEFT JOIN carton ct ON ct.id = e.carton_id
               WHERE c.shipment_id = %s OR ct.shipment_id = %s
               GROUP BY e.stage, e.verdict ORDER BY e.stage""",
            (shipment_id, shipment_id))
        by_stage = cur.fetchall()
        cur.execute("SELECT * FROM v_reconciliation WHERE shipment_id = %s",
                    (shipment_id,))
        recon = cur.fetchall()
    return {"by_stage": by_stage, "reconciliation": recon}


class AllocationRow(BaseModel):
    size: str
    colour: str
    qty: int


@app.post("/api/shipment/{shipment_id}/allocation")
def set_allocation(shipment_id: int, rows: list[AllocationRow]) -> dict:
    """
    Manager's size allocation, with the O3 validation.

    The allocated total must equal the declared total. This is the one point
    where a typo poisons everything downstream — every count, every
    reconciliation, every verdict — so it is refused rather than warned about.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT declared_qty, status FROM shipment WHERE id = %s",
                    (shipment_id,))
        ship = cur.fetchone()
        if ship is None:
            raise HTTPException(404, f"no shipment {shipment_id}")
        if ship["status"] == "closed":
            raise HTTPException(409, "shipment is closed")

        total = sum(r.qty for r in rows)
        if total != ship["declared_qty"]:
            raise HTTPException(
                422,
                f"allocation totals {total} but the shipment declares "
                f"{ship['declared_qty']} — difference of "
                f"{total - ship['declared_qty']:+d}",
            )

        cur.execute("DELETE FROM shipment_allocation WHERE shipment_id = %s",
                    (shipment_id,))
        for r in rows:
            cur.execute(
                """INSERT INTO shipment_allocation
                     (shipment_id, size_code_id, colour_id, qty)
                   VALUES (%s,
                     (SELECT id FROM size_code WHERE canonical = %s),
                     (SELECT id FROM colour WHERE name = %s), %s)""",
                (shipment_id, r.size, r.colour, r.qty))
    return {"shipment_id": shipment_id, "allocated": total, "validated": True}


# The client routes live in their own module so the verification logic above
# stays readable. Registered last so that FastAPI's generated /docs lists the
# research endpoints first.
app.include_router(client_router)

# The supervisor interface — increment 4, O5. Monitoring and setup only;
# nothing here scans.
app.include_router(web_router)
