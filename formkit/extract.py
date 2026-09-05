"""PDF → fields.json

两遍策略：
  Pass A (discover)  整页 → 发现所有 label/value/bbox
  Pass B (reread)    对 needs_review 的字段裁图放大 → 专门重读

Pass B 是整条链路的质量核心：Pass A 在整页缩略图上看一个手写小格子，
有效像素只有几十个；Pass B 把同一个格子裁出来放大 3 倍单独问，
识别率有数量级差异。这也是为什么必须要有 bbox —— 没有坐标就没法裁。
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import prompts
from .llm import LLMError, vision_json
from .render import (
    page_to_data_uri,
    render_pages,
    to_data_uri,
    upscale_for_ocr,
    crop_bbox,
)
from .schema import (
    SCHEMA_VERSION,
    FIELD_TYPES,
    Field,
    Option,
    decide_review,
    doc_id_for,
    gemini_box_to_bbox,
    clamp_bbox,
    make_field_id,
    normalize_label,
)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


def _coerce_type(raw: Any) -> str:
    t = str(raw or "text").strip().lower()
    return t if t in FIELD_TYPES else "text"


def _coerce_conf(raw: Any) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if c > 1.0:  # 有的模型返回 0-100
        c = c / 100.0
    return max(0.0, min(1.0, c))


def _parse_bbox(item: Dict[str, Any]) -> Optional[List[float]]:
    """兼容两种坐标写法：Gemini 的 box_2d，以及直接给 bbox 的模型。"""
    if "box_2d" in item:
        b = gemini_box_to_bbox(item.get("box_2d"))
        if b:
            return b
    if "bbox" in item:
        raw = item.get("bbox")
        b = clamp_bbox(raw)
        if b:
            return b
        # 也可能是 0-1000 量纲的 [x0,y0,x1,y1]
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            try:
                scaled = [float(v) / 1000.0 for v in raw]
            except (TypeError, ValueError):
                return None
            return clamp_bbox(scaled)
    return None


def _fields_from_page(page_no: int, payload: Dict[str, Any],
                      threshold: float) -> List[Field]:
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        return []

    taken: Dict[str, int] = {}
    out: List[Field] = []
    for seq, item in enumerate(raw_fields, start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            label = f"未命名字段{seq}"

        options: List[Option] = []
        for o in item.get("options") or []:
            if isinstance(o, dict):
                options.append(
                    Option(label=str(o.get("label") or "").strip(),
                           checked=bool(o.get("checked")))
                )

        f = Field(
            field_id=make_field_id(page_no, label, taken),
            page=page_no,
            seq=seq,
            label=label,
            label_norm=normalize_label(label),
            type=_coerce_type(item.get("type")),
            value=str(item.get("value") or "").strip(),
            options=options,
            bbox=_parse_bbox(item),
            is_handwritten=bool(item.get("is_handwritten")),
            confidence=_coerce_conf(item.get("confidence")),
            source="vlm",
        )
        f.needs_review, f.review_reason = decide_review(f, threshold)
        out.append(f)
    return out


def discover_page(page_img, page_no: int, model: str, api: Optional[str],
                  threshold: float) -> Dict[str, Any]:
    """Pass A：整页字段发现。返回 {page 元信息, fields}。"""
    data_uri = page_to_data_uri(page_img)
    payload = vision_json(
        model=model,
        system=prompts.DISCOVER_SYSTEM,
        user=prompts.DISCOVER_USER,
        image_data_uri=data_uri,
        api=api,
    )
    fields = _fields_from_page(page_no, payload, threshold)
    return {
        "page": page_no,
        "form_code": str(payload.get("form_code") or "").strip(),
        "form_title": str(payload.get("form_title") or "").strip(),
        "width": page_img.width,
        "height": page_img.height,
        "fields": fields,
    }


def reread_field(page_img, f: Field, model: str, api: Optional[str],
                 threshold: float) -> Field:
    """Pass B：裁图放大后单独重读一个字段。

    只在成功且新置信度更高时才覆盖原值 —— 重读失败不应该让结果变得更差。
    """
    if f.bbox is None or f.type == "checkbox_group":
        return f
    crop = upscale_for_ocr(crop_bbox(page_img, f.bbox, pad_ratio=0.18))
    try:
        payload = vision_json(
            model=model,
            system=prompts.REREAD_SYSTEM,
            user=prompts.reread_user(f.label, f.type, f.value),
            image_data_uri=to_data_uri(crop),
            api=api,
            retries=1,
        )
    except LLMError:
        return f  # 重读失败：保留 Pass A 的结果，字段仍然标记为待复核

    new_conf = _coerce_conf(payload.get("confidence"))
    new_value = str(payload.get("value") or "").strip()
    if new_conf <= f.confidence and not (new_value and not f.value):
        return f

    f.value = new_value
    f.confidence = new_conf
    f.source = "vlm_retry"
    alts = [str(a).strip() for a in (payload.get("alternatives") or []) if str(a).strip()]
    note = str(payload.get("note") or "").strip()
    f.needs_review, f.review_reason = decide_review(f, threshold)
    extra = []
    if alts:
        extra.append("其他可能读法：" + " / ".join(alts[:3]))
    if note:
        extra.append(note)
    if extra:
        f.review_reason = "；".join(x for x in [f.review_reason] + extra if x)
    return f


def extract(
    pdf_path: str,
    model: str = DEFAULT_MODEL,
    api: Optional[str] = None,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    reread: bool = True,
    max_workers: int = 4,
    dpi: int = 200,
    progress=lambda msg: None,
) -> Dict[str, Any]:
    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()
    doc_id = doc_id_for(pdf_bytes)

    progress(f"渲染 PDF（{dpi} DPI）…")
    pages = render_pages(pdf_path, dpi=dpi)
    progress(f"共 {len(pages)} 页")

    page_meta: List[Dict[str, Any]] = []
    all_fields: List[Field] = []
    failed_pages: List[Dict[str, str]] = []

    # Pass A：整页并发发现
    with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(discover_page, img, i + 1, model, api, threshold): i + 1
            for i, img in enumerate(pages)
        }
        results: Dict[int, Dict[str, Any]] = {}
        for fut in cf.as_completed(futures):
            pno = futures[fut]
            try:
                results[pno] = fut.result()
                progress(f"  第 {pno} 页发现 {len(results[pno]['fields'])} 个字段")
            except Exception as e:  # noqa: BLE001
                failed_pages.append({"page": pno, "error": str(e)})
                progress(f"  第 {pno} 页失败：{e}")

    for pno in sorted(results):
        r = results[pno]
        page_meta.append({k: r[k] for k in ("page", "form_code", "form_title", "width", "height")})
        all_fields.extend(r["fields"])

    # Pass B：低置信 / 手写字段裁图重读
    if reread:
        todo = [f for f in all_fields if f.needs_review and f.bbox and f.type != "checkbox_group"]
        progress(f"重读 {len(todo)} 个待复核字段…")
        with cf.ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(
                lambda f: reread_field(pages[f.page - 1], f, model, api, threshold), todo
            ))

    review_n = sum(1 for f in all_fields if f.needs_review)
    progress(f"完成：{len(all_fields)} 个字段，其中 {review_n} 个需人工复核")

    return {
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "source_pdf": os.path.basename(pdf_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "dpi": dpi,
        "confidence_threshold": threshold,
        "page_count": len(pages),
        "failed_pages": failed_pages,
        "pages": page_meta,
        "fields": [f.to_json() for f in all_fields],
    }


def write_fields_json(result: Dict[str, Any], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
