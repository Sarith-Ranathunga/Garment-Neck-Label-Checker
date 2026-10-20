"""
Tests for the client-facing endpoints.

These run against a real PostgreSQL instance loaded with schema.sql and
seed_demo.sql. They are integration tests on purpose: the things that can go
wrong here are SQL things — a wrong join, a stale column, an enum cast — and a
mocked database would not catch any of them.

Set GNLC_DATABASE_URL before running. The whole module skips if no database is
reachable, so the unit tests in test_recognition.py and test_barcode.py still
run on a machine with nothing installed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import cursor  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    try:
        with cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM checklist")
            if cur.fetchone()["n"] == 0:
                pytest.skip("database reachable but not seeded")
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"no database: {exc}")
    return TestClient(app)


DEVICE = "test-device-01"

# Carton numbers are unique per shipment, so a fixed test number would pass
# once and then fail on a database that already holds it. The suite has to be
# runnable repeatedly against the same instance.
RUN = uuid.uuid4().hex[:6]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ================================================================== basics ==

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Garment Neck Label Checker" in r.text


def test_operators_are_workers_and_supervisors_only(client):
    r = client.get("/api/operators")
    assert r.status_code == 200
    roles = {u["role"] for u in r.json()}
    assert roles <= {"worker", "supervisor"}
    assert "manager" not in roles


def test_login_known_user(client):
    r = client.post("/api/login", json={"username": "w1", "device_id": DEVICE})
    assert r.status_code == 200
    assert r.json()["role"] == "worker"


def test_login_unknown_user_is_404_not_500(client):
    r = client.post("/api/login",
                    json={"username": "nobody", "device_id": DEVICE})
    assert r.status_code == 404


def test_sizes_are_in_packing_list_order(client):
    rows = client.get("/api/sizes").json()
    orders = [r["sort_order"] for r in rows]
    assert orders == sorted(orders)
    assert [r["canonical"] for r in rows][:3] == ["XXS", "XS", "S"]


# ============================================================== checklists ==

def test_checklists_only_open_ones_on_active_shipments(client):
    rows = client.get("/api/checklists").json()
    assert rows, "seed has two open checklists"
    assert all(r["stage"] in ("S1", "S5", "S6") for r in rows)


def test_checklists_stage_filter(client):
    rows = client.get("/api/checklists", params={"stage": "S5"}).json()
    assert all(r["stage"] == "S5" for r in rows)


def test_checklists_rejects_a_bad_stage(client):
    assert client.get("/api/checklists", params={"stage": "S9"}).status_code == 422


# ----------------------------------------------------------------- bundle --

def test_bundle_valid_sizes_come_from_the_allocation(client):
    """
    The closed size set is the constraint that makes recognition tractable.

    If this list ever came from `size_code` instead of `shipment_allocation`,
    the phone would offer sizes that are not in the shipment and the
    recogniser would parse against a wider vocabulary than the one the
    96.8%-vs-39.1% argument in §2.2 depends on.
    """
    b = client.get("/api/checklist/1/bundle").json()
    assert b["valid_sizes"] == ["S", "M", "L", "XL", "2XL"]
    assert "XXS" not in b["valid_sizes"], "not allocated in this shipment"


def test_bundle_size_map_covers_the_buyer_notation(client):
    """XXL -> 2XL is the row whose absence makes every 2XL a false positive."""
    b = client.get("/api/checklist/1/bundle").json()
    assert b["size_map"]["XXL"] == "2XL"
    assert b["size_map"]["XXXL"] == "3XL"
    assert b["size_map"]["2XL"] == "2XL", "canonical spelling also accepted"


def test_bundle_carries_the_barcode_map(client):
    """
    Without this the phone cannot resolve an opaque hangtag offline, and S5
    stops working the moment the WiFi drops.
    """
    b = client.get("/api/checklist/1/bundle").json()
    codes = {e["barcode"]: e for e in b["barcodes"]}
    assert "840395619520" in codes, "the opaque UPC-A form must be included"
    assert codes["840395619520"]["size"] == "M"
    assert "HUEL-BLACK-XXL" in codes
    assert codes["HUEL-BLACK-XXL"]["size"] == "2XL"


def test_bundle_items_fall_back_to_the_allocation(client):
    """An operator must never get an empty size list; packing would stop."""
    b = client.get("/api/checklist/1/bundle").json()
    assert b["items"], "seed has no checklist_item rows, so this is the fallback"
    assert sum(i["expected_qty"] for i in b["items"]) == 56_000


def test_bundle_unknown_checklist_is_404(client):
    assert client.get("/api/checklist/9999/bundle").status_code == 404


def test_progress_unknown_checklist_is_404(client):
    assert client.get("/api/checklist/9999/progress").status_code == 404


# ================================================================= cartons ==

def test_open_carton_is_idempotent(client):
    """
    A double tap on factory WiFi must not produce two cartons, and must not
    produce an error the operator has to interpret.
    """
    body = {"shipment_id": 1, "carton_no": f"T-{RUN}-A", "opened_by": 3}
    first = client.post("/api/carton", json=body)
    second = client.post("/api/carton", json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_close_carton_derives_contents_then_refuses_a_second_close(client):
    body = {"shipment_id": 1, "carton_no": f"T-{RUN}-B", "opened_by": 3}
    cid = client.post("/api/carton", json=body).json()["id"]
    r = client.post(f"/api/carton/{cid}/close",
                    json={"net_weight_kg": 9.8, "gross_weight_kg": 11.0})
    assert r.status_code == 200
    assert r.json()["closed"] is True
    assert r.json()["total"] == 0, "no scans were put in this carton"
    again = client.post(f"/api/carton/{cid}/close", json={})
    assert again.status_code == 409


def test_close_unknown_carton_is_404(client):
    assert client.post("/api/carton/999999/close", json={}).status_code == 404


def test_cartons_listing(client):
    rows = client.get("/api/shipment/1/cartons").json()
    assert any(c["carton_no"] == f"T-{RUN}-A" for c in rows)


# ============================================== the DP3 bypass measurement ==

def test_bypass_writes_a_null_recognition_result_id(client):
    """
    ⭐ The DP3 measurement in §5.8.

    A manual entry with no recognition behind it must be recorded as a NULL
    foreign key. The server writes the NULL itself, so a client cannot make a
    bypass look like a legitimate fallback — which is what would make O4
    unfalsifiable.
    """
    r = client.post("/api/bypass", json={
        "local_id": str(uuid.uuid4()), "device_id": DEVICE, "operator_id": 3,
        "checklist_id": 1, "stage": "S1", "entered_size": "M",
        "captured_at": now(), "reason": "test bypass",
    })
    assert r.status_code == 200
    assert r.json()["bypass"] is True
    scan_id = r.json()["scan_event_id"]

    with cursor() as cur:
        cur.execute(
            """SELECT recognition_result_id FROM exception_event
                WHERE scan_event_id = %s""", (scan_id,))
        assert cur.fetchone()["recognition_result_id"] is None
        cur.execute("SELECT entry_method::text AS m, verdict FROM scan_event "
                    "WHERE id = %s", (scan_id,))
        row = cur.fetchone()
        assert row["m"] == "manual"
        assert row["verdict"] is None, "a bypass produces no verdict"


def test_bypass_appears_in_v_bypass(client):
    """The view §5.8 reads must actually count it."""
    with cursor() as cur:
        cur.execute("SELECT * FROM v_bypass")
        assert cur.fetchall(), "v_bypass returned nothing after a bypass"


def test_bypass_rejects_an_unknown_size(client):
    r = client.post("/api/bypass", json={
        "local_id": str(uuid.uuid4()), "device_id": DEVICE, "operator_id": 3,
        "checklist_id": 1, "stage": "S1", "entered_size": "MEDIUM",
        "captured_at": now(),
    })
    assert r.status_code == 400


def test_bypass_is_idempotent_on_device_and_local_id(client):
    """A WorkManager retry of a bypass must not count twice."""
    lid = str(uuid.uuid4())
    body = {"local_id": lid, "device_id": DEVICE, "operator_id": 3,
            "checklist_id": 1, "stage": "S1", "entered_size": "L",
            "captured_at": now()}
    a = client.post("/api/bypass", json=body).json()
    b = client.post("/api/bypass", json=body).json()
    assert a["scan_event_id"] == b["scan_event_id"]


def test_bypass_rejects_a_bad_stage(client):
    r = client.post("/api/bypass", json={
        "local_id": str(uuid.uuid4()), "device_id": DEVICE, "operator_id": 3,
        "stage": "S4", "entered_size": "M", "captured_at": now()})
    assert r.status_code == 422


# ============================================= the third latency timestamp ==

def test_displayed_at_produces_an_end_to_end_figure(client):
    """
    §5.7 reports capture-to-screen, not engine time. Only the device knows
    when the verdict appeared, so the figure exists only if this route is
    called.
    """
    captured = datetime.now(timezone.utc)
    r = client.post("/api/bypass", json={
        "local_id": str(uuid.uuid4()), "device_id": DEVICE, "operator_id": 3,
        "checklist_id": 1, "stage": "S1", "entered_size": "M",
        "captured_at": captured.isoformat()})
    scan_id = r.json()["scan_event_id"]

    shown = captured + timedelta(milliseconds=1450)
    d = client.post(f"/api/scan/{scan_id}/displayed",
                    json={"displayed_at": shown.isoformat()})
    assert d.status_code == 200
    assert 1400 <= d.json()["end_to_end_ms"] <= 1500


def test_displayed_at_unknown_scan_is_404(client):
    r = client.post("/api/scan/999999999/displayed",
                    json={"displayed_at": now()})
    assert r.status_code == 404


# ============================================================== assistance ==

def test_assistance_is_recorded(client):
    r = client.post("/api/assistance", json={
        "operator_id": 3, "shipment_id": 1, "stage": "S5",
        "cause": "label unreadable", "note": "worn print"})
    assert r.status_code == 200
    assert r.json()["assistance_id"] > 0


# ======================================================= allocation guard ===

def test_allocation_refuses_a_total_that_does_not_match(client):
    """
    O3's one unforgiving validation. A typo here poisons every count
    downstream, so it is refused rather than warned about.
    """
    r = client.post("/api/shipment/1/allocation",
                    json=[{"size": "M", "colour": "BLACK", "qty": 100}])
    assert r.status_code == 422
    assert "56000" in r.text


# ========================================================== reconciliation ==

def test_reconciliation_view_is_queryable(client):
    r = client.get("/api/shipment/1/reconciliation")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_flow_view_is_queryable(client):
    r = client.get("/api/shipment/1/flow")
    assert r.status_code == 200
    assert "by_stage" in r.json() and "reconciliation" in r.json()


# ================================================ server-side barcode guard ==

def test_server_resolves_an_opaque_barcode_itself(client):
    """
    The phone resolves barcodes from its cached map so S5 survives a WiFi
    outage, but the server must not trust that cache: a stale map would
    compare against an expired expected size, and that is a false positive
    counted against O2.
    """
    from app.main import lookup_barcode
    row = lookup_barcode("840395619520")
    assert row is not None
    assert row["canonical"] == "M"
    assert row["size_notation"] == "M"


def test_server_resolves_a_delimited_barcode_without_a_lookup_row(client):
    from app import barcode as bc
    from app.main import lookup_barcode
    tag = bc.resolve("HUEL-BLACK-XXL", lookup_barcode("HUEL-BLACK-XXL"))
    assert tag.source is bc.BarcodeSource.PARSED
    assert tag.size_notation == "XXL"
    assert tag.barcode_map_id is not None, "the SKU still comes from the table"


def test_unknown_barcode_is_a_data_gap_not_a_mismatch(client):
    from app import barcode as bc
    from app.main import lookup_barcode
    tag = bc.resolve("999999999999", lookup_barcode("999999999999"))
    assert tag.source is bc.BarcodeSource.UNRESOLVED
    assert tag.size_notation is None
