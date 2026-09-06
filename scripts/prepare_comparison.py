"""Extract an auditable sample PDF and page images without modifying the corpus."""

import argparse
import hashlib
import json
from pathlib import Path

import pypdfium2 as pdfium

from lexoid.core.recognition.rendering import render_pdf_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--pages", nargs="+", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = pdfium.PdfDocument(str(args.source))
    sample = pdfium.PdfDocument.new()
    try:
        if len(set(args.pages)) != len(args.pages) or any(p < 1 or p > len(source) for p in args.pages):
            parser.error("sample pages must be distinct physical pages in the source")
        sample.import_pages(source, [p - 1 for p in args.pages])
        sample.save(args.output_dir / "sample.pdf")
    finally:
        sample.close()
        source.close()
    for index, original in enumerate(args.pages, 1):
        page = render_pdf_page(str(args.output_dir / "sample.pdf"), index, 240)
        page.image.save(args.output_dir / f"page-{index}.png")
        print({"sample_page": index, "source_page": original, "size": page.image.size}, flush=True)
    manifest = {"source": str(args.source), "source_pages": args.pages,
                "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
                "sample_sha256": hashlib.sha256((args.output_dir / "sample.pdf").read_bytes()).hexdigest(),
                "render_dpi": 240, "reference_status": "pending image check; no human acceptance"}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
