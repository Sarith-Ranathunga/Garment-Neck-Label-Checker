#!/usr/bin/env python3
"""
Episode B — controlled error injection.  ARTIFICIAL · SUMMATIVE · O2

Establishes what the artefact is capable of detecting under controlled
conditions. It does NOT support any claim that packing errors fell in
practice; only Episode C can do that (§3.09 claim discipline).

Two design rules, both from Chapter 3:

  REAL STRUCTURE, SYNTHETIC ERRORS.
  The shipment's sizes, quantities, pieces per carton and remainder cartons
  are copied from a real shipment. An invented shipment would be tidier than
  reality, and neat data flatters any detection system. The mess is what
  causes errors, so the mess is kept and only the errors are synthetic.

  SWEEP THE RATE, DO NOT FIX IT.
  Injecting at one rate makes the result a property of that guess. Rates
  0.5, 1, 2, 3 and 5 per cent are swept so the claim holds regardless of the
  true rate, which is not known in advance.

Reported at every level: detection rate AND false-positive rate. Both, always.
A detector that flags everything has perfect detection and is useless.

⚠️ COMPARISON ALONE IS DETERMINISTIC, SO TESTING IT ALONE PROVES NOTHING.
   If both sizes are known perfectly, the comparison is correct by
   construction and the sweep returns 100% detection at 0% false positives —
   a tautology, not a finding.

   What makes O2 a real question is that the neck-label size is NOT known
   perfectly: it arrives from recognition, which Episode A measured at
   93.75% correct, 2.50% wrong and 3.75% no read (label accessible), and
   90.00 / 3.75 / 6.25 through packaging film. So the harness applies that
   MEASURED outcome distribution to the neck-label read before comparing.
   Episode A supplies the error profile; Episode B measures what the system
   does with it.

   Two runs are therefore reported:

     --ocr-profile none    the comparison logic in isolation. Establishes
                           that the logic itself is sound. Expect 100% / 0%.
     --ocr-profile open    recognition errors at Episode A's measured rates.
     --ocr-profile film    the same, through packaging film.

   And an ablation, --no-size-map, which removes the buyer notation map.
   That is not a hypothetical: without it, every garment whose hangtag and
   neck label use different notation for the SAME size is reported as an
   error.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                                   # noqa: E402
from app.recognition import SizeCodeMap, Verdict, compare         # noqa: E402

INJECTION_RATES = (0.005, 0.01, 0.02, 0.03, 0.05)
DEFAULT_REPLICATES = 20          # per rate, to damp sampling noise

# Episode A, EasyOCR, measured. correct / wrong read / no read.
OCR_PROFILES = {
    "none": None,
    "open": (0.9375, 0.0250, 0.0375),
    "film": (0.9000, 0.0375, 0.0625),
}


@dataclass
class Level:
    rate: float
    garments: int
    injected: int
    detected: int
    missed: int
    false_positives: int
    true_negatives: int
    detection_rate: float | None        # detected / (injected - fallback)
                                        # what the system achieves when it
                                        # actually returns a verdict
    end_to_end_rate: float | None       # detected / injected
                                        # fallback counted as undetected:
                                        # the conservative reading
    false_positive_rate: float
    notation_pairs: int           # how many correct garments used a different
                                  # notation on each side — the hard negatives
    routed_to_fallback: int       # recognition returned no read: the SAFE
                                  # failure. Not a miss — a human checks it
    fallback_on_injected: int     # of those, how many carried a real error
    ocr_induced_fp: int           # false positives caused by a WRONG READ
                                  # rather than by the comparison logic


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def load_structure(shipment_id: int) -> tuple[list[dict], SizeCodeMap, list[str]]:
    """Real shipment structure: per-size quantities, and the buyer's map."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT sc.canonical AS size, sc.sort_order, a.qty
               FROM shipment_allocation a
               JOIN size_code sc ON sc.id = a.size_code_id
               WHERE a.shipment_id = %s AND a.qty > 0
               ORDER BY sc.sort_order""", (shipment_id,))
        alloc = cur.fetchall()
        if not alloc:
            raise SystemExit(f"shipment {shipment_id} has no allocation")

        cur.execute(
            """SELECT b.code, m.buyer_notation, s.canonical
               FROM shipment sh
               JOIN purchase_order p ON p.id = sh.po_id
               JOIN buyer b ON b.id = p.buyer_id
               JOIN buyer_size_code_map m ON m.buyer_id = b.id
               JOIN size_code s ON s.id = m.size_code_id
               WHERE sh.id = %s""", (shipment_id,))
        rows = cur.fetchall()

    code = rows[0]["code"] if rows else "?"
    smap = SizeCodeMap(
        buyer_code=code,
        notation_to_canonical={r["buyer_notation"].upper(): r["canonical"]
                               for r in rows},
    )
    sizes = [r["size"] for r in alloc]
    return alloc, smap, sizes


def adjacent(sizes: list[str], size: str) -> str:
    """
    Pick an ADJACENT size for the injected error.

    Not a random one. All 29 mismatches found in the physical sampling were
    adjacent-size errors — M against L, L against XL — with none spanning two
    or more steps. Injecting random swaps would make the task easier than
    reality and inflate the detection rate.
    """
    i = sizes.index(size)
    options = [j for j in (i - 1, i + 1) if 0 <= j < len(sizes)]
    return sizes[random.choice(options)] if options else size


def alt_notation(smap: SizeCodeMap, canonical: str, exclude: str) -> str | None:
    """Another notation for the same canonical size — e.g. 2XL written XXL."""
    for notation, canon in smap.notation_to_canonical.items():
        if canon == canonical and notation.upper() != exclude.upper():
            return notation
    return None


def apply_ocr(true_size: str, sizes: list[str], profile, rng,
              real=None) -> str | None:
    """
    What the recognition layer returns for this garment.

    Three modes, in descending order of evidential strength:

      real is not None   EasyOCR is run over an ACTUAL photograph of a garment
                         of this size from the Episode A corpus. The errors are
                         observed, not modelled, and their correlation across a
                         single garment's four rotations is preserved.
      profile            Episode A's measured outcome rates are sampled. A
                         wrong read returns an ADJACENT size, because all 29
                         mismatches found in the physical sampling were
                         adjacent — injecting random swaps would make the task
                         easier than reality.
      profile is None    recognition is assumed perfect, which isolates the
                         comparison logic and is a tautology on its own.
    """
    if real is not None:
        return real.read(true_size, rng)
    if profile is None:
        return true_size
    correct, wrong, _no_read = profile
    r = rng.random()
    if r < correct:
        return true_size
    if r < correct + wrong:
        return adjacent(sizes, true_size)
    return None


def run_level(rate: float, alloc: list[dict], smap: SizeCodeMap,
              sizes: list[str], scale: int, rng: random.Random,
              profile=None, use_map: bool = True, real=None) -> Level:
    """One injection level over one synthetic pass of the shipment."""
    population: list[str] = []
    for a in alloc:
        n = max(1, round(int(a["qty"]) / scale))
        population.extend([a["size"]] * n)
    rng.shuffle(population)

    n_inject = max(1, round(len(population) * rate))
    inject_at = set(rng.sample(range(len(population)), n_inject))

    detected = missed = fp = tn = notation_pairs = 0
    fallback = fallback_inj = ocr_fp = 0

    # THE ABLATION.
    #
    # Not "a map with holes in it" — a system that never had the concept.
    # Such a system compares the two strings directly, so a hangtag printed
    # XXL against a neck label resolved to 2XL is simply "not equal" and is
    # reported as an identification error. That is the real cost of omitting
    # the map, and it is what this measures.
    active_map = smap

    def compare_fn(read, notation, m):
        if not use_map:
            if read is None:
                return Verdict.NO_READ
            return (Verdict.MATCH
                    if read.strip().upper() == (notation or "").strip().upper()
                    else Verdict.MISMATCH)
        return compare(read, notation, m)

    for idx, true_size in enumerate(population):
        if idx in inject_at:
            # The hangtag claims an adjacent size. The neck label tells truth
            # — but recognition may not read it correctly.
            wrong = adjacent(sizes, true_size)
            notation = next((k for k, v in smap.notation_to_canonical.items()
                             if v == wrong), wrong)
            read = apply_ocr(true_size, sizes, profile, rng, real)
            if read is None:
                # No read: routed to manual entry. The error is NOT missed by
                # the system — it is handed to a person. Counted separately
                # rather than scored either way, because scoring it would
                # claim something about the human that this episode does not
                # measure.
                fallback += 1
                fallback_inj += 1
                continue
            v = compare_fn(read, notation, active_map)
            if v is Verdict.MISMATCH:
                detected += 1
            else:
                missed += 1
        else:
            # A CORRECT garment. Half of them are deliberately written in a
            # different notation on each side — a hangtag reading XXL against
            # a neck label resolved to 2XL. These are the hard negatives: a
            # system without the size code map flags every one of them.
            notation = next((k for k, v in smap.notation_to_canonical.items()
                             if v == true_size), true_size)
            other = alt_notation(smap, true_size, notation)
            if other and rng.random() < 0.5:
                notation = other
                notation_pairs += 1
            read = apply_ocr(true_size, sizes, profile, rng, real)
            if read is None:
                fallback += 1
                continue
            v = compare_fn(read, notation, active_map)
            if v is Verdict.MISMATCH:
                fp += 1
                if read != true_size:
                    # The garment was correct; the system flagged it because
                    # the LABEL WAS MISREAD. This is the dangerous failure
                    # mode inherited from recognition, and it is the reason
                    # O2 cannot be assessed without an OCR profile.
                    ocr_fp += 1
            else:
                tn += 1

    return Level(
        rate=rate,
        garments=len(population),
        injected=n_inject,
        detected=detected,
        missed=missed,
        false_positives=fp,
        true_negatives=tn,
        detection_rate=(detected / (n_inject - fallback_inj))
                       if (n_inject - fallback_inj) else None,
        end_to_end_rate=(detected / n_inject) if n_inject else None,
        false_positive_rate=(fp / (fp + tn)) if (fp + tn) else 0.0,
        notation_pairs=notation_pairs,
        routed_to_fallback=fallback,
        fallback_on_injected=fallback_inj,
        ocr_induced_fp=ocr_fp,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Episode B — error injection sweep")
    ap.add_argument("shipment_id", type=int)
    ap.add_argument("--scale", type=int, default=50,
                    help="divide real quantities by this to keep runtime sane; "
                         "structure (the ratios) is preserved")
    ap.add_argument("--replicates", type=int, default=DEFAULT_REPLICATES)
    ap.add_argument("--seed", type=int, default=20261011,
                    help="recorded so the run can be reproduced exactly")
    ap.add_argument("--ocr-profile", choices=sorted(OCR_PROFILES) + ["real"],
                    default="open",
                    help="none = comparison logic in isolation; open/film = "
                         "Episode A's measured rates, sampled; real = run "
                         "EasyOCR over the Episode A images themselves")
    ap.add_argument("--images", type=Path,
                    help="directory of Episode A photographs, named "
                         "G01_A_000.jpeg. Required by --ocr-profile real")
    ap.add_argument("--truth", type=Path,
                    help="JSON garment register, G01 -> size. Required by "
                         "--ocr-profile real")
    ap.add_argument("--condition", choices=["A", "B"], default="A",
                    help="A = label directly accessible, B = through "
                         "packaging film. Only with --ocr-profile real")
    ap.add_argument("--gpu", action="store_true",
                    help="let EasyOCR use a GPU if one is present")
    ap.add_argument("--no-size-map", action="store_true",
                    help="ablation: remove the buyer notation map")
    ap.add_argument("--out", default="episode_b_results")
    args = ap.parse_args()
    use_map = not args.no_size_map
    rng = random.Random(args.seed)
    alloc, smap, sizes = load_structure(args.shipment_id)

    real = None
    profile = None
    if args.ocr_profile == "real":
        if not args.images or not args.truth:
            ap.error("--ocr-profile real needs --images and --truth")
        from real_ocr import CONDITION_NAMES, RealRecogniser, load_truth

        real = RealRecogniser(
            image_dir=args.images, truth=load_truth(args.truth),
            valid_sizes=sizes, size_map=smap,
            condition=args.condition, gpu=args.gpu,
        )
        print(f"Recognising the Episode A corpus "
              f"(condition {args.condition} — "
              f"{CONDITION_NAMES[args.condition]})")
        print("Each image is read once and cached; the sweep then samples "
              "those real outcomes with replacement.\n")

        def _tick(i, n, got):
            mark = {"correct": "ok", "wrong_read": "WRONG", "no_read": "no read"}
            print(f"  [{i:3}/{n}] {Path(got.path).name:<18} "
                  f"true={got.true_size:<4} read={str(got.read_size):<5} "
                  f"{mark.get(got.outcome, got.outcome):<7} "
                  f"rot={str(got.rotation_used):<4} {got.duration_ms:5} ms")

        seen = real.warm(progress=_tick)
        c, w, nr = real.measured_profile()
        print(f"\n  corpus: {len(seen)} images — "
              f"correct {c*100:.2f}%  wrong {w*100:.2f}%  no read {nr*100:.2f}%")
        if real.skipped_size:
            print(f"  excluded (size not in this shipment): "
                  f"{', '.join(real.skipped_size)}")
        print()

        # Per-image record, so every number below is traceable to a photograph.
        per_image = Path(args.out).with_name(
            Path(args.out).name + "_images").with_suffix(".csv")
        with open(per_image, "w", newline="") as fh:
            w_ = csv.DictWriter(fh, fieldnames=[
                "path", "garment", "condition", "rotation", "true_size",
                "read_size", "outcome", "rotation_used", "confidence",
                "duration_ms", "raw_text"])
            w_.writeheader()
            for o in sorted(seen, key=lambda x: x.path):
                w_.writerow({
                    "path": Path(o.path).name, "garment": o.garment,
                    "condition": o.condition, "rotation": o.rotation,
                    "true_size": o.true_size, "read_size": o.read_size,
                    "outcome": o.outcome, "rotation_used": o.rotation_used,
                    "confidence": o.confidence, "duration_ms": o.duration_ms,
                    "raw_text": (o.raw_text or "").replace("\n", " ")[:300],
                })
        print(f"  per-image record: {per_image}\n")
    else:
        profile = OCR_PROFILES[args.ocr_profile]

    print(f"Episode B — shipment {args.shipment_id}")
    print(f"  structure: {len(alloc)} sizes, "
          f"{sum(int(a['qty']) for a in alloc):,} garments "
          f"(scaled 1:{args.scale})")
    print(f"  buyer {smap.buyer_code}, "
          f"{len(smap.notation_to_canonical)} notations mapped")
    print(f"  seed {args.seed}, {args.replicates} replicates per level")
    if real is not None:
        c, w, nr = real.measured_profile()
        print(f"  OCR profile: real images, condition {args.condition}"
              f"  (correct {c*100:.2f}% / wrong {w*100:.2f}%"
              f" / no read {nr*100:.2f}%)")
    else:
        print(f"  OCR profile: {args.ocr_profile}"
          + ("" if profile is None else
             f"  (correct {profile[0]*100:.2f}% / wrong {profile[1]*100:.2f}%"
             f" / no read {profile[2]*100:.2f}%)"))
    print(f"  size code map: {'ACTIVE' if use_map else 'ABLATED'}\n")

    rows: list[dict] = []
    print(f"{'rate':>6} {'n':>7} {'inj':>6} {'det':>6} {'miss':>5} "
          f"{'fb':>5} {'FP':>5} {'ocrFP':>6} {'det%':>7} {'e2e%':>7} "
          f"{'FP%':>7} {'O2':>5}")
    print("-" * 86)

    for rate in INJECTION_RATES:
        reps = [run_level(rate, alloc, smap, sizes, args.scale, rng,
                          profile, use_map, real)
                for _ in range(args.replicates)]
        agg = Level(
            rate=rate,
            garments=sum(r.garments for r in reps),
            injected=sum(r.injected for r in reps),
            detected=sum(r.detected for r in reps),
            missed=sum(r.missed for r in reps),
            false_positives=sum(r.false_positives for r in reps),
            true_negatives=sum(r.true_negatives for r in reps),
            detection_rate=None,
            end_to_end_rate=None,
            false_positive_rate=0.0,
            notation_pairs=sum(r.notation_pairs for r in reps),
            routed_to_fallback=sum(r.routed_to_fallback for r in reps),
            fallback_on_injected=sum(r.fallback_on_injected for r in reps),
            ocr_induced_fp=sum(r.ocr_induced_fp for r in reps),
        )
        verdicts_given = agg.injected - agg.fallback_on_injected
        agg.detection_rate = (agg.detected / verdicts_given) if verdicts_given else None
        agg.end_to_end_rate = (agg.detected / agg.injected) if agg.injected else None
        denom = agg.false_positives + agg.true_negatives
        agg.false_positive_rate = agg.false_positives / denom if denom else 0.0

        # O2 thresholds: detection >= 95%, false positives <= 1%
        met = (agg.detection_rate is not None
               and agg.detection_rate >= 0.95
               and agg.false_positive_rate <= 0.01)
        print(f"{rate*100:5.1f}% {agg.garments:7,} {agg.injected:6,} "
              f"{agg.detected:6,} {agg.missed:5,} {agg.routed_to_fallback:5,} "
              f"{agg.false_positives:5,} {agg.ocr_induced_fp:6,} "
              f"{agg.detection_rate*100:6.2f}% {agg.end_to_end_rate*100:6.2f}% "
              f"{agg.false_positive_rate*100:6.2f}% {'PASS' if met else 'FAIL':>5}")
        rows.append(asdict(agg))

    out = Path(args.out)
    with open(out.with_suffix(".csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    with open(out.with_suffix(".json"), "w") as fh:
        json.dump({"shipment_id": args.shipment_id, "seed": args.seed,
                   "scale": args.scale, "replicates": args.replicates,
                   "ocr_profile": args.ocr_profile,
                   "condition": args.condition if args.ocr_profile == "real" else None,
                   "measured_corpus_profile": (
                       list(real.measured_profile()) if real else None),
                   "corpus_images": len(real.cached()) if real else None,
                   "size_code_map": "ablated" if args.no_size_map else "active",
                   "levels": rows}, fh, indent=2)

    print(f"\nwritten {out.with_suffix('.csv')} and {out.with_suffix('.json')}")
    print("\ndet%   = detected / verdicts actually given  (fallback excluded)")
    print("e2e%   = detected / errors injected          (fallback counted as undetected)")
    print("fb     = routed to manual fallback after a no read (the SAFE failure)")
    print("ocrFP  = false positives caused by a WRONG READ, not by the comparison")
    print("\nNOTE: these are CAPABILITY results from synthetic data.")
    print("      No improvement claim may be sourced from them (§3.09).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
