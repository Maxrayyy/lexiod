"""Measure detector input size without changing source render or recognition crops."""

import argparse
import json
from pathlib import Path
import resource
import time

from lexoid.core.paddle_runtime import paddle_runtime_options
from lexoid.core.recognition.paddle import PaddleTextAdapter
from lexoid.core.recognition.rendering import render_pdf_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--detector-max-side", type=int)
    args = parser.parse_args()
    from paddleocr import PaddleOCR

    options = paddle_runtime_options()
    if args.detector_max_side is not None:
        if args.detector_max_side < 960:
            parser.error("detector max side must be at least 960")
        options.update(text_det_limit_side_len=args.detector_max_side, text_det_limit_type="max")
    page = render_pdf_page(str(args.source), args.page, 240)
    started = time.monotonic()
    model = PaddleOCR(**options, use_doc_orientation_classify=False,
                      use_doc_unwarping=False, use_textline_orientation=False)
    initialized = time.monotonic()
    blocks = PaddleTextAdapter(pipeline=model).predict_batch([page])[page.page]
    metrics = {"page": page.page, "render": [page.width, page.height],
               "detector_max_side": args.detector_max_side,
               "init_seconds": round(initialized - started, 2),
               "infer_seconds": round(time.monotonic() - initialized, 2),
               "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
               "blocks": len(blocks)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({**metrics, "ocr_blocks": [b.to_dict() for b in blocks]},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics), flush=True)


if __name__ == "__main__":
    main()
