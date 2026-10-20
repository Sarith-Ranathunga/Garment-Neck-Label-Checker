"""
Tests for the research algorithm. No OCR model required — the recogniser is
injected, so the constrained parse and the notation resolution can be
exercised on their own.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.recognition import (  # noqa: E402
    Outcome, Verdict, SizeCodeMap, LabelRecogniser,
    find_candidates, recognise_size, compare, check_context, is_bypass,
)

# A buyer who writes XXL where the canonical size is 2XL.
BUYER_A = SizeCodeMap(
    buyer_code="A",
    notation_to_canonical={
        "S": "S", "M": "M", "L": "L", "XL": "XL",
        "XXL": "2XL", "XXXL": "3XL",
    },
)
# A buyer who writes 2XL for the same garment.
BUYER_B = SizeCodeMap(
    buyer_code="B",
    notation_to_canonical={
        "S": "S", "M": "M", "L": "L", "XL": "XL",
        "2XL": "2XL", "3XL": "3XL",
    },
)

SHIPMENT_SIZES = ["S", "M", "L", "XL", "2XL"]


class FakeReader:
    """Returns a scripted text per rotation, as EasyOCR would."""

    def __init__(self, per_rotation):
        self.per_rotation = per_rotation
        self.calls = []

    def read(self, image, rotation):
        self.calls.append(rotation)
        return self.per_rotation.get(rotation, ("", None))


def fake(per_rotation):
    r = LabelRecogniser.__new__(LabelRecogniser)
    r.read = FakeReader(per_rotation).read  # type: ignore[method-assign]
    return r


# ---------------------------------------------------------------- parse ----

def test_longest_notation_wins():
    """'XXL' must not be matched as 'XL'. This is the classic failure."""
    notations = BUYER_A.notations_for(SHIPMENT_SIZES)
    assert find_candidates("BRAND XXL MADE IN SRI LANKA", notations) == ["XXL"]


def test_ocr_noise_is_tolerated():
    """Episode A raw output routinely looked like this."""
    notations = BUYER_A.notations_for(SHIPMENT_SIZES)
    assert find_candidates("BRAND ' L ~ MADE I~ SRI LANKA", notations) == ["L"]
    assert find_candidates("BRAND L | MADE IN SRI LA", notations) == ["L"]


def test_single_letter_sizes_are_not_found_inside_words():
    """
    'MADE IN SRI LANKA' contains S, M and L as substrings. A substring search
    would match all three on every label in the shipment.
    """
    notations = BUYER_A.notations_for(SHIPMENT_SIZES)
    assert find_candidates("BRAND MADE IN SRI LANKA", notations) == []


def test_ambiguous_text_yields_two_candidates_not_a_guess():
    """
    Real Episode A output: 'SRI' truncated to 'S' sits beside a genuine 'L'.
    The parse reports both; recognise_size then tries another rotation rather
    than picking one.
    """
    notations = BUYER_A.notations_for(SHIPMENT_SIZES)
    assert set(find_candidates("BRAND  L  .  MADE  lN  S", notations)) == {"L", "S"}


def test_size_outside_the_shipment_is_not_matched():
    """
    The whole constrained-vocabulary argument. 3XL exists for this buyer but
    is not in this shipment, so it must not be returned.
    """
    notations = BUYER_A.notations_for(SHIPMENT_SIZES)  # no 3XL
    assert find_candidates("BRAND XXXL COTTON", notations) == []


def test_xxl_is_not_matched_inside_xxxl():
    """
    ⭐ A substring search reads a 3XL garment as 2XL — a WRONG READ, the
    dangerous outcome. Whole-token matching prevents it.
    """
    full = SizeCodeMap("A", dict(BUYER_A.notation_to_canonical))
    notations = full.notations_for(["S", "M", "L", "XL", "2XL", "3XL"])
    assert find_candidates("BRAND XXXL COTTON", notations) == ["XXXL"]


def test_empty_text_yields_nothing():
    assert find_candidates("", BUYER_A.notations_for(SHIPMENT_SIZES)) == []


# ------------------------------------------------------ rotation retry -----

def test_first_rotation_short_circuits():
    r = fake({0: ("BRAND L MADE IN SRI LANKA", 0.91)})
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r)
    assert res.outcome is Outcome.CORRECT
    assert res.canonical_size == "L"
    assert res.rotation_used == 0
    assert res.rotations_tried == 1


def test_retry_recovers_a_read_that_zero_degrees_missed():
    """
    Episode A: 270 deg through film fell to 70%. The retry exists because of
    that measurement.
    """
    r = fake({
        0:   ("|||  ~~ ", 0.11),
        90:  ("", None),
        180: ("BRAND XXL PREMIUM", 0.88),
    })
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r)
    assert res.outcome is Outcome.CORRECT
    assert res.canonical_size == "2XL"      # resolved from the buyer's XXL
    assert res.rotation_used == 180
    assert res.rotations_tried == 3


def test_ambiguity_at_every_rotation_gives_no_read():
    """Safe failure in preference to a manufactured wrong read."""
    r = fake({
        0:   ("BRAND L . MADE lN S", 0.55),
        90:  ("BRAND L MADE lN S", 0.52),
        180: ("L  S  BRAND", 0.49),
        270: ("S L", 0.44),
    })
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r)
    assert res.outcome is Outcome.NO_READ
    assert res.rotations_tried == 4


def test_a_later_rotation_resolves_ambiguity():
    """Different orientation, different noise — the retry earns its keep."""
    r = fake({
        0:  ("BRAND L . MADE lN S", 0.55),
        90: ("BRAND L MADE IN SRI LANKA", 0.90),
    })
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r)
    assert res.outcome is Outcome.CORRECT
    assert res.canonical_size == "L"
    assert res.rotation_used == 90


def test_all_rotations_fail_gives_no_read_not_a_guess():
    r = fake({})
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r)
    assert res.outcome is Outcome.NO_READ
    assert res.canonical_size is None
    assert res.rotations_tried == 4


def test_wrong_read_is_distinguished_when_ground_truth_is_known():
    """Evaluation only. In production a wrong read is indistinguishable."""
    r = fake({0: ("BRAND M COTTON", 0.80)})
    res = recognise_size(None, SHIPMENT_SIZES, BUYER_A, r, true_size="L")
    assert res.outcome is Outcome.WRONG_READ
    assert res.canonical_size == "M"


def test_confidence_is_captured():
    """Episode A produced it and discarded it. Not again."""
    r = fake({0: ("BRAND L", 0.93)})
    assert recognise_size(None, SHIPMENT_SIZES, BUYER_A, r).confidence == 0.93


# --------------------------------------------------------- comparison ------

def test_notation_difference_is_not_a_mismatch():
    """
    ⭐ The case that would otherwise wreck the O2 false-positive rate:
    neck label reads XXL, hangtag is encoded 2XL, same garment.
    """
    neck = BUYER_A.resolve("XXL")            # -> 2XL
    assert compare(neck, "XXL", BUYER_A) is Verdict.MATCH


def test_genuine_adjacent_size_error_is_caught():
    """
    All 29 mismatches found in the physical sampling were adjacent-size
    errors. This is the case the artefact exists to detect.
    """
    assert compare("L", "XL", BUYER_B) is Verdict.MISMATCH


def test_no_read_is_not_reported_as_a_mismatch():
    assert compare(None, "L", BUYER_A) is Verdict.NO_READ


def test_undeclared_hangtag_notation_is_not_a_mismatch():
    """A data problem must not be reported as an identification error."""
    assert compare("L", "GROSSE-52", BUYER_A) is Verdict.OUT_OF_CONTEXT


# ------------------------------------------------------------ context ------

def test_out_of_context_scan_is_rejected():
    assert check_context("ST-100", "Navy", "ST-100", "Navy") is True
    assert check_context("ST-200", "Navy", "ST-100", "Navy") is False
    assert check_context(None, "Navy", "ST-100", "Navy") is False


# --------------------------------------------------------------- DP3 -------

def test_bypass_detection():
    assert is_bypass(None) is True      # manual entry with no failed read
    assert is_bypass(4412) is False     # legitimate fallback after a no read


if __name__ == "__main__":
    import traceback
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception:
                failed += 1
                print(f"  FAIL  {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
