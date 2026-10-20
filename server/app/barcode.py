"""
Hangtag barcode resolution.

Two forms are in use at the case company, confirmed from real hangtags on
11 October 2026:

  1.  DELIMITED       'HUEL-BLACK-M'        Code 128. Style, colour and size
                                            are in the string and can be read
                                            straight out of it.

  2.  OPAQUE NUMERIC  '840395619520'        UPC-A. Carries NO size, no style,
                                            no colour. It is only an
                                            identifier, so everything must be
                                            looked up in `barcode_map`.

The artefact supports both, because a packing department does not get to
choose what its buyers print. Resolution tries the string first and falls
back to the lookup.

⚠️ WHY THE TWO FORMS ARE NOT EQUALLY SAFE — this belongs in §4.6.

A delimited barcode that decodes cleanly is self-describing: the size came
off the hangtag itself. An opaque barcode tells you nothing until it is
matched against a table that somebody typed in. If that table is wrong, the
artefact will confidently compare the neck label against the WRONG expected
size and report a mismatch that does not exist, or miss one that does.

So the two paths carry different risk, and the resolution records which one
was used. §5.5 reports them separately rather than pooling them, for the same
reason the three recognition outcomes are never combined.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class BarcodeSource(str, Enum):
    """How the hangtag's attributes were obtained. Reported separately."""

    PARSED = "parsed"       # read out of the barcode string itself
    LOOKUP = "lookup"       # resolved via barcode_map
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class HangtagInfo:
    source: BarcodeSource
    raw: str
    style_no: str | None = None
    colour: str | None = None
    size_notation: str | None = None
    barcode_map_id: int | None = None
    sku: str | None = None


# A delimited hangtag code: at least three parts, separated by - or /.
# Deliberately strict. A loose pattern would "successfully" parse an opaque
# numeric code into nonsense, which is exactly the wrong failure.
_DELIMITED = re.compile(r"^[A-Za-z0-9]+(?:[-/][A-Za-z0-9 ]+){2,}$")
_SPLIT = re.compile(r"[-/]")

# A pure numeric code of EAN/UPC length. These never carry attributes.
_NUMERIC = re.compile(r"^\d{8,14}$")


def normalise(raw: str) -> str:
    return (raw or "").strip().upper()


def looks_opaque(raw: str) -> bool:
    """True when the barcode cannot describe itself and must be looked up."""
    return bool(_NUMERIC.match(normalise(raw)))


def parse_delimited(raw: str) -> HangtagInfo | None:
    """
    Read style, colour and size out of a delimited code such as HUEL-BLACK-M.

    Returns None rather than guessing when the shape is not clearly delimited.
    A wrong parse is worse than no parse: it produces a confident comparison
    against an invented expected size.
    """
    s = normalise(raw)
    if not s or _NUMERIC.match(s) or not _DELIMITED.match(s):
        return None

    parts = [p.strip() for p in _SPLIT.split(s) if p.strip()]
    if len(parts) < 3:
        return None

    # Convention: the LAST part is the size. Style and colour names can
    # themselves contain hyphens; a size code never does.
    style_no = parts[0]
    size_notation = parts[-1]
    colour = " ".join(parts[1:-1])

    return HangtagInfo(
        source=BarcodeSource.PARSED,
        raw=s,
        style_no=style_no,
        colour=colour,
        size_notation=size_notation,
    )


def resolve(raw: str, lookup_row: dict | None = None) -> HangtagInfo:
    """
    Resolve a scanned hangtag barcode.

    `lookup_row` is the `barcode_map` row for this barcode, or None if the
    caller found no match. The caller does the query; this function decides
    what the result means.
    """
    s = normalise(raw)
    if not s:
        return HangtagInfo(source=BarcodeSource.UNRESOLVED, raw=s)

    # 1. Self-describing code
    parsed = parse_delimited(s)
    if parsed is not None:
        # A lookup row, if one exists, still wins for the SKU and the id,
        # because the packing list needs the buyer's SKU and that is never
        # in the barcode.
        if lookup_row:
            return HangtagInfo(
                source=BarcodeSource.PARSED,
                raw=s,
                style_no=parsed.style_no,
                colour=parsed.colour,
                size_notation=parsed.size_notation,
                barcode_map_id=lookup_row.get("id"),
                sku=lookup_row.get("sku"),
            )
        return parsed

    # 2. Opaque code — the table is the only source of truth
    if lookup_row:
        return HangtagInfo(
            source=BarcodeSource.LOOKUP,
            raw=s,
            style_no=lookup_row.get("style_no"),
            colour=lookup_row.get("colour"),
            size_notation=lookup_row.get("size_notation"),
            barcode_map_id=lookup_row.get("id"),
            sku=lookup_row.get("sku"),
        )

    # 3. Neither. An unknown barcode is NOT a mismatch — it is a data gap, and
    #    reporting it as a size error would be a false positive against O2.
    return HangtagInfo(source=BarcodeSource.UNRESOLVED, raw=s)
