"""Score fixed, image-checked text spans and spatially matched handwritten fields."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from pylatexenc.latex2text import LatexNodes2Text, MacroTextSpec, get_default_latex_context_db
from pylatexenc.latexwalker import get_default_latex_context_db as walker_context
from pylatexenc.macrospec import MacroSpec
from rapidfuzz.distance import Levenshtein
from rapidfuzz.fuzz import partial_ratio_alignment
from scipy.optimize import linear_sum_assignment

from run_comparison import VARIANTS, save


def normalized(text):
    text = re.sub("[\u2070\u00b9\u00b2\u00b3\u2074-\u2079]+",
                  lambda m: "^" + unicodedata.normalize("NFKC", m.group()), text)
    text = unicodedata.normalize("NFKC", text).casefold()
    for symbol in ("−", "～", "~", "\u2013", "\u2014"):
        text = text.replace(symbol, "-")
    return re.sub(r"[\s,:;()，。：；（）‘’“”\"']", "", text)


def best_span(expected, actual):
    if not actual:
        return "", len(expected)
    if expected in actual:
        return expected, 0
    alignment = partial_ratio_alignment(expected, actual)
    found = actual[alignment.dest_start:alignment.dest_end]
    return found, Levenshtein.distance(expected, found)


def page_texts(tex):
    if r"\begin{document}" in tex:
        tex = tex.split(r"\begin{document}", 1)[1]
    parts = re.split(r"(?m)^\s*% LEXOID_PAGE_COMPLETED:\s*(\d+)/3\s*$", tex)
    parsing = walker_context()
    parsing.add_context_category("lexiod", macros=[MacroSpec("hwfield", "{{"), MacroSpec("SA", "{")], prepend=True)
    rendering = get_default_latex_context_db()
    rendering.add_context_category("lexiod", macros=[MacroTextSpec("hwfield", simplify_repl="%(2)s"),
                                                    MacroTextSpec("SA", discard=True)], prepend=True)
    converter = LatexNodes2Text(latex_context=rendering)
    return {int(parts[i + 1]): normalized(converter.latex_to_text(parts[i], latex_context=parsing))
            for i in range(0, len(parts) - 1, 2)}


def intersection_over_union(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0


def match_fields(references, pages):
    matches = {}
    for page in pages:
        selected = [r for r in references if r["page"] == page["page"]]
        fields = page["fields"]
        if not selected or not fields:
            continue
        width, height = page["render"]["width"], page["render"]["height"]
        boxes = [[f["bbox"][0] / width, f["bbox"][1] / height,
                  f["bbox"][2] / width, f["bbox"][3] / height] for f in fields]
        costs = [[1 - intersection_over_union(r["region"], b) for b in boxes] for r in selected]
        rows, cols = linear_sum_assignment(costs)
        for row, col in zip(rows, cols):
            overlap = 1 - costs[row][col]
            if overlap >= 0.01:
                matches[selected[row]["id"]] = (fields[col], round(overlap, 4))
    return matches


def aggregate(rows):
    characters = sum(r["characters"] for r in rows)
    edits = sum(r["edits"] for r in rows)
    return {"samples": len(rows), "characters": characters, "edits": edits,
            "character_match_pct": round(100 * max(0, 1 - edits / characters), 2) if characters else None,
            "exact_samples": sum(r["edits"] == 0 for r in rows),
            "exact_pct": round(100 * sum(r["edits"] == 0 for r in rows) / len(rows), 2) if rows else None}


def score(root):
    reference_file = root / "reference" / "transcription.json"
    reference = json.loads(reference_file.read_text())
    all_rows, summary = [], {}
    for variant in VARIANTS:
        directory = root / variant
        evidence = []
        for number in (1, 2, 3):
            path = directory / "tex" / f"page-{number}.recognition.json"
            if path.exists():
                evidence.append(json.loads(path.read_text()))
        matches = match_fields(reference["handwritten"], evidence)
        for stage, tex_path in (("raw", directory / "tex" / "sample.tex"),
                                ("reconciled", directory / "tex" / "sample.reconciled.tex"),
                                ("optimized", directory / "optimized" / "sample.optimized.tex")):
            if not tex_path.exists():
                if stage != "raw":
                    continue
                tex = "\n".join(p.read_text() for p in sorted((directory / "tex").glob("page-*.tex")))
            else:
                tex = tex_path.read_text()
            visible = page_texts(tex)
            if not visible:
                continue
            from texopt.reconcile import field_segments
            try:
                values = {fid: entry["value"] for fid, entry in field_segments(tex).items()}
            except ValueError:
                values = {}
            rows = []
            for category in ("printed", "handwritten"):
                for ref in reference[category]:
                    expected = normalized(ref["text"])
                    detail = {}
                    if category == "printed":
                        found, edits = best_span(expected, visible.get(ref["page"], ""))
                    else:
                        field, overlap = matches.get(ref["id"], ({}, 0))
                        fid = field.get("field_id")
                        found = normalized(values.get(fid, ""))
                        edits = Levenshtein.distance(expected, found)
                        detail = {"field_id": fid, "field_label": field.get("label"), "iou": overlap,
                                  "needs_review": field.get("needs_review"), "zero_sample": expected == "0"}
                    rows.append({"variant": variant, "stage": stage, "category": category,
                        "id": ref["id"], "page": ref["page"], "reference": ref["text"], "matched": found,
                        "characters": len(expected), "edits": edits, **detail})
            summary[f"{variant}.{stage}"] = {
                "pages_present": sorted(visible),
                "printed": aggregate([r for r in rows if r["category"] == "printed"]),
                "handwritten": aggregate([r for r in rows if r["category"] == "handwritten"]),
                "handwritten_nonzero": aggregate([r for r in rows if r["category"] == "handwritten" and not r["zero_sample"]]),
                "unmatched_handwriting_regions": [r["id"] for r in rows if r["category"] == "handwritten" and not r["field_id"]],
            }
            all_rows.extend(rows)
    save(root / "scores.json", {"reference_sha256": hashlib.sha256(reference_file.read_bytes()).hexdigest(),
        "reference_status": reference["reference_status"], "summary": summary, "details": all_rows})
    with (root / "scores.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["variant", "stage", "category", "id", "page",
            "reference", "matched", "characters", "edits", "field_id", "field_label", "iou", "needs_review", "zero_sample"])
        writer.writeheader()
        writer.writerows(all_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path("/data/benchmarks/2026-09-05"))
    score(parser.parse_args().root)
