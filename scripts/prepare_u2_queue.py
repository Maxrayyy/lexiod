"""Prepare the approved U2 queue and archive the four incorrect legacy results."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil


RECHECKS = {
    "S22C-726080515560": {
        "pages": 67, "checked_page": 1,
        "source_content": "Batch production record, section 1.3, third-level reactor cell inoculation",
        "old_content": "Inspection report with unrelated material fields and carbon-dioxide test results",
    },
    "S22C-726080516000": {
        "pages": 77, "checked_page": 1,
        "source_content": "Cell supernatant collection processing form",
        "old_content": "Medical-device business license",
    },
    "S22C-726080516040": {
        "pages": 69, "checked_page": 2,
        "source_content": "Cell-count sample handover form and cell-count records",
        "old_content": "Inspection/testing report with unrelated sample information",
    },
    "S22C-726080516090": {
        "pages": 84, "checked_page": 1,
        "source_content": "Batch production record, section 3.4.2, dispensing module",
        "old_content": "Water-quality inspection report and drinking-water sample",
    },
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    root = args.workspace.resolve()
    audit_root = root / "data/audits/2026-09-05"
    audit = json.loads((audit_root / "u2-output-audit.json").read_text())
    missing = [Path(row["source"]).relative_to("Downloads/U2") for row in audit["files"]
               if not row["completed_by_user_policy"]]
    corrected = [Path("批次/A37Z201202605029") / (name + ".pdf") for name in RECHECKS]
    queue = sorted(set(missing + corrected))
    source_root = root / "Downloads/U2"
    source_names = Counter(path.name for path in source_root.rglob("*.pdf")
                           if not path.name.startswith("."))
    for relative in queue:
        if not (source_root / relative).is_file() or source_names[relative.name] != 1:
            raise ValueError(f"Missing or ambiguous source: {relative}")
    backups = []
    archive_root = root / "data/.archive/2026-09-05/u2-orientation"
    for relative in corrected:
        current = root / "data/U2" / relative.parent / "optimized" / (relative.stem + ".optimized.tex")
        archived = archive_root / relative.parent / current.name
        if current.exists() and archived.exists():
            raise FileExistsError(f"Archive already exists: {archived}")
        if not current.exists() and not archived.exists():
            raise FileNotFoundError(current)
        backups.append((current, archived))
    records = []
    for current, archived in backups:
        checksum = sha256(current if current.exists() else archived)
        if current.exists():
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(archived))
        if sha256(archived) != checksum:
            raise RuntimeError(f"Archive verification failed: {archived}")
        records.append({"original": str(current.relative_to(root)),
                        "archived": str(archived.relative_to(root)), "sha256": checksum})
    payload = {
        "source_root": "/input/U2", "output_root": "/data/U2",
        "include": [relative.name for relative in queue],
        "files": [str(relative) for relative in queue],
        "unprocessed_count": len(missing), "rerun_count": len(corrected),
        "total_count": len(queue), "accepted_files_excluded": audit["u2_completed"] - len(corrected),
        "rechecks": RECHECKS, "archived_outputs": records,
        "policy": "Only missing U2 files and the four visually confirmed incorrect legacy conversions; remaining existing outputs accepted by user instruction.",
    }
    (audit_root / "u2-run-queue.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("unprocessed_count", "rerun_count", "total_count", "accepted_files_excluded")}, indent=2))


if __name__ == "__main__":
    main()
