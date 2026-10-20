#!/usr/bin/env python3
"""
Real recognition for Episode B, over the Episode A image corpus.

Episode B's error injection is synthetic by necessity — a factory cannot be
asked to mislabel garments. What need NOT be synthetic is the recognition
layer. The original harness sampled Episode A's measured outcome distribution
(93.75 / 2.50 / 3.75 open); this module runs **EasyOCR over the actual
photographs instead**, so the recognition errors Episode B reasons about are
observed rather than modelled.

That is a real improvement in evidence, and it is worth being precise about
why. The sampled profile assumes recognition failures are independent draws
from a fixed distribution. They are not: a faded woven label on a light
garment fails in a correlated way across all four of its rotations, and a
crisp printed one succeeds across all four. Sampling the corpus preserves that
structure; sampling a fitted distribution destroys it, and destroys it in the
direction that flatters the system — independent errors average out, and
correlated ones do not.

⚠️ **Each image is recognised at most once, and the result is cached.**
   This is not only a speed measure, though it is that: a 5-rate × 20-replicate
   sweep needs roughly 110,000 reads, which at ~750 ms each is about 23 hours,
   against roughly two minutes for 160 cached images. It is also correct —
   recognition of a given image is deterministic, so a cached result is the
   same result.

   What the sweep then does is **sample the observed corpus with replacement**:
   a bootstrap over real outcomes, rather than draws from a fitted curve.

Ground truth comes from the garment register in the Episode A recording sheet
and is passed in, never inferred from the engine's own output — the whole
point of the register is that the true size was recorded and independently
verified by a second person *before* any recognition was run.
"""

from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recognition import (                                     # noqa: E402
    LabelRecogniser, Outcome, SizeCodeMap, recognise_size,
)

# G07_B_180.jpeg -> garment G07, condition B, rotation 180.
_NAME = re.compile(r"^G(?P<num>\d+)_(?P<condition>[AB])_(?P<rot>\d+)",
                   re.IGNORECASE)


def garment_id(name: str) -> str | None:
    """
    Normalise a garment id from a filename.

    ⚠️ The corpus is not consistently padded: the photographs are named
    `G01_A_000` for some garments and `G3_A_000` for others, while the garment
    register in the recording sheet uses `G01`-`G20` throughout. Matching on
    the raw string silently drops seven of the twenty garments — and it drops
    them from the *middle* of the size range, so the run would still produce a
    plausible-looking number from a corpus missing a third of its garments.

    Padding to two digits is what makes the two agree.
    """
    m = _NAME.match(name)
    return f"G{int(m.group('num')):02d}" if m else None

CONDITION_NAMES = {"A": "label directly accessible",
                   "B": "label through packaging film"}


@dataclass
class ImageOutcome:
    """What the recogniser returned for one photograph."""
    path: str
    garment: str
    condition: str
    rotation: int
    true_size: str
    read_size: str | None
    outcome: str
    raw_text: str | None
    rotation_used: int | None
    confidence: float | None
    duration_ms: int

    @property
    def is_correct(self) -> bool:
        return self.read_size == self.true_size

    @property
    def is_wrong_read(self) -> bool:
        return self.read_size is not None and self.read_size != self.true_size

    @property
    def is_no_read(self) -> bool:
        return self.read_size is None


@dataclass
class RealRecogniser:
    """
    Recognition backed by the Episode A corpus.

    `read(true_size, rng)` returns what the engine actually produced for a
    randomly chosen photograph of a garment whose true size is `true_size`,
    under the configured condition.
    """

    image_dir: Path
    truth: dict[str, str]                 # G01 -> 'L'
    valid_sizes: list[str]
    size_map: SizeCodeMap
    condition: str = "A"
    gpu: bool = False

    _by_size: dict[str, list[Path]] = field(default_factory=dict, init=False)
    _cache: dict[str, ImageOutcome] = field(default_factory=dict, init=False)
    _engine: LabelRecogniser | None = field(default=None, init=False)

    # ------------------------------------------------------------ setup --

    def __post_init__(self) -> None:
        self.condition = self.condition.upper()
        by_size: dict[str, list[Path]] = defaultdict(list)
        skipped_unknown: set[str] = set()
        skipped_size: set[str] = set()

        for path in sorted(self.image_dir.iterdir()):
            if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            m = _NAME.match(path.name)
            if not m:
                continue
            if m.group("condition").upper() != self.condition:
                continue
            garment = garment_id(path.name)
            true_size = self.truth.get(garment)
            if true_size is None:
                skipped_unknown.add(garment)
                continue
            # A garment whose size is not in this shipment cannot stand in for
            # one that is. Dropping it is correct: including it would widen the
            # vocabulary beyond what the shipment actually contains, which is
            # the constraint the whole recognition argument rests on.
            if true_size not in self.valid_sizes:
                skipped_size.add(f"{garment}={true_size}")
                continue
            by_size[true_size].append(path)

        self._by_size = dict(by_size)
        self.skipped_unknown = sorted(skipped_unknown)
        self.skipped_size = sorted(skipped_size)

        missing = [s for s in self.valid_sizes if s not in self._by_size]
        if missing:
            raise SystemExit(
                f"no corpus images for size(s) {', '.join(missing)} under "
                f"condition {self.condition}. Episode B cannot run against real "
                f"images for a shipment containing a size the corpus never "
                f"photographed — that gap is a finding to report, not a "
                f"parameter to work around."
            )

    @property
    def engine(self) -> LabelRecogniser:
        if self._engine is None:
            self._engine = LabelRecogniser(gpu=self.gpu)
        return self._engine

    # ------------------------------------------------------- recognition --

    def outcome_for(self, path: Path) -> ImageOutcome:
        """Recognise one image, once. Cached thereafter."""
        key = str(path)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        import cv2
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise SystemExit(f"could not read image {path}")

        m = _NAME.match(path.name)
        garment = garment_id(path.name)
        rotation = int(m.group("rot"))
        true_size = self.truth[garment]

        result = recognise_size(image, self.valid_sizes, self.size_map,
                                self.engine)

        if result.canonical_size is None:
            outcome = Outcome.NO_READ.value
        elif result.canonical_size == true_size:
            outcome = Outcome.CORRECT.value
        else:
            # ⚠️ The dangerous one. The engine returned a size, confidently,
            #    and it is the wrong size. Nothing downstream can tell.
            outcome = Outcome.WRONG_READ.value

        got = ImageOutcome(
            path=key, garment=garment, condition=self.condition,
            rotation=rotation, true_size=true_size,
            read_size=result.canonical_size, outcome=outcome,
            raw_text=result.raw_text, rotation_used=result.rotation_used,
            confidence=result.confidence, duration_ms=result.duration_ms,
        )
        self._cache[key] = got
        return got

    def warm(self, progress=None) -> list[ImageOutcome]:
        """
        Recognise every image up front.

        Done before the sweep rather than lazily so the cost is paid once and
        visibly, and so the per-image record below can be written even if the
        sweep is interrupted.
        """
        paths = [p for ps in self._by_size.values() for p in ps]
        out: list[ImageOutcome] = []
        for i, path in enumerate(sorted(paths), 1):
            out.append(self.outcome_for(path))
            if progress:
                progress(i, len(paths), out[-1])
        return out

    def read(self, true_size: str, rng: random.Random) -> str | None:
        """
        What the engine produced for a real photograph of this size.

        Sampled with replacement from the corpus, so the empirical outcome
        distribution — including its correlation structure across a single
        garment's four rotations — carries into the sweep.
        """
        choices = self._by_size.get(true_size)
        if not choices:
            return true_size
        return self.outcome_for(rng.choice(choices)).read_size

    # ---------------------------------------------------------- summary --

    def measured_profile(self) -> tuple[float, float, float]:
        """
        correct / wrong / no-read, as actually observed on this corpus.

        Printed beside Episode A's recorded figures so any drift between the
        two is visible rather than assumed away. They should agree; if they do
        not, one of the two runs needs explaining, and that is a finding.
        """
        seen = list(self._cache.values())
        if not seen:
            return (0.0, 0.0, 0.0)
        n = len(seen)
        correct = sum(1 for o in seen if o.is_correct)
        wrong = sum(1 for o in seen if o.is_wrong_read)
        no_read = sum(1 for o in seen if o.is_no_read)
        return (correct / n, wrong / n, no_read / n)

    def cached(self) -> list[ImageOutcome]:
        return list(self._cache.values())


def load_truth(path: Path) -> dict[str, str]:
    """
    Ground truth from the Episode A garment register.

    Accepts either a flat mapping or the richer per-garment objects, so the
    file can carry colour and label type alongside the size without a second
    format.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    truth: dict[str, str] = {}
    for garment, value in raw.items():
        key = str(garment).strip().upper()
        if not re.fullmatch(r"G\d+", key):
            continue                      # header rows and notes
        size = value if isinstance(value, str) else value.get("size")
        if size:
            truth[key] = str(size).strip().upper()
    if not truth:
        raise SystemExit(f"{path} contains no G## -> size entries")
    return truth
