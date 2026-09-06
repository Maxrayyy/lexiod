"""Offline pagination checks on existing recognition and optimization artifacts."""

import json
from pathlib import Path

from PIL import Image, ImageDraw
import pypdfium2 as pdfium

from texopt.cli import _compile_latex
from texopt.page_layout import prepare_layout
from texopt.reconcile import field_segments
from texopt.textio import write_utf8_atomic


def main():
    cases = [
        ("u1-five-pages", Path("/data/U1/OOX/optimized/BP-C3152R_20260806_145323.optimized.tex"),
         Path("/data/U1/OOX/tex/BP-C3152R_20260806_145323.recognition.json")),
        ("vision-three-pages", Path("/data/benchmarks/2026-09-05/vision/optimized/sample.optimized.tex"),
         Path("/data/benchmarks/2026-09-05/vision/tex/sample.recognition.json")),
    ]
    root = Path("/work/pagination-validation")
    results = []
    for name, input_tex, evidence_path in cases:
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        original = input_tex.read_text()
        evidence = json.loads(evidence_path.read_text())
        target = folder / "fixed.tex"
        source, report = prepare_layout(original, evidence)
        before = {key: field["payload"] for key, field in field_segments(original).items()}
        after = {key: field["payload"] for key, field in field_segments(source).items()}
        assert before == after, "Field values changed"
        write_utf8_atomic(target, source)
        ok, log = _compile_latex(target, input_tex.parent, "xelatex", 120, layout_report=report)
        write_utf8_atomic(folder / "compile.log", log)
        report["field_payloads_preserved"] = len(before)
        write_utf8_atomic(folder / "verification.json", json.dumps(report, indent=2))
        document = pdfium.PdfDocument(str(target.with_suffix(".layout.pdf")))
        thumbs = []
        try:
            for n in range(len(document)):
                page = document[n]
                image = page.render(scale=1.4).to_pil().convert("RGB")
                image.save(folder / f"page-{n + 1}.png")
                image.thumbnail((450, 620))
                thumb = Image.new("RGB", (470, 650), "#dedede")
                ImageDraw.Draw(thumb).text((10, 8), f"Output page {n + 1}", fill="black")
                thumb.paste(image, (10, 26))
                thumbs.append(thumb)
                page.close()
        finally:
            document.close()
        contact = Image.new("RGB", (470 * min(3, len(thumbs)), 650 * ((len(thumbs) + 2) // 3)), "#dedede")
        for n, thumb in enumerate(thumbs):
            contact.paste(thumb, (470 * (n % 3), 650 * (n // 3)))
        contact.save(folder / "contact.png")
        results.append({"name": name, "ok": ok, "pages": report["actual_pages"],
                        "field_payloads_preserved": len(before), "errors": report["errors"]})
        print(json.dumps(results[-1]), flush=True)
    write_utf8_atomic(root / "summary.json", json.dumps(results, indent=2))
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
