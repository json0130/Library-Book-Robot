"""Write EAN-13 barcode PNGs for every book in the catalogue, to test the scanner without real books.

    python tools/make_test_barcodes.py                 # data/test_barcodes/<isbn>-<title>.png
    python tools/make_test_barcodes.py --out /tmp/codes --scale 5

Point the webcam at one on a screen, or print them (100% size, black on white). Also writes
not-an-isbn.png (a valid EAN-13 without a 978/979 prefix, like a library sticker) to check that
the scanner ignores it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python tools/...`

from robot.config import load_config  # noqa: E402
from robot.services.books import Catalog, CatalogError  # noqa: E402
from tests.helpers.ean13 import bitmap, check_digit, png_bytes  # noqa: E402

NOT_AN_ISBN = "590123412345"        # + check digit: an EAN-13 with a Polish GS1 prefix, not a book


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def write(path: Path, digits: str, scale: int) -> None:
    width, height, gray = bitmap(digits, module_px=scale, height=40 * scale)
    path.write_bytes(png_bytes(width, height, gray))


def main(argv=None) -> int:
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path, default=cfg.path("data/test_barcodes"), help="output folder")
    p.add_argument("--scale", type=int, default=3, help="pixels per barcode module (default 3)")
    p.add_argument("--catalog", type=Path, default=cfg.path(cfg.books["catalog_path"]))
    args = p.parse_args(argv)
    try:
        catalog = Catalog.load(args.catalog)
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    for book in catalog.books:
        write(args.out / f"{book.isbn13}-{slug(book.title)}.png", book.isbn13, args.scale)
    write(args.out / "not-an-isbn.png", NOT_AN_ISBN + check_digit(NOT_AN_ISBN), args.scale)
    print(f"Wrote {len(catalog) + 1} barcodes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
