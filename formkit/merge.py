"""fields.json + values.json → final.json（面向入库）

设计要点：
  - doc_id 必须一致，否则拒绝合并。把 A 文档的校对结果并到 B 文档上
    是最危险的一类错误，且事后极难发现，所以这里是硬失败而不是警告。
  - 输出保留完整溯源信息（bbox/confidence/source/edited），
    因为「模板不固定」意味着入库后你无法靠 schema 反推数据是怎么来的。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


class MergeError(RuntimeError):
    pass


def merge(fields_doc: Dict[str, Any], values_doc: Dict[str, Any],
          strict: bool = True) -> Dict[str, Any]:
    fid_doc = fields_doc.get("doc_id")
    val_doc = values_doc.get("doc_id")
    if fid_doc != val_doc:
        raise MergeError(
            f"doc_id 不匹配：fields.json={fid_doc!r} values.json={val_doc!r}。"
            "这两份文件不属于同一份 PDF，拒绝合并。"
        )

    values: Dict[str, Any] = values_doc.get("values") or {}
    known = {f["field_id"] for f in fields_doc["fields"]}
    orphans = sorted(set(values) - known)
    if orphans and strict:
        raise MergeError(
            f"values.json 里有 {len(orphans)} 个 field_id 不在 fields.json 中："
            f"{orphans[:5]}… 通常意味着 fields.json 被重新生成过。"
        )

    records: List[Dict[str, Any]] = []
    missing: List[str] = []
    edited_n = confirmed_n = 0

    for f in fields_doc["fields"]:
        fid = f["field_id"]
        v = values.get(fid)
        if v is None:
            missing.append(fid)
            value = f.get("value", "")
            options = f.get("options") or []
            edited = confirmed = False
            note = ""
            source = f.get("source", "vlm")
        else:
            value = v.get("value", "")
            opt_map = v.get("options") or {}
            options = [
                {"label": o["label"], "checked": bool(opt_map.get(o["label"], o.get("checked")))}
                for o in (f.get("options") or [])
            ]
            edited = bool(v.get("edited"))
            confirmed = bool(v.get("confirmed"))
            note = v.get("note") or ""
            source = "human" if edited else f.get("source", "vlm")

        edited_n += int(edited)
        confirmed_n += int(confirmed)

        rec = {
            "doc_id": fid_doc,
            "field_id": fid,
            "page": f["page"],
            "seq": f["seq"],
            "label": f["label"],
            "label_norm": f["label_norm"],
            "type": f["type"],
            "value": value,
            # checkbox_group 的结构化值单独放一列，方便入库时进 JSONB
            "value_json": options if f["type"] == "checkbox_group" else None,
            "bbox": f.get("bbox"),
            "confidence": f.get("confidence", 0.0),
            "is_handwritten": f.get("is_handwritten", False),
            "source": source,
            "edited": edited,
            "confirmed": confirmed,
            "note": note,
        }
        records.append(rec)

    # 便于直接消费的扁平视图：{label: value}，按页分组
    flat: Dict[str, Dict[str, Any]] = {}
    for r in records:
        bucket = flat.setdefault(f"page_{r['page']:02d}", {})
        key = r["label"]
        n = 2
        while key in bucket:
            key = f"{r['label']}_{n}"
            n += 1
        bucket[key] = (
            {o["label"]: o["checked"] for o in r["value_json"]}
            if r["value_json"] is not None else r["value"]
        )

    return {
        "schema_version": fields_doc.get("schema_version"),
        "doc_id": fid_doc,
        "source_pdf": fields_doc.get("source_pdf"),
        "extracted_at": fields_doc.get("created_at"),
        "reviewed_at": values_doc.get("reviewed_at"),
        "reviewer": values_doc.get("reviewer") or "",
        "model": fields_doc.get("model"),
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "fields_total": len(records),
            "fields_edited": edited_n,
            "fields_confirmed": confirmed_n,
            "fields_unreviewed": len(missing),
        },
        "pages": fields_doc.get("pages", []),
        "records": records,
        "flat": flat,
    }


def load(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump(obj: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
