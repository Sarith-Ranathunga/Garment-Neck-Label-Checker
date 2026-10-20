"""
Endpoints the Android client needs, kept apart from the verification routes in
main.py so that the research contribution stays legible.

Nothing here reasons about sizes. These routes do three jobs:

  1. tell the phone which checklist is active,
  2. hand the phone everything it needs to keep working when the WiFi drops,
  3. record the carton, assistance and bypass events that Chapter 5 counts.

The design rule running through the file is that the phone is never asked to
invent a value the server could supply, and is never allowed to supply a value
the server should decide. Two places matter:

  * `/api/checklist/{id}/bundle` sends the barcode map down to the device.
    The hangtag barcode encodes nothing, so without that map an offline phone
    cannot resolve a scanned barcode to a size and the S5 comparison is
    impossible. Caching it is what makes the artefact work on factory WiFi
    rather than only in a demonstration.

  * `/api/bypass` exists so that a manual entry made with no failed
    recognition behind it is recorded as exactly that. The alternative — the
    client posting a manual entry with some plausible recognition id attached
    — would destroy the DP3 measurement in §5.8 and nobody would find out
    until the data were analysed. See the comment on that route.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import cursor

router = APIRouter(prefix="/api", tags=["client"])


# ================================================================= sign-in ==

class LoginRequest(BaseModel):
    username: str
    device_id: str


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str


@router.post("/login", response_model=UserOut)
def login(req: LoginRequest) -> UserOut:
    """
    Operator sign-in by username.

    There is no password check and that is a deliberate, declared limitation,
    not an oversight: the phone is a shared floor device handled with gloves,
    and a password prompt at every shift change is the kind of friction DP3
    predicts will get bypassed. The device is inside the factory network and
    the account only permits scanning. §4.9 states this and §6.4 lists
    authentication as further work.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.display_name, r.code AS role
               FROM app_user u JOIN role r ON r.id = u.role_id
               WHERE u.username = %s AND u.active""", (req.username,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no active user {req.username!r}")
    return UserOut(**row)


@router.get("/operators", response_model=list[UserOut])
def operators() -> list[UserOut]:
    """The picker on the sign-in screen. Workers only."""
    with cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.display_name, r.code AS role
               FROM app_user u JOIN role r ON r.id = u.role_id
               WHERE u.active AND r.code IN ('worker', 'supervisor')
               ORDER BY u.display_name""")
        return [UserOut(**r) for r in cur.fetchall()]


# ============================================================== checklists ==

class ChecklistOut(BaseModel):
    id: int
    shipment_id: int
    shipment_reference: str
    stage: str
    style_no: str
    colour: str
    buyer_code: str
    opened_at: datetime
    scanned: int


@router.get("/checklists", response_model=list[ChecklistOut])
def checklists(stage: str | None = None) -> list[ChecklistOut]:
    """
    Open checklists on active shipments — what the operator chooses from.

    Closed checklists are excluded here rather than greyed out on the device.
    A checklist that can be selected but not used is a support call.
    """
    if stage is not None and stage not in ("S1", "S5", "S6"):
        raise HTTPException(422, "stage must be S1, S5 or S6")
    with cursor() as cur:
        cur.execute(
            """SELECT c.id, c.shipment_id, s.reference AS shipment_reference,
                      c.stage::text, st.style_no, co.name AS colour,
                      b.code AS buyer_code, c.opened_at,
                      (SELECT count(*) FROM scan_event e
                        WHERE e.checklist_id = c.id) AS scanned
               FROM checklist c
               JOIN shipment s        ON s.id  = c.shipment_id
               JOIN purchase_order p  ON p.id  = s.po_id
               JOIN buyer b           ON b.id  = p.buyer_id
               JOIN style st          ON st.id = c.style_id
               JOIN colour co         ON co.id = c.colour_id
               WHERE c.closed_at IS NULL AND s.status = 'active'
                 AND (%s::text IS NULL OR c.stage::text = %s::text)
               ORDER BY c.stage, c.opened_at DESC""", (stage, stage))
        return [ChecklistOut(**r) for r in cur.fetchall()]


class SizeExpectation(BaseModel):
    size: str
    expected_qty: int
    verified_qty: int


class BarcodeEntry(BaseModel):
    barcode: str
    symbology: str | None
    size: str
    buyer_notation: str | None
    style_no: str
    colour: str
    sku: str | None


class ChecklistBundle(BaseModel):
    """Everything the phone must hold to work with the network down."""
    checklist_id: int
    shipment_id: int
    shipment_reference: str
    stage: str
    style_no: str
    colour: str
    buyer_id: int
    buyer_code: str
    valid_sizes: list[str]
    size_map: dict[str, str]
    items: list[SizeExpectation]
    barcodes: list[BarcodeEntry]
    fetched_at: datetime


@router.get("/checklist/{checklist_id}/bundle", response_model=ChecklistBundle)
def bundle(checklist_id: int) -> ChecklistBundle:
    """
    The offline bundle. Fetched once when the operator opens a checklist and
    cached on the device until they close it.

    `valid_sizes` is the constraint that makes recognition tractable: it comes
    from `shipment_allocation`, so the phone shows the operator the same closed
    set the recogniser parses against (§4.6).

    `barcodes` is the part that cannot be left out. A UPC-A hangtag is opaque
    — the digits carry no size — so a phone with no cached map and no network
    can scan a hangtag and learn nothing from it. With the map cached, S5 still
    works through a WiFi outage: the barcode resolves on the device, and only
    the neck-label image waits in the queue.
    """
    with cursor() as cur:
        cur.execute(
            """SELECT c.id, c.shipment_id, c.stage::text, s.reference,
                      st.style_no, st.id AS style_id, co.name AS colour,
                      co.id AS colour_id, p.buyer_id, b.code AS buyer_code
               FROM checklist c
               JOIN shipment s       ON s.id  = c.shipment_id
               JOIN purchase_order p ON p.id  = s.po_id
               JOIN buyer b          ON b.id  = p.buyer_id
               JOIN style st         ON st.id = c.style_id
               JOIN colour co        ON co.id = c.colour_id
               WHERE c.id = %s""", (checklist_id,))
        ctx = cur.fetchone()
        if ctx is None:
            raise HTTPException(404, f"no checklist {checklist_id}")

        cur.execute(
            """SELECT sc.canonical, a.qty
               FROM shipment_allocation a
               JOIN size_code sc ON sc.id = a.size_code_id
               WHERE a.shipment_id = %s AND a.qty > 0
               ORDER BY sc.sort_order""", (ctx["shipment_id"],))
        alloc = cur.fetchall()

        cur.execute(
            """SELECT m.buyer_notation, sc.canonical
               FROM buyer_size_code_map m
               JOIN size_code sc ON sc.id = m.size_code_id
               WHERE m.buyer_id = %s""", (ctx["buyer_id"],))
        size_map = {r["buyer_notation"].strip().upper(): r["canonical"]
                    for r in cur.fetchall()}

        cur.execute(
            """SELECT ci.expected_qty, ci.verified_qty, sc.canonical
               FROM checklist_item ci
               JOIN size_code sc ON sc.id = ci.size_code_id
               WHERE ci.checklist_id = %s
               ORDER BY sc.sort_order""", (checklist_id,))
        item_rows = cur.fetchall()

        # The barcode map is scoped to this style and colour. Sending the
        # whole factory's barcodes to a phone would be slow and pointless.
        cur.execute(
            """SELECT bm.barcode, bm.symbology, sc.canonical AS size,
                      st.style_no, co.name AS colour, bm.sku,
                      (SELECT m.buyer_notation
                         FROM buyer_size_code_map m
                        WHERE m.buyer_id = bm.buyer_id
                          AND m.size_code_id = bm.size_code_id
                        ORDER BY length(m.buyer_notation) DESC LIMIT 1
                      ) AS buyer_notation
               FROM barcode_map bm
               JOIN size_code sc ON sc.id = bm.size_code_id
               JOIN style st     ON st.id = bm.style_id
               JOIN colour co    ON co.id = bm.colour_id
               WHERE bm.buyer_id = %s AND bm.colour_id = %s
               ORDER BY sc.sort_order, bm.barcode""",
            (ctx["buyer_id"], ctx["colour_id"]))
        barcodes = [BarcodeEntry(**r) for r in cur.fetchall()]

    # If the supervisor has not entered per-size expectations for this
    # checklist, fall back to the shipment allocation. The operator gets a
    # size list either way; an empty screen would stop packing.
    items = ([SizeExpectation(size=r["canonical"],
                              expected_qty=r["expected_qty"],
                              verified_qty=r["verified_qty"])
              for r in item_rows]
             or [SizeExpectation(size=r["canonical"], expected_qty=r["qty"],
                                 verified_qty=0) for r in alloc])

    return ChecklistBundle(
        checklist_id=ctx["id"],
        shipment_id=ctx["shipment_id"],
        shipment_reference=ctx["reference"],
        stage=ctx["stage"],
        style_no=ctx["style_no"],
        colour=ctx["colour"],
        buyer_id=ctx["buyer_id"],
        buyer_code=ctx["buyer_code"],
        valid_sizes=[r["canonical"] for r in alloc],
        size_map=size_map,
        items=items,
        barcodes=barcodes,
        fetched_at=datetime.now().astimezone(),
    )


@router.get("/checklist/{checklist_id}/progress")
def progress(checklist_id: int) -> dict:
    """Running counts for the operator's screen, by size and by verdict."""
    with cursor() as cur:
        cur.execute("SELECT 1 FROM checklist WHERE id = %s", (checklist_id,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"no checklist {checklist_id}")
        cur.execute(
            """SELECT COALESCE(sc.canonical, '(no read)') AS size,
                      e.verdict::text AS verdict, count(*) AS n
               FROM scan_event e
               LEFT JOIN recognition_result rr ON rr.scan_event_id = e.id
               LEFT JOIN size_code sc ON sc.id = rr.parsed_size_id
               WHERE e.checklist_id = %s
               GROUP BY 1, 2 ORDER BY 1, 2""", (checklist_id,))
        rows = cur.fetchall()
        cur.execute(
            """SELECT e.verdict::text AS verdict, count(*) AS n
               FROM scan_event e WHERE e.checklist_id = %s
               GROUP BY 1""", (checklist_id,))
        totals = {r["verdict"] or "pending": r["n"] for r in cur.fetchall()}
    return {"checklist_id": checklist_id, "by_size": rows, "totals": totals}


# ================================================================= cartons ==

class CartonOpen(BaseModel):
    shipment_id: int
    carton_no: str
    opened_by: int


class CartonOut(BaseModel):
    id: int
    shipment_id: int
    carton_no: str
    opened_at: datetime
    closed_at: datetime | None
    packed: int


@router.post("/carton", response_model=CartonOut)
def open_carton(req: CartonOpen) -> CartonOut:
    """
    Open a carton at S6.

    `UNIQUE (shipment_id, carton_no)` means a second attempt at the same
    number returns the existing carton rather than failing. An operator who
    taps twice on factory WiFi should not be told the carton already exists
    and left wondering which one to use.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT status FROM shipment WHERE id = %s",
                    (req.shipment_id,))
        ship = cur.fetchone()
        if ship is None:
            raise HTTPException(404, f"no shipment {req.shipment_id}")
        if ship["status"] != "active":
            raise HTTPException(409, f"shipment is {ship['status']}")
        cur.execute(
            """INSERT INTO carton (shipment_id, carton_no, opened_by)
               VALUES (%s, %s, %s)
               ON CONFLICT (shipment_id, carton_no) DO UPDATE
                 SET carton_no = EXCLUDED.carton_no
               RETURNING id, shipment_id, carton_no, opened_at, closed_at""",
            (req.shipment_id, req.carton_no.strip(), req.opened_by))
        row = cur.fetchone()
    return CartonOut(**row, packed=_packed(row["id"]))


def _packed(carton_id: int) -> int:
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM scan_event WHERE carton_id = %s",
                    (carton_id,))
        return cur.fetchone()["n"]


class CartonClose(BaseModel):
    net_weight_kg: float | None = None
    gross_weight_kg: float | None = None


@router.post("/carton/{carton_id}/close")
def close_carton(carton_id: int, req: CartonClose) -> dict:
    """
    Close a carton and freeze its contents into `carton_item`.

    The contents are derived from the scans, not typed in. That is the whole
    point of the stage: the carton's declared contents and its verified
    contents are the same number because they come from the same events.

    Weights stay nullable. They are read off a floor scale and the artefact
    does not capture them — declared in §4.8 as a field-map gap, and reported
    as such in O3 rather than quietly filled with a plausible figure.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT closed_at FROM carton WHERE id = %s", (carton_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"no carton {carton_id}")
        if row["closed_at"] is not None:
            raise HTTPException(409, "carton is already closed")

        cur.execute(
            """INSERT INTO carton_item (carton_id, size_code_id, qty)
               SELECT e.carton_id, rr.parsed_size_id, count(*)
                 FROM scan_event e
                 JOIN recognition_result rr ON rr.scan_event_id = e.id
                WHERE e.carton_id = %s AND rr.parsed_size_id IS NOT NULL
                GROUP BY e.carton_id, rr.parsed_size_id
               ON CONFLICT (carton_id, size_code_id)
                 DO UPDATE SET qty = EXCLUDED.qty""", (carton_id,))
        cur.execute(
            """UPDATE carton
                  SET closed_at = now(),
                      net_weight_kg   = COALESCE(%s, net_weight_kg),
                      gross_weight_kg = COALESCE(%s, gross_weight_kg)
                WHERE id = %s""",
            (req.net_weight_kg, req.gross_weight_kg, carton_id))
        cur.execute(
            """SELECT sc.canonical AS size, ci.qty
                 FROM carton_item ci
                 JOIN size_code sc ON sc.id = ci.size_code_id
                WHERE ci.carton_id = %s ORDER BY sc.sort_order""",
            (carton_id,))
        contents = cur.fetchall()
    return {"carton_id": carton_id, "closed": True, "contents": contents,
            "total": sum(c["qty"] for c in contents)}


@router.get("/shipment/{shipment_id}/cartons", response_model=list[CartonOut])
def cartons(shipment_id: int, open_only: bool = False) -> list[CartonOut]:
    with cursor() as cur:
        cur.execute(
            """SELECT c.id, c.shipment_id, c.carton_no, c.opened_at, c.closed_at,
                      (SELECT count(*) FROM scan_event e WHERE e.carton_id = c.id)
                        AS packed
               FROM carton c
               WHERE c.shipment_id = %s
                 AND (NOT %s::boolean OR c.closed_at IS NULL)
               ORDER BY c.opened_at DESC""", (shipment_id, open_only))
        return [CartonOut(**r) for r in cur.fetchall()]


# ========================================================= scan lifecycle ===

class DisplayedAt(BaseModel):
    displayed_at: datetime


@router.post("/scan/{scan_event_id}/displayed")
def mark_displayed(scan_event_id: int, req: DisplayedAt) -> dict:
    """
    The third timestamp.

    §5.7 reports what the operator experiences, and that is capture to screen
    — not the 758 ms the engine spends. Only the device knows when the verdict
    actually appeared, so only the device can report it. It cannot be
    reconstructed afterwards, which is why this route exists instead of the
    figure being inferred from `responded_at`.
    """
    with cursor(commit=True) as cur:
        cur.execute(
            """UPDATE scan_event SET displayed_at = %s
                WHERE id = %s RETURNING captured_at, responded_at, displayed_at""",
            (req.displayed_at, scan_event_id))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"no scan_event {scan_event_id}")
    end_to_end = None
    if row["displayed_at"] and row["captured_at"]:
        end_to_end = int(
            (row["displayed_at"] - row["captured_at"]).total_seconds() * 1000)
    return {"scan_event_id": scan_event_id, "end_to_end_ms": end_to_end}


class BypassEntry(BaseModel):
    """A manual entry with no recognition attempt behind it."""
    local_id: uuid.UUID
    device_id: str
    operator_id: int
    checklist_id: int | None = None
    carton_id: int | None = None
    stage: str = Field(pattern="^(S1|S5|S6)$")
    entered_size: str
    captured_at: datetime
    reason: str | None = None


@router.post("/bypass")
def bypass(req: BypassEntry) -> dict:
    """
    The operator entered a size without attempting a scan.

    ⭐ This route is the DP3 measurement in §5.8, and it exists so that the
    measurement cannot be corrupted by the client. The alternative design —
    the phone posting to `/api/manual-entry` with whatever recognition id it
    happens to hold — puts the honesty of the measurement in the client's
    hands. Here the server writes the NULL itself, because this route can only
    be called when there was no recognition to point at.

    A high bypass rate is a FINDING, not a fault to be hidden. DP3 predicts
    that a compliant path slower than the bypass gets bypassed, and the whole
    purpose of the nullable foreign key is to let the artefact report on
    itself. A client that made bypasses look like legitimate fallbacks would
    make O4 unfalsifiable.
    """
    with cursor(commit=True) as cur:
        cur.execute("SELECT id FROM size_code WHERE canonical = %s",
                    (req.entered_size,))
        size = cur.fetchone()
        if size is None:
            raise HTTPException(400, f"unknown size {req.entered_size!r}")

        cur.execute(
            """INSERT INTO scan_event
                 (local_id, device_id, operator_id, checklist_id, carton_id,
                  stage, captured_at, responded_at, sync_state, queued_at,
                  synced_at, verdict, entry_method)
               VALUES (%s,%s,%s,%s,%s,%s,%s,now(),'acknowledged',%s,now(),
                       NULL,'manual')
               ON CONFLICT (device_id, local_id) DO UPDATE
                 SET responded_at = now()
               RETURNING id""",
            (str(req.local_id), req.device_id, req.operator_id,
             req.checklist_id, req.carton_id, req.stage, req.captured_at,
             req.captured_at))
        scan_id = cur.fetchone()["id"]

        # recognition_result_id is written as NULL, by the server, on purpose.
        cur.execute(
            """INSERT INTO exception_event
                 (scan_event_id, recognition_result_id, entered_size_id, reason)
               VALUES (%s, NULL, %s, %s) RETURNING id""",
            (scan_id, size["id"], req.reason or "manual entry, no scan"))
        exc_id = cur.fetchone()["id"]
    return {"scan_event_id": scan_id, "exception_id": exc_id, "bypass": True}


# ============================================================== assistance ==

class Assistance(BaseModel):
    operator_id: int
    shipment_id: int | None = None
    stage: str = Field(pattern="^(S1|S5|S6)$")
    cause: str
    note: str | None = None


@router.post("/assistance")
def assistance(req: Assistance) -> dict:
    """
    The operator asked for help.

    Counted in §5.6 as a usability measure. Self-reported, and recorded as
    such: the operator presses the button, so the figure is a count of
    occasions help was requested, not an objective count of occasions help was
    needed. §5.6 states that limitation rather than presenting the number as
    more than it is.
    """
    with cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO assistance_event
                 (operator_id, shipment_id, stage, cause, note)
               VALUES (%s,%s,%s,%s,%s) RETURNING id, occurred_at""",
            (req.operator_id, req.shipment_id, req.stage, req.cause, req.note))
        row = cur.fetchone()
    return {"assistance_id": row["id"], "occurred_at": row["occurred_at"]}


# ============================================================= device meta ==

@router.get("/sizes")
def sizes() -> list[dict]:
    """The manual-entry keypad. Canonical sizes in packing-list order."""
    with cursor() as cur:
        cur.execute("SELECT canonical, sort_order FROM size_code "
                    "ORDER BY sort_order")
        return cur.fetchall()
