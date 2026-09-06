"""Compile exact benchmark artifacts and render persistent inspection copies."""

import argparse
import json
from pathlib import Path
import subprocess

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from run_comparison import VARIANTS, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/data/benchmarks/2026-09-05"))
    parser.add_argument("--variant", choices=VARIANTS)
    args = parser.parse_args()
    for variant in ((args.variant,) if args.variant else VARIANTS):
        directory = args.root / variant
        results = {}
        for kind, source in (("raw", directory / "tex" / "sample.tex"),
                             ("optimized", directory / "optimized" / "sample.optimized.tex")):
            if not source.exists():
                continue
            preview = directory / "preview" / kind
            preview.mkdir(parents=True, exist_ok=True)
            outputs, codes = [], []
            for _ in range(2):
                result = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                    "-file-line-error", f"-output-directory={preview}", str(source)],
                    cwd=source.parent, capture_output=True, text=True, errors="replace", timeout=180)
                codes.append(result.returncode)
                outputs.append(result.stdout + result.stderr)
                if result.returncode:
                    break
            (preview / "compile.log").write_text("\n".join(outputs), encoding="utf-8")
            results[kind] = {"compile_codes": codes, "ok": codes == [0, 0]}
            pdf_path = preview / source.with_suffix(".pdf").name
            if codes == [0, 0] and pdf_path.exists():
                pdf = pdfium.PdfDocument(str(pdf_path))
                thumbs = []
                try:
                    results[kind]["pdf_pages"] = len(pdf)
                    for number in range(len(pdf)):
                        rendered = pdf[number].render(scale=120 / 72).to_pil().convert("RGB")
                        rendered.save(preview / f"page-{number + 1}.png")
                        rendered.thumbnail((360, 510))
                        thumb = Image.new("RGB", (380, 540), "#d5d5d5")
                        thumb.paste(rendered, ((380 - rendered.width) // 2, 25))
                        ImageDraw.Draw(thumb).text((10, 8), f"{kind} / page {number + 1}", fill="black")
                        thumbs.append(thumb)
                finally:
                    pdf.close()
                columns = min(4, len(thumbs))
                sheet = Image.new("RGB", (380 * columns, 540 * ((len(thumbs) + columns - 1) // columns)), "white")
                for number, thumb in enumerate(thumbs):
                    sheet.paste(thumb, ((number % columns) * 380, (number // columns) * 540))
                sheet.save(preview / "contact.png")
        save(directory / "preview" / "inspection.json", results)
        print(json.dumps({variant: results}), flush=True)


if __name__ == "__main__":
    main()
