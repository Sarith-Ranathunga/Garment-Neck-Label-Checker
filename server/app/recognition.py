"""
Garment Neck Label Checker — recognition and verification.

This module is the research contribution. Everything else in the service is
ordinary software.

Two parts, matching Chapter 4 §4.6:

  PART 1  the recognition model      — EasyOCR, selected on measured evidence
  PART 2  the research algorithm     — four-rotation retry, constrained
                                       vocabulary parse, buyer notation
                                       resolution, three-outcome classification

Episode A measured both engines under identical conditions
(OCR_Test_Recording_Sheet_FILLED_V03.xlsx, 20 garments x 2 conditions x
4 rotations = 160 images, 320 recognition events):

    condition                Tesseract        EasyOCR
    label accessible         88.75% correct   93.75% correct
    through packaging film   86.25% correct   90.00% correct
    mean time                783 / 761 ms     758 / 766 ms

EasyOCR is more accurate in both conditions at equivalent speed, so it is the
engine used here. That choice rests on this study's own data, not on a claim
from the literature.

Correct reads by rotation (Episode A, EasyOCR) are why ROTATIONS exists:

    rotation    accessible   through film
    0 deg       95.0%        95.0%
    90 deg      95.0%        90.0%
    180 deg     80.0%        90.0%
    270 deg     85.0%        70.0%

Orientation, not the film, is the limiting factor: at 0 deg the two conditions
are identical. A worker holding a garment has no fixed orientation, so the
retry is an empirical response to a measured failure mode.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

# Rotations are tried in descending order of measured success so that the
# common case exits the loop first.
ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)


class Outcome(str, Enum):
    """
    Three outcomes, never combined into one accuracy figure (Chapter 3 §3.09).

    A WRONG_READ passes a defective garment while asserting it is correct.
    A NO_READ routes the garment to manual entry. They are not the same
    failure and must not be averaged together.
    """

    CORRECT = "correct"
    WRONG_READ = "wrong_read"
    NO_READ = "no_read"


class Verdict(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NO_READ = "no_read"
    OUT_OF_CONTEXT = "out_of_context"


@dataclass(frozen=True)
class SizeCodeMap:
    """
    Buyer-specific notation resolved to a canonical size.

    Without this, a hangtag reading 2XL against a neck label reading XXL is
    counted as an identification error. That single confusion would put the
    O2 false-positive rate beyond its threshold before the system ever met a
    genuine mistake.
    """

    buyer_code: str
    # buyer notation (upper-cased, stripped) -> canonical size
    notation_to_canonical: dict[str, str] = field(default_factory=dict)

    def resolve(self, token: str | None) -> str | None:
        if token is None:
            return None
        key = _normalise(token)
        return self.notation_to_canonical.get(key)

    def notations_for(self, canonical_sizes: Iterable[str]) -> set[str]:
        """Every buyer notation that maps to one of these canonical sizes."""
        wanted = set(canonical_sizes)
        return {
            notation
            for notation, canonical in self.notation_to_canonical.items()
            if canonical in wanted
        }


@dataclass
class RecognitionResult:
    outcome: Outcome
    canonical_size: str | None
    raw_text: str | None
    rotation_used: int | None
    rotations_tried: int
    confidence: float | None
    duration_ms: int
    engine: str = "easyocr"
    engine_version: str | None = None


def _normalise(token: str) -> str:
    """
    Upper-case, strip, and remove characters that never appear in a size code.

    OCR routinely returns 'L|', 'L .' or 'L~' for a clean L. Normalising here
    rather than in the parse keeps the raw text intact for the record while
    letting the comparison succeed.
    """
    return re.sub(r"[^A-Z0-9]", "", token.upper())


class LabelRecogniser:
    """
    Wraps EasyOCR. Injectable so the algorithm can be tested without the model
    loaded — the constrained parse is the part worth unit-testing, and it does
    not need a neural network to exercise.
    """

    def __init__(self, reader=None, languages: Sequence[str] = ("en",), gpu: bool = False):
        self._reader = reader
        self._languages = list(languages)
        self._gpu = gpu

    def _ensure_reader(self):
        if self._reader is None:
            import easyocr  # imported lazily: loading the models is slow

            self._reader = easyocr.Reader(self._languages, gpu=self._gpu)
        return self._reader

    def read(self, image, rotation: int) -> tuple[str, float | None]:
        """
        Return (text, mean confidence) for one rotation of one image.

        ⚠️ Confidence is captured here. Episode A produced it and discarded it,
        which is why that run could not show whether routing low-confidence
        reads to manual entry converts wrong reads into no reads. It is
        recorded from now on.
        """
        import cv2  # local import keeps this module importable without OpenCV

        if rotation:
            code = {
                90: cv2.ROTATE_90_CLOCKWISE,
                180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE,
            }[rotation]
            image = cv2.rotate(image, code)

        reader = self._ensure_reader()
        detections = reader.readtext(image)
        if not detections:
            return "", None

        text = " ".join(d[1] for d in detections)
        scores = [float(d[2]) for d in detections if d[2] is not None]
        confidence = sum(scores) / len(scores) if scores else None
        return text, confidence


# --------------------------------------------------------------------------
#  PART 2 — the research algorithm
# --------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def find_candidates(
    text: str,
    valid_notations: set[str],
) -> list[str]:
    """
    THE CONSTRAINED-VOCABULARY PARSE.

    Search the recognised text only for notations valid in THIS shipment,
    rather than for any size that exists.

    This is the operational form of the argument carrying the O1 threshold.
    Kralicek et al. (2019) report end-to-end recall of 0.391-0.599 on
    free-form multi-language care instructions but 96.8% on fibre composition,
    where the target belongs to a closed set of five materials. The
    determining factor is vocabulary constraint, not image quality.

    A purchase order is released as several shipments, each with its own size
    composition, so the valid set at any one verification is small and known
    in advance — which is what makes this a constrained task rather than an
    open-text one.

    ⚠️ MATCHING IS WHOLE-TOKEN, NOT SUBSTRING, and that is not a detail.

    A substring search produces wrong reads — the dangerous outcome — in two
    ways that both occur on real labels:

      1.  'XXL' is a substring of 'XXXL'. A 3XL garment would be read as 2XL.
      2.  Single-character sizes appear inside ordinary words. 'S' is inside
          'SRI', 'M' is inside 'MADE', 'L' is inside 'LANKA'. Every label
          reading 'MADE IN SRI LANKA' would match S, M and L at once.

    Splitting on non-alphanumeric characters and comparing whole tokens
    removes both. Returning ALL matching tokens, rather than picking one,
    lets the caller treat ambiguity as a failure instead of a guess.
    """
    tokens = {_normalise(t) for t in _TOKEN_SPLIT.split(text)}
    tokens.discard("")
    if not tokens:
        return []

    return sorted(
        {n for n in valid_notations if _normalise(n) in tokens},
        key=len,
        reverse=True,
    )


def recognise_size(
    image,
    valid_canonical_sizes: Iterable[str],
    size_map: SizeCodeMap,
    recogniser: LabelRecogniser,
    true_size: str | None = None,
) -> RecognitionResult:
    """
    Establish the size on a garment's neck label.

    `true_size` is supplied only during evaluation, where ground truth is
    known. In production it is None and the outcome is CORRECT or NO_READ —
    a wrong read is, by definition, indistinguishable from a correct one at
    the moment it is made. That is precisely what makes it the dangerous
    outcome.
    """
    started = time.perf_counter()
    valid_notations = size_map.notations_for(valid_canonical_sizes)

    raw_text: str | None = None
    confidence: float | None = None
    tried = 0

    for rotation in ROTATIONS:
        tried += 1
        text, conf = recogniser.read(image, rotation)
        if raw_text is None:
            raw_text, confidence = text, conf

        candidates = find_candidates(text, valid_notations)

        # ⚠️ Ambiguity is a failure, not a coin toss.
        #
        # If two valid sizes both appear as whole tokens, the read is not
        # trustworthy — OCR noise fragments words, so 'SRI' can arrive as 'S'
        # alongside a genuine 'L'. Picking one would manufacture a wrong read,
        # which is the outcome the artefact exists to avoid.
        #
        # Instead the next rotation is tried: a different orientation produces
        # different noise and usually resolves the ambiguity. If every
        # rotation is ambiguous the result is NO_READ, and the garment goes to
        # manual entry. That costs a little read rate and buys safety, which
        # is the trade this design makes everywhere.
        if len(candidates) == 1:
            canonical = size_map.resolve(candidates[0])
            elapsed = int((time.perf_counter() - started) * 1000)
            if true_size is None:
                outcome = Outcome.CORRECT
            else:
                outcome = (
                    Outcome.CORRECT
                    if canonical == true_size
                    else Outcome.WRONG_READ
                )
            return RecognitionResult(
                outcome=outcome,
                canonical_size=canonical,
                raw_text=text,
                rotation_used=rotation,
                rotations_tried=tried,
                confidence=conf,
                duration_ms=elapsed,
            )

    # No rotation produced a candidate. This is the SAFE failure: the garment
    # goes to manual entry with the exception logged, and is never blocked.
    return RecognitionResult(
        outcome=Outcome.NO_READ,
        canonical_size=None,
        raw_text=raw_text,
        rotation_used=None,
        rotations_tried=tried,
        confidence=confidence,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def compare(
    neck_canonical: str | None,
    hangtag_notation: str | None,
    size_map: SizeCodeMap,
) -> Verdict:
    """
    Second size segregation: compare the two identifiers.

    BOTH sides are resolved to canonical form before comparison. Comparing the
    raw strings would read every notation difference as an identification
    error.
    """
    if neck_canonical is None:
        return Verdict.NO_READ

    hangtag_canonical = size_map.resolve(hangtag_notation)
    if hangtag_canonical is None:
        # The hangtag carries a notation this buyer has not declared. That is
        # a data problem, not a size mismatch, and must not be reported as one.
        return Verdict.OUT_OF_CONTEXT

    return Verdict.MATCH if neck_canonical == hangtag_canonical else Verdict.MISMATCH


def check_context(
    scanned_style: str | None,
    scanned_colour: str | None,
    checklist_style: str,
    checklist_colour: str,
) -> bool:
    """
    O2 context verification: the scanned garment must belong to the declared
    checklist. Barcode comparison is deterministic, so the threshold is 100%.
    """
    if scanned_style is None or scanned_colour is None:
        return False
    return (
        _normalise(scanned_style) == _normalise(checklist_style)
        and _normalise(scanned_colour) == _normalise(checklist_colour)
    )


def is_bypass(recognition_result_id: int | None) -> bool:
    """
    DP3, measured.

    A manual entry made when recognition DID return a reading is a bypass of
    the control, not a response to its failure. DP3 holds that a compliant
    path slower than its alternative will be bypassed; Charles et al.
    (2015, p. 92) observed inspectors deviating from a prescribed procedure
    for exactly that reason.

    Until now the risk was cited and never measured. This is the measurement,
    and it costs nothing: it is a nullable foreign key.
    """
    return recognition_result_id is None
