"""Inventory legacy U2 outputs and move optimized TeX to mirrored data paths."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil


def pdfs(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
                  and not any(part.startswith(".") for part in p.relative_to(root).parts))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--move-optimized", action="store_true")
    args = parser.parse_args()
    root = args.workspace.resolve()
    sources = pdfs(root / "Downloads/U2")
    legacy_raw = root / "outputs/02-lexoid-tex"
    legacy_optimized = root / "outputs/04-reviewed-tex"
    report_dir = root / "data/audits/2026-09-05"
    records, moves, claimed = [], [], set()
    for source in sources:
        relative = source.relative_to(root / "Downloads/U2")
        layouts = [relative, Path(*relative.parts[1:])]
        raw = next((legacy_raw / p.with_suffix(".tex") for p in layouts
                    if (legacy_raw / p.with_suffix(".tex")).is_file()), None)
        optimized = next((legacy_optimized / p.with_suffix(".optimized.tex") for p in layouts
                          if (legacy_optimized / p.with_suffix(".optimized.tex")).is_file()), None)
        destination = root / "data/U2" / relative.parent / "optimized" / (relative.stem + ".optimized.tex")
        if optimized:
            if optimized in claimed:
                raise ValueError(f"Ambiguous legacy output: {optimized}")
            claimed.add(optimized)
            if destination.exists():
                raise FileExistsError(f"Refusing to overwrite existing output: {destination}")
            moves.append({"from": str(optimized.relative_to(root)),
                          "to": str(destination.relative_to(root)), "sha256": digest(optimized)})
        records.append({
            "source": str(source.relative_to(root)),
            "group": str(relative.parent),
            "completed_by_user_policy": bool(raw or optimized or destination.is_file()),
            "raw": str(raw.relative_to(root)) if raw else None,
            "legacy_optimized": str(optimized.relative_to(root)) if optimized else None,
            "optimized_destination": str(destination.relative_to(root)),
        })
    unmatched = [str(p.relative_to(root)) for p in legacy_optimized.rglob("*.optimized.tex")
                 if p not in claimed and not any(part.startswith(".") for part in p.relative_to(legacy_optimized).parts)]
    totals = Counter(r["group"] for r in records)
    done = Counter(r["group"] for r in records if r["completed_by_user_policy"])
    u1 = pdfs(root / "Downloads/U1")
    payload = {
        "policy": "Existing corresponding outputs count as completed by user instruction; no content, page-count, or quality revalidation.",
        "u2_total": len(records), "u2_completed": sum(done.values()),
        "u2_missing": len(records) - sum(done.values()),
        "optimized_moves_planned": len(moves), "optimized_moves_completed": 0,
        "unmatched_optimized": unmatched,
        "groups": [{"group": group, "total": total, "completed": done[group],
                    "missing": total - done[group]} for group, total in sorted(totals.items())],
        "u1_total": len(u1),
        "u1_groups": dict(sorted(Counter(str(p.parent.relative_to(root / "Downloads/U1")) for p in u1).items())),
        "files": records, "moves": moves,
    }
    save(report_dir / "u2-output-audit.json", payload)
    if args.move_optimized:
        for move in moves:
            source, destination = root / move["from"], root / move["to"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            if source.exists() or digest(destination) != move["sha256"]:
                raise RuntimeError(f"Move verification failed: {destination}")
            payload["optimized_moves_completed"] += 1
            save(report_dir / "u2-output-audit.json", payload)
    lines = ["# U2 旧输出核对", "", "按用户要求，已有对应产物即视为完成，不重新验收。", "",
             f"U2 共 {len(records)} 个 PDF，已有产物 {sum(done.values())} 个，尚无产物 {len(records) - sum(done.values())} 个。",
             f"已移动优化 TeX：{payload['optimized_moves_completed']} 个。U1 待转换 PDF：{len(u1)} 个。", "",
             "| 源目录 | PDF 数 | 已完成 | 尚无产物 |", "| --- | --- | --- | --- |"]
    lines.extend(f"| {g['group']} | {g['total']} | {g['completed']} | {g['missing']} |" for g in payload["groups"])
    lines += ["", "## 尚无产物的 U2 文件", ""]
    lines.extend(f"- `{r['source']}`" for r in records if not r["completed_by_user_policy"])
    lines += ["", "完整逐文件清单、移动前后路径和 SHA-256 见 `u2-output-audit.json`。", ""]
    (report_dir / "u2-output-audit.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key not in {"files", "moves", "u1_groups"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
