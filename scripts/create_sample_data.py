"""Create a small SYNTHETIC labelled sample workbook for demos and evaluation.

All addresses are invented for testing. Columns:
  AddressA, AddressB        - required inputs
  TrueLabel                 - human judgement: SAME, DIFFERENT or UNKNOWN (cannot tell)
  Note                      - what the case exercises
TrueLabel/Note are ignored by the app (extra columns) and used by scripts/evaluate.py.

Usage: python scripts/create_sample_data.py [output.xlsx]
"""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

ROWS: list[tuple[str | None, str | None, str, str]] = [
    ("12 Main Street, Springfield, IL 62701", "12 main st., springfield il 62701", "SAME", "case/punctuation/abbreviation"),
    ("12 Main St, Springfield, IL 62701", "21 Main St, Springfield, IL 62701", "DIFFERENT", "building number differs"),
    ("12 Main St Apt 4B, Springfield, IL 62701", "12 Main Street Apartment 4B, Springfield IL 62701", "SAME", "unit abbreviation"),
    ("12 Main St Apt 4B, Springfield, IL 62701", "12 Main St Apt 5C, Springfield, IL 62701", "DIFFERENT", "unit differs"),
    ("12 Main St, Springfield, IL 62701", "12 Main St, Springfield, IL 62704", "UNKNOWN", "postal code differs (typo or different place)"),
    ("12 Main St, Springfield, IL", "Main St, Springfield, IL", "UNKNOWN", "partial: building number missing"),
    ("12 Main St", "12 Main Street", "UNKNOWN", "partial: no city or postcode"),
    ("350 5th Ave, New York, NY 10118", "350 Fifth Avenue, New York, NY 10118", "SAME", "ordinal written out"),
    ("350 5th Ave, New York, NY 10118", "350 6th Ave, New York, NY 10118", "DIFFERENT", "numbered street differs"),
    ("12 Main St, Springfield IL 62701", "12 Oak Ave, Springfield IL 62701", "DIFFERENT", "street differs"),
    ("Flat 4B, 12 M.G. Road, Indiranagar, Bengaluru 560038", "Flat 4B, 12 MG Rd, Indiranagar, Bengaluru - 560038", "SAME", "India: abbreviations"),
    ("Flat 4B, 12 M.G. Road, Indiranagar, Bengaluru 560038", "Flat 4B, 12 MG Rd, Indiranagar, Bangalore 560038", "SAME", "India: city alias"),
    ("221B Baker Street, London NW1 6XE, UK", "221B Baker St, London NW16XE", "SAME", "UK postcode spacing"),
    ("Hauptstraße 5, 10115 Berlin", "Hauptstr. 5, 10115 Berlin, Germany", "SAME", "German abbreviation"),
    ("15 rue de la Paix, 75002 Paris", "15 Rue de la Paix 75002 Paris France", "SAME", "French address"),
    ("PO Box 45, Austin TX 78701", "P.O. Box 45, Austin, TX 78701", "SAME", "PO box formatting"),
    ("PO Box 45, Austin TX 78701", "PO Box 54, Austin TX 78701", "DIFFERENT", "PO box differs"),
    (None, "12 Main St, Springfield, IL 62701", "UNKNOWN", "AddressA empty"),
    ("12 Main St, Springfield, IL 62701", "   ", "UNKNOWN", "AddressB whitespace only"),
    ("12 Main Street, Springfield, IL 62701", "12 main st., springfield il 62701", "SAME", "duplicate of row 2"),
    ("=HYPERLINK(\"http://example.com\")", "12 Main St", "UNKNOWN", "formula-like text must stay literal"),
    ("Springfield, IL", "Springfield, Illinois", "UNKNOWN", "city/state only"),
]


def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Addresses")
    ws.append(["AddressA", "AddressB", "TrueLabel", "Note"])
    for row in ROWS:
        cells = []
        for v in row:
            c = WriteOnlyCell(ws, value=v)
            if isinstance(v, str):
                c.data_type = "s"  # keep "=..." as text
            cells.append(c)
        ws.append(cells)
    wb.save(out)
    print(f"Wrote {len(ROWS)} synthetic rows to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/sample/sample_input.xlsx"))
