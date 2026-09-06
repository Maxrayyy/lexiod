"""Exercise real CPU adapters and optionally recognize one corpus page."""

import argparse
import json
from pathlib import Path
import resource
import time

from lexoid.core.recognition.models import PageEvidence, RenderMetadata
from lexoid.core.recognition.paddle import (
    PaddleLayoutAdapter, PaddleTableAdapter, PaddleTextAdapter, PaddleVlFallbackAdapter,
)
from lexoid.core.recognition.rendering import render_pdf_page
from lexoid.core.recognition.vision import VisionLatexAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--vl", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timings = {}
    def checkpoint(stage):
        now = time.monotonic()
        timings[stage] = {"elapsed_seconds": round(now - started, 2),
                          "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)}
        print(stage, timings[stage], flush=True)
        (args.output_dir / "performance.json").write_text(
            json.dumps(timings, indent=2), encoding="utf-8")

    page = render_pdf_page(str(args.source), 1, 240, auto_orient=True)
    page.image.save(args.output_dir / "source.png")
    print("render", page.width, page.height, page.rotation, flush=True)
    checkpoint("render")
    blocks = PaddleTextAdapter().predict_batch([page])[1]
    print("text", len(blocks), flush=True)
    checkpoint("text")
    regions = PaddleLayoutAdapter().predict(page)
    print("layout", [(r.kind, r.bbox) for r in regions], flush=True)
    checkpoint("layout")
    tables = PaddleTableAdapter().predict(page, regions)
    print("tables", [(t.rows, t.columns, len(t.cells)) for t in tables], flush=True)
    checkpoint("tables")
    evidence = PageEvidence("recognition/v1", 1,
        RenderMetadata(page.dpi, page.width, page.height, page.rotation), blocks, tables)
    if args.vl and tables:
        print("vl", PaddleVlFallbackAdapter().predict(page, tables[0]).source, flush=True)
        checkpoint("vl")
    payload = {"schema": "recognition/v1", "pages": [evidence.to_dict()]}
    if args.vision:
        from dataclasses import replace
        from lexoid.core.model_config import resolve_model
        from lexoid.core.recognition.service import document_sha256
        model = resolve_model("LEXOID_MODEL")
        result = VisionLatexAdapter(model).recognize(page, evidence, page_count=1)
        payload.update(document_sha256=document_sha256(args.source), model=model)
        payload["pages"] = [replace(evidence, fields=result.fields).to_dict()]
        (args.output_dir / "sample.tex").write_text(result.latex, encoding="utf-8")
        print("vision", len(result.fields), "fields", flush=True)
        checkpoint("vision")
    (args.output_dir / "sample.recognition.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("seconds", round(time.monotonic() - started, 2), flush=True)


if __name__ == "__main__":
    main()
