"""
Tests for the supervisor interface.

O5 is demonstrated rather than evaluated, so there is no accuracy to assert.
What these tests check instead is that the pages **render against the real
schema** and that the figures on them are the figures Chapter 5 will quote —
a dashboard that renders but silently shows a wrong total is worse than one
that errors, because the wrong number ends up in the dissertation.

The validation test at the bottom is the one that matters most: the
allocation total must be refused when it disagrees with the declared total,
and nothing may be written when it is.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jinja2")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import cursor  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    try:
        with cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM shipment")
            if cur.fetchone()["n"] == 0:
                pytest.skip("database reachable but not seeded")
    except Exception as exc:                        # pragma: no cover
        pytest.skip(f"no database: {exc}")
    # follow_redirects stays off so the 303s can be asserted on directly.
    return TestClient(app, follow_redirects=False)


def text(response) -> str:
    """Page text with tags stripped, for assertions that ignore markup."""
    return re.sub(r"<[^>]+>", " ", response.text)


# ============================================================ every page ====

def test_landing_page_links_into_the_interface(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/ui" in r.text


def test_shipments_index_renders(client):
    r = client.get("/ui")
    assert r.status_code == 200, r.text[:400]
    assert "SHP-130022-01" in r.text


def test_shipments_index_with_trailing_slash(client):
    assert client.get("/ui/").status_code == 200


def test_dashboard_renders(client):
    r = client.get("/ui/shipment/1")
    assert r.status_code == 200, r.text[:400]
    body = text(r)
    assert "Four-point reconciliation" in body
    assert "HUEL" in body


def test_dashboard_unknown_shipment_is_404(client):
    assert client.get("/ui/shipment/999999").status_code == 404


def test_checklists_page_renders(client):
    r = client.get("/ui/shipment/1/checklists")
    assert r.status_code == 200, r.text[:400]
    assert "Open a checklist" in text(r) or "closed" in text(r)


def test_setup_page_renders(client):
    r = client.get("/ui/setup")
    assert r.status_code == 200, r.text[:400]
    assert "Size allocation" in text(r)


def test_setup_page_with_a_shipment_selected(client):
    r = client.get("/ui/setup", params={"shipment_id": 1})
    assert r.status_code == 200, r.text[:400]
    assert "56,000" in r.text, "the declared total should be formatted"


def test_evaluation_page_renders(client):
    r = client.get("/ui/evaluation")
    assert r.status_code == 200, r.text[:400]
    body = text(r)
    assert "Bypass behaviour" in body
    assert "Latency" in body


# ================================================ the numbers on the page ===

def test_the_dashboard_total_matches_the_database(client):
    """
    The reconciliation total shown must equal the allocation in the database.

    This is the figure a reader of Chapter 5 would check first, and the view
    it comes from does a CROSS JOIN over every size code — exactly the kind of
    query that quietly double-counts.
    """
    with cursor() as cur:
        cur.execute(
            "SELECT SUM(qty) AS total FROM shipment_allocation WHERE shipment_id = 1")
        allocated = cur.fetchone()["total"]

    r = client.get("/ui/shipment/1")
    assert f"{int(allocated):,}" in r.text, \
        f"the page does not show the real allocated total {allocated:,}"


def test_the_deltas_are_shown_not_just_the_counts(client):
    """
    O3 claims a divergence is attributable to the stage where it occurred.

    Four raw counts leave the reader to subtract. The deltas are the claim, so
    the table has to carry them.
    """
    r = client.get("/ui/shipment/1")
    assert r.text.count("&Delta;") >= 3, "the three deltas must be column headers"


def test_stage_one_and_five_are_not_added_together(client):
    """
    S1 establishes a size and S5 verifies one. The page must keep them apart,
    because a match at S1 is not evidence about O2 — nothing was compared.
    """
    body = text(client.get("/ui/shipment/1")).lower()
    assert "established" in body
    assert "verified" in body
    assert "stage 1" in body and "stage 5" in body


# =============================== ⭐ the allocation validation, O3's guard ===

def _new_shipment(client, declared: int) -> int:
    """A draft shipment to test the allocation guard against."""
    reference = f"TEST-{uuid.uuid4().hex[:8].upper()}"
    with cursor() as cur:
        cur.execute("SELECT id FROM app_user WHERE username = 'mgr'")
        manager = cur.fetchone()["id"]
        cur.execute("SELECT id FROM style WHERE buyer_id = 1 LIMIT 1")
        style = cur.fetchone()["id"]

    r = client.post("/ui/setup/shipment", data={
        "po_number": "130022",          # existing PO, must be reused
        "buyer_id": "1",
        "style_id": str(style),
        "reference": reference,
        "ship_date": str(date.today() + timedelta(days=21)),
        "declared_qty": str(declared),
        "created_by": str(manager),
    })
    assert r.status_code == 303, r.text[:300]
    match = re.search(r"shipment_id=(\d+)", r.headers["location"])
    assert match, r.headers["location"]
    return int(match.group(1))


def test_a_new_shipment_reuses_an_existing_purchase_order(client):
    before_id = _new_shipment(client, 100)
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM purchase_order WHERE po_number = '130022'")
        assert cur.fetchone()["n"] == 1, "a second PO row was created"
        cur.execute("SELECT status::text AS s FROM shipment WHERE id = %s",
                    (before_id,))
        assert cur.fetchone()["s"] == "draft", "a new shipment starts as draft"


def test_an_unbalanced_allocation_is_refused_and_writes_nothing(client):
    """
    ⭐ The one unforgiving validation in the artefact.

    A typo here poisons the valid size set the recogniser parses against,
    every per-size count and every reconciliation delta. So it is refused
    rather than warned about — and, critically, **nothing is written**: a
    partial allocation would be worse than none, because it would look like
    data.
    """
    sid = _new_shipment(client, 100)

    r = client.post("/ui/setup/allocation", data={
        "shipment_id": str(sid), "colour_id": "1",
        "qty_S": "30", "qty_M": "30",       # 60, not 100
    })
    assert r.status_code == 422
    assert "100" in r.text and "60" in r.text, \
        "the message must name both totals so the manager can find the typo"

    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM shipment_allocation "
                    "WHERE shipment_id = %s", (sid,))
        assert cur.fetchone()["n"] == 0, "a refused allocation wrote rows anyway"
        cur.execute("SELECT status::text AS s FROM shipment WHERE id = %s", (sid,))
        assert cur.fetchone()["s"] == "draft", "a refused allocation activated it"


def test_a_balanced_allocation_saves_and_activates_the_shipment(client):
    """An allocation that balances is exactly the condition for scannability."""
    sid = _new_shipment(client, 100)

    r = client.post("/ui/setup/allocation", data={
        "shipment_id": str(sid), "colour_id": "1",
        "qty_S": "40", "qty_M": "60",
    })
    assert r.status_code == 303, r.text[:300]
    assert r.headers["location"] == f"/ui/shipment/{sid}"

    with cursor() as cur:
        cur.execute("SELECT SUM(qty) AS t FROM shipment_allocation "
                    "WHERE shipment_id = %s", (sid,))
        assert cur.fetchone()["t"] == 100
        cur.execute("SELECT status::text AS s FROM shipment WHERE id = %s", (sid,))
        assert cur.fetchone()["s"] == "active"


def test_saving_an_allocation_replaces_rather_than_adds(client):
    """
    A correction must not double the shipment.

    The handler deletes before inserting; without that, a manager fixing one
    size would silently add a second full set and the reconciliation would
    show a surplus that never existed.
    """
    sid = _new_shipment(client, 100)
    for payload in ({"qty_S": "40", "qty_M": "60"},
                    {"qty_S": "50", "qty_M": "50"}):
        r = client.post("/ui/setup/allocation",
                        data={"shipment_id": str(sid), "colour_id": "1",
                              **payload})
        assert r.status_code == 303

    with cursor() as cur:
        cur.execute("SELECT SUM(qty) AS t, COUNT(*) AS n "
                    "FROM shipment_allocation WHERE shipment_id = %s", (sid,))
        row = cur.fetchone()
    assert row["t"] == 100, "the allocation doubled instead of being replaced"
    assert row["n"] == 2


def test_allocation_rejects_a_non_numeric_quantity(client):
    sid = _new_shipment(client, 100)
    r = client.post("/ui/setup/allocation", data={
        "shipment_id": str(sid), "colour_id": "1", "qty_S": "forty"})
    assert r.status_code == 422


def test_allocation_rejects_a_negative_quantity(client):
    sid = _new_shipment(client, 100)
    r = client.post("/ui/setup/allocation", data={
        "shipment_id": str(sid), "colour_id": "1",
        "qty_S": "-10", "qty_M": "110"})
    assert r.status_code == 422


def test_allocation_on_an_unknown_shipment_is_404(client):
    r = client.post("/ui/setup/allocation", data={
        "shipment_id": "999999", "colour_id": "1", "qty_S": "1"})
    assert r.status_code == 404


# ====================================================== checklist control ===

def test_open_and_close_a_checklist(client):
    with cursor() as cur:
        cur.execute("SELECT id FROM app_user WHERE username = 'sup'")
        sup = cur.fetchone()["id"]
        cur.execute("SELECT id FROM style WHERE buyer_id = 1 LIMIT 1")
        style = cur.fetchone()["id"]
        cur.execute("SELECT id FROM colour WHERE name = 'BLACK'")
        colour = cur.fetchone()["id"]

    r = client.post("/ui/shipment/1/checklists", data={
        "stage": "S1", "style_id": str(style),
        "colour_id": str(colour), "opened_by": str(sup)})
    assert r.status_code == 303

    with cursor() as cur:
        cur.execute("SELECT id FROM checklist WHERE shipment_id = 1 "
                    "AND closed_at IS NULL ORDER BY id DESC LIMIT 1")
        cid = cur.fetchone()["id"]

    r = client.post(f"/ui/checklist/{cid}/close")
    assert r.status_code == 303

    with cursor() as cur:
        cur.execute("SELECT closed_at FROM checklist WHERE id = %s", (cid,))
        assert cur.fetchone()["closed_at"] is not None

    # Closing twice is a conflict, not a silent success.
    assert client.post(f"/ui/checklist/{cid}/close").status_code == 409


def test_a_bad_stage_is_refused(client):
    r = client.post("/ui/shipment/1/checklists", data={
        "stage": "S9", "style_id": "1", "colour_id": "1", "opened_by": "2"})
    assert r.status_code == 422


def test_a_checklist_cannot_be_opened_on_a_draft_shipment(client):
    """A draft shipment has no validated size set, so it is not scannable."""
    sid = _new_shipment(client, 100)
    r = client.post(f"/ui/shipment/{sid}/checklists", data={
        "stage": "S1", "style_id": "1", "colour_id": "1", "opened_by": "2"})
    assert r.status_code == 409


# ====================================================== mismatch handling ===

def test_resolving_a_mismatch_keeps_the_note(client):
    """
    The note is kept rather than a boolean, because "correct garment, system
    was wrong" is a false positive and belongs in the O2 figure. A flag would
    lose that.
    """
    with cursor() as cur:
        cur.execute("SELECT id FROM mismatch WHERE resolved_at IS NULL "
                    "ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("no unresolved mismatch in the database")

    r = client.post(f"/ui/mismatch/{row['id']}/resolve",
                    data={"shipment_id": "1",
                          "note": "hangtag swapped at tagging"})
    assert r.status_code == 303

    with cursor() as cur:
        cur.execute("SELECT resolved_at, resolution_note FROM mismatch "
                    "WHERE id = %s", (row["id"],))
        got = cur.fetchone()
    assert got["resolved_at"] is not None
    assert got["resolution_note"] == "hangtag swapped at tagging"


def test_resolving_an_unknown_mismatch_is_404(client):
    r = client.post("/ui/mismatch/999999/resolve",
                    data={"shipment_id": "1", "note": "x"})
    assert r.status_code == 404


# ========================================================= shipment close ===

def test_closing_a_shipment_then_closing_it_again(client):
    sid = _new_shipment(client, 10)
    client.post("/ui/setup/allocation",
                data={"shipment_id": str(sid), "colour_id": "1", "qty_S": "10"})
    assert client.post(f"/ui/shipment/{sid}/close").status_code == 303
    assert client.post(f"/ui/shipment/{sid}/close").status_code == 409
