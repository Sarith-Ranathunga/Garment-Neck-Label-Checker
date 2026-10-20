"""
The exact request the Android client sends, end to end.

`test_client_api.py` covers the routes the client calls for setup and
bookkeeping. This file covers the one that carries the research: a multipart
POST to `/api/scan` shaped exactly as `ScanRepository.trySend()` builds it,
with the recogniser stubbed so no model has to load.

What is being checked is the *contract between the two halves of the
artefact*. Each test below corresponds to a property Chapter 5 depends on, and
each would fail silently in production if it broke: a verdict attributed to the
wrong outcome, a retry counted as a second garment, a bypass indistinguishable
from a fallback.

Requires a seeded database; skips without one.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402
from app.db import cursor  # noqa: E402
from app.recognition import LabelRecogniser  # noqa: E402

DEVICE = "contract-device"


class StubReader:
    """
    Stands in for EasyOCR.

    Returns a fixed text for every rotation. That is enough: the constrained
    parse, not the neural network, is what these tests are about, and the
    parse is what the research contributes.
    """

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def readtext(self, image, detail=1, **kwargs):  # noqa: ARG002
        self.calls += 1
        return [([[0, 0], [1, 0], [1, 1], [0, 1]], self.text, 0.91)]


@pytest.fixture
def client(monkeypatch):
    try:
        with cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM checklist")
            if cur.fetchone()["n"] == 0:
                pytest.skip("database reachable but not seeded")
    except Exception as exc:                      # pragma: no cover
        pytest.skip(f"no database: {exc}")
    return TestClient(main_mod.app)


def use_label(monkeypatch, text: str) -> StubReader:
    """Point the service's recogniser at a stubbed reader."""
    reader = StubReader(text)
    monkeypatch.setattr(main_mod, "_recogniser", LabelRecogniser(reader=reader))
    return reader


def jpeg(width: int = 320, height: int = 160) -> bytes:
    """A small valid JPEG. The stub ignores the pixels; the service decodes it."""
    blank = np.full((height, width, 3), 255, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", blank)
    assert ok
    return buf.tobytes()


def post_scan(client, payload: dict, image: bytes | None = jpeg()):
    """
    Exactly the request `ScanRepository.trySend()` builds: a `payload` form
    field holding JSON, plus an `image` file part.
    """
    files = {}
    if image is not None:
        files["image"] = ("scan.jpg", io.BytesIO(image), "image/jpeg")
    return client.post(
        "/api/scan",
        data={"payload": json.dumps(payload)},
        files=files or None,
    )


def base_payload(**over) -> dict:
    body = {
        "local_id": str(uuid.uuid4()),
        "device_id": DEVICE,
        "operator_id": 3,
        "checklist_id": 1,
        "carton_id": None,
        "stage": "S1",
        "hangtag_barcode": None,
        "hangtag_size_raw": None,
        "scanned_style": None,
        "scanned_colour": None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    body.update(over)
    return body


# ============================================ the client's own request shape ==

def test_the_clients_multipart_request_is_accepted(client, monkeypatch):
    use_label(monkeypatch, "MADE IN SRI LANKA  M  100% COTTON")
    r = post_scan(client, base_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_size"] == "M"
    assert body["verdict"] == "match"
    assert body["scan_event_id"] > 0


def test_recognition_result_id_is_returned(client, monkeypatch):
    """
    ⭐ The client cannot invent this, so the server has to supply it.

    Without it in the response, a manual entry after a no-read has nothing
    truthful to point at, and the only options left to the client would be to
    guess or to misreport the entry as a bypass. Either would corrupt §5.8.
    """
    use_label(monkeypatch, "L")
    r = post_scan(client, base_payload())
    assert r.status_code == 200
    rid = r.json()["recognition_result_id"]
    assert rid is not None and rid > 0

    with cursor() as cur:
        cur.execute(
            "SELECT scan_event_id FROM recognition_result WHERE id = %s", (rid,))
        row = cur.fetchone()
    assert row is not None
    assert row["scan_event_id"] == r.json()["scan_event_id"]


def test_a_retry_with_the_same_local_id_is_one_garment(client, monkeypatch):
    """
    The whole reason `local_id` is generated once and reused.

    A request that timed out on the phone may well have succeeded on the host.
    If the retry created a second row, one garment would be counted twice and
    O3's reconciliation would show a surplus that never existed.
    """
    use_label(monkeypatch, "XL")
    payload = base_payload()

    first = post_scan(client, payload)
    second = post_scan(client, payload)       # the WorkManager retry
    assert first.status_code == second.status_code == 200
    assert first.json()["scan_event_id"] == second.json()["scan_event_id"]

    with cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM scan_event
                WHERE device_id = %s AND local_id = %s""",
            (payload["device_id"], payload["local_id"]))
        assert cur.fetchone()["n"] == 1


def test_an_oversized_image_is_refused_with_a_usable_message(client, monkeypatch):
    """
    The device crops and compresses; the server states the limit.

    The message has to name the limit, because the only way the client author
    can fix this is by knowing the number.
    """
    use_label(monkeypatch, "M")
    oversized = b"\xff\xd8\xff" + b"\x00" * 1_000_000
    r = post_scan(client, base_payload(), image=oversized)
    assert r.status_code == 413
    assert "900000" in r.text or "crop" in r.text


def test_a_scan_with_no_image_is_refused_not_guessed(client, monkeypatch):
    use_label(monkeypatch, "M")
    r = post_scan(client, base_payload(), image=None)
    assert r.status_code == 400


# =============================================== S1 establishes, S5 verifies ==

def test_s1_establishes_a_size_rather_than_verifying_one(client, monkeypatch):
    """
    At S1 no hangtag exists, so there is nothing to compare against.

    The research keeps "established" and "verified" apart, and so does the
    code: a match at S1 is not evidence about O2, because no comparison
    happened. A system that reported S1 as a verification would inflate its
    own accuracy with scans that were never checked against anything.
    """
    use_label(monkeypatch, "2XL")
    r = post_scan(client, base_payload(stage="S1"))
    assert r.json()["verdict"] == "match"

    with cursor() as cur:
        cur.execute("SELECT hangtag_size_id, hangtag_barcode FROM scan_event "
                    "WHERE id = %s", (r.json()["scan_event_id"],))
        row = cur.fetchone()
    assert row["hangtag_barcode"] is None
    assert row["hangtag_size_id"] is None, "nothing was compared against"


def test_s5_matches_buyer_notation_against_canonical(client, monkeypatch):
    """
    The row that stops a notation difference being read as a defect.

    The hangtag says XXL; the neck label says 2XL. They are the same garment.
    Without `buyer_size_code_map` every such garment is a false positive, and
    2XL is 8,250 units of this shipment.
    """
    use_label(monkeypatch, "2XL  MADE IN SRI LANKA")
    r = post_scan(client, base_payload(
        stage="S5", hangtag_barcode="HUEL-BLACK-XXL",
        scanned_style="HUEL (women's)", scanned_colour="BLACK",
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "match", body
    assert body["canonical_size"] == "2XL"
    assert body["hangtag_size_raw"] == "XXL"
    assert body["hangtag_source"] == "parsed"


def test_s5_detects_a_genuine_mismatch_and_records_it(client, monkeypatch):
    use_label(monkeypatch, "M")
    r = post_scan(client, base_payload(
        stage="S5", hangtag_barcode="HUEL-BLACK-L",
        scanned_style="HUEL (women's)", scanned_colour="BLACK",
    ))
    body = r.json()
    assert body["verdict"] == "mismatch", body
    assert "set this garment aside" in body["message"].lower()

    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM mismatch WHERE scan_event_id = %s",
                    (body["scan_event_id"],))
        assert cur.fetchone()["n"] == 1, "the mismatch must be recorded, not just reported"


def test_the_server_resolves_an_opaque_barcode_the_phone_sent_blind(
    client, monkeypatch
):
    """
    A UPC-A hangtag encodes nothing, so the size must come from the table.

    The phone resolves it from its cached map; the server resolves it again
    rather than trusting the device, because a stale cache would compare
    against an expected size that no longer applies — a false positive, which
    is the exact failure O2 measures.
    """
    # An opaque code on THIS checklist's style. The seed's UPC-A codes belong
    # to the men's style, so one is added here for the women's style: a test
    # that silently relied on a cross-style code would be testing the context
    # check instead of the lookup.
    with cursor(commit=True) as cur:
        cur.execute(
            """INSERT INTO barcode_map
                 (barcode, symbology, buyer_id, style_id, colour_id,
                  size_code_id, sku)
               SELECT '740395610001', 'UPC_A', 1, 1, 1, id, 'TEST-W-BLK-M'
                 FROM size_code WHERE canonical = 'M'
               ON CONFLICT (barcode) DO NOTHING""")

    use_label(monkeypatch, "M")
    r = post_scan(client, base_payload(
        stage="S5", hangtag_barcode="740395610001",
        hangtag_size_raw=None,            # the phone sent nothing
    ))
    body = r.json()
    assert body["hangtag_source"] == "lookup", body
    assert body["hangtag_size_raw"] == "M"
    assert body["verdict"] == "match", body


def test_an_unknown_barcode_is_a_data_gap_not_a_mismatch(client, monkeypatch):
    """
    The distinction that keeps the false-positive rate honest.

    An unknown barcode means the system does not know what this garment should
    be. Calling that a mismatch would count a false positive against O2 and
    send the operator to pull a garment that is very likely correct.
    """
    use_label(monkeypatch, "M")
    r = post_scan(client, base_payload(
        stage="S5", hangtag_barcode="999999999999",
    ))
    body = r.json()
    assert body["hangtag_source"] == "unresolved"
    assert body["verdict"] == "no_read", body
    assert body["verdict"] != "mismatch"
    assert "not in the system" in body["message"].lower()


def test_out_of_context_is_rejected_before_any_ocr_runs(client, monkeypatch):
    """
    The wrong garment at this station is not a size error.

    It is also checked before the expensive step: there is no point running
    recognition on a garment that is not on this checklist, and the reader
    call count proves it did not.
    """
    reader = use_label(monkeypatch, "M")
    r = post_scan(client, base_payload(
        stage="S5", hangtag_barcode="HUEL-BLACK-M",
        scanned_style="SOME OTHER STYLE", scanned_colour="RED",
    ))
    body = r.json()
    assert body["verdict"] == "out_of_context", body
    assert reader.calls == 0, "OCR ran on a garment that was already rejected"


# ============================================== the three recognition outcomes ==

def test_an_unreadable_label_is_a_no_read_never_a_guess(client, monkeypatch):
    """
    The safe failure. A no-read declines to guess; a wrong read is the
    dangerous outcome. The three are never combined (§3.09), and the message
    sends the operator to the fallback rather than to a verdict.
    """
    use_label(monkeypatch, "100% COTTON  WASH AT 30")
    r = post_scan(client, base_payload())
    body = r.json()
    assert body["verdict"] == "no_read"
    assert body["outcome"] == "no_read"
    assert body["canonical_size"] is None


def test_an_ambiguous_label_is_a_no_read_rather_than_a_wrong_read(
    client, monkeypatch
):
    """
    Two candidate sizes on one label must not resolve to either.

    This is the bug the test suite caught during the build: `XXL` is a
    substring of `XXXL`, so a 3XL garment read as 2XL. A wrong read is worse
    than no read, because the operator acts on it.
    """
    use_label(monkeypatch, "M  L")
    r = post_scan(client, base_payload())
    assert r.json()["verdict"] == "no_read"
    assert r.json()["canonical_size"] is None


def test_sizes_are_not_matched_inside_ordinary_words(client, monkeypatch):
    """
    The second bug the suite caught: `S` in SRI, `M` in MADE, `L` in LANKA.

    Every "MADE IN SRI LANKA" label matched three sizes at once. Whole-token
    matching is what fixed it, and this is the label that proves it.
    """
    use_label(monkeypatch, "MADE IN SRI LANKA  OVERSIZE FIT")
    r = post_scan(client, base_payload())
    assert r.json()["canonical_size"] is None, \
        "a size was matched inside a word"


# ================================== the fallback and bypass paths, from here ==

def test_the_fallback_path_records_the_recognition_that_failed(
    client, monkeypatch
):
    """
    O1: no garment can be blocked indefinitely.

    The entry points at the recognition that actually failed, so it is
    distinguishable from a bypass for the rest of the artefact's life.
    """
    use_label(monkeypatch, "100% COTTON")
    scan = post_scan(client, base_payload()).json()
    assert scan["verdict"] == "no_read"

    r = client.post("/api/manual-entry", json={
        "scan_event_id": scan["scan_event_id"],
        "entered_size": "M",
        "recognition_result_id": scan["recognition_result_id"],
        "reason": "print worn away",
    })
    assert r.status_code == 200
    assert r.json()["bypass"] is False

    with cursor() as cur:
        cur.execute(
            """SELECT recognition_result_id FROM exception_event
                WHERE scan_event_id = %s""", (scan["scan_event_id"],))
        assert cur.fetchone()["recognition_result_id"] is not None


def test_a_fallback_and_a_bypass_are_distinguishable_afterwards(
    client, monkeypatch
):
    """
    ⭐ The property the whole §5.8 finding rests on.

    One legitimate fallback and one bypass are recorded, and afterwards the
    database can still tell them apart. If it could not, the bypass rate would
    be unknowable and DP3 would be untestable — the artefact would have lost
    the ability to report on its own weakness.
    """
    use_label(monkeypatch, "WASH SEPARATELY")
    fallback_scan = post_scan(client, base_payload()).json()
    client.post("/api/manual-entry", json={
        "scan_event_id": fallback_scan["scan_event_id"],
        "entered_size": "L",
        "recognition_result_id": fallback_scan["recognition_result_id"],
        "reason": "no read",
    })

    bypass = client.post("/api/bypass", json={
        "local_id": str(uuid.uuid4()), "device_id": DEVICE, "operator_id": 3,
        "checklist_id": 1, "stage": "S1", "entered_size": "L",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "reason": "did not scan",
    }).json()

    with cursor() as cur:
        cur.execute(
            """SELECT scan_event_id, recognition_result_id IS NULL AS is_bypass
                FROM exception_event
                WHERE scan_event_id IN (%s, %s)""",
            (fallback_scan["scan_event_id"], bypass["scan_event_id"]))
        rows = {r["scan_event_id"]: r["is_bypass"] for r in cur.fetchall()}

    assert rows[fallback_scan["scan_event_id"]] is False
    assert rows[bypass["scan_event_id"]] is True


# =========================================================== latency, §5.7 ==

def test_the_end_to_end_figure_needs_all_three_timestamps(client, monkeypatch):
    """
    §5.7 reports capture to screen, not the engine's time.

    `responded_at` is set by the host and `displayed_at` only by the device.
    Until the client reports the third one, the figure does not exist — which
    is why the client sends it in a call of its own.
    """
    use_label(monkeypatch, "S")
    scan = post_scan(client, base_payload()).json()

    with cursor() as cur:
        cur.execute(
            """SELECT captured_at, responded_at, displayed_at
                FROM scan_event WHERE id = %s""", (scan["scan_event_id"],))
        before = cur.fetchone()
    assert before["responded_at"] is not None
    assert before["displayed_at"] is None, "only the device can know this"

    shown = datetime.now(timezone.utc).isoformat()
    r = client.post(f"/api/scan/{scan['scan_event_id']}/displayed",
                    json={"displayed_at": shown})
    assert r.status_code == 200
    assert r.json()["end_to_end_ms"] is not None

    with cursor() as cur:
        cur.execute("SELECT * FROM v_latency WHERE id = %s",
                    (scan["scan_event_id"],))
        assert cur.fetchone() is not None, \
            "the view §5.7 reads must include the completed scan"
