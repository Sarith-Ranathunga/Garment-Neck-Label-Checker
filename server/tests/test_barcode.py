import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.barcode import (BarcodeSource, resolve, parse_delimited, looks_opaque)

def test_delimited_real_example():
    i = resolve("HUEL-BLACK-M")
    assert i.source is BarcodeSource.PARSED
    assert (i.style_no, i.colour, i.size_notation) == ("HUEL", "BLACK", "M")

def test_delimited_multiword_colour():
    i = resolve("ST1004-NAVY BLUE-XXL")
    assert i.colour == "NAVY BLUE" and i.size_notation == "XXL"

def test_delimited_slash_separator():
    assert resolve("HUEL/BLACK/XXXL").size_notation == "XXXL"

def test_opaque_upca_is_never_parsed():
    """The real UPC-A. Parsing it would invent attributes."""
    assert looks_opaque("840395619520")
    assert parse_delimited("840395619520") is None
    assert resolve("840395619520").source is BarcodeSource.UNRESOLVED

def test_opaque_resolves_through_lookup():
    row = {"id": 7, "style_no": "HUEL", "colour": "BLACK",
           "size_notation": "XXL", "sku": "HU-BLK-XXL"}
    i = resolve("840395619520", row)
    assert i.source is BarcodeSource.LOOKUP
    assert i.size_notation == "XXL" and i.barcode_map_id == 7

def test_parsed_still_takes_sku_from_lookup():
    row = {"id": 9, "sku": "HU-BLK-M"}
    i = resolve("HUEL-BLACK-M", row)
    assert i.source is BarcodeSource.PARSED and i.sku == "HU-BLK-M"

def test_unknown_barcode_is_a_gap_not_a_mismatch():
    i = resolve("999999999999")
    assert i.source is BarcodeSource.UNRESOLVED and i.size_notation is None

def test_two_part_code_is_not_parsed():
    """Too few parts to be sure which is the size."""
    assert parse_delimited("HUEL-M") is None

def test_empty():
    assert resolve("").source is BarcodeSource.UNRESOLVED

if __name__ == "__main__":
    import traceback
    p=f=0
    for n,fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try: fn(); p+=1; print(f"  PASS  {n}")
            except Exception: f+=1; print(f"  FAIL  {n}"); traceback.print_exc()
    print(f"\n{p} passed, {f} failed"); raise SystemExit(1 if f else 0)
