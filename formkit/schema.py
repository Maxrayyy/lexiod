"""数据契约：fields.json / values.json / final.json

设计约束（来自需求）：
  1. 表单模板不固定 —— field_id 必须由内容推导，不能预先枚举。
  2. 最终结果要入库 —— field_id 必须在同一文档的多次重跑之间保持稳定，
     否则数据库里同一份扫描件会产生两套主键。

因此 field_id 的生成刻意**只依赖 (页码, 归一化后的 label, 页内重名序号)**，
不依赖 value、不依赖 bbox、不依赖模型返回的顺序 —— 这三者在重跑时都会抖动。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = "1.0"

# value 里出现这些字样，说明模型写了一句「描述」而不是一个「值」，必须进人工复核。
#
# 注意 handwritten / signature 这类英文词**只在被括号或方括号包起来时**才算占位符：
# 表单正文里合法地出现 "handwritten value" 是完全可能的，
# 裸词匹配会造成大量误报（实测 fixture 已经踩到过）。
ILLEGIBLE_PATTERNS = re.compile(
    r"难以辨认|无法辨认|辨认不清|识别不清|看不清|字迹不清|内容不清|"
    r"手写内容|手写部分|未能识别|"
    r"illegible|unreadable|not\s+legible|cannot\s+(?:be\s+)?read|"
    r"[（(\[【]\s*(?:handwritten|signature|unclear|illegible|blank)[^）)\]】]*[）)\]】]",
    re.IGNORECASE,
)

# bbox 覆盖超过这个面积比例，说明模型没真正定位到字段，只是圈了整页/整块。
# 这种框裁出来等于没裁，必须提醒人工对照原件。
BBOX_MAX_AREA = 0.55

FIELD_TYPES = ("text", "number", "date", "checkbox_group", "signature", "table")


def _strip_punct(s: str) -> str:
    """去掉标点与空白，保留中日韩文字、字母、数字。"""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("Z") or cat.startswith("C"):
            continue
        out.append(ch)
    return "".join(out)


def normalize_label(label: str) -> str:
    """把 OCR 出来的 label 归一化成可用于比对/聚合的键。

    全角转半角、去标点空白、小写化。用于：
      - field_id 的组成部分
      - 数据库里跨文档聚合同名字段（label_norm 列）
    """
    if not label:
        return "unnamed"
    s = unicodedata.normalize("NFKC", label).strip().lower()
    s = _strip_punct(s)
    return s[:48] or "unnamed"


def make_field_id(page: int, label: str, taken: Dict[str, int]) -> str:
    """生成稳定 field_id。

    格式: p{页码两位}.{归一化label}[.{页内重名序号}]
    例:  p01.样品名称        p03.检验项目.2

    `taken` 是调用方持有的「本页已用 slug → 计数」字典，用于处理同页重名。
    重名序号按**页内出现顺序**分配，所以只要 OCR 出的 label 序列稳定，id 就稳定。
    """
    slug = normalize_label(label)
    n = taken.get(slug, 0)
    taken[slug] = n + 1
    suffix = "" if n == 0 else f".{n + 1}"
    return f"p{page:02d}.{slug}{suffix}"


def doc_id_for(pdf_bytes: bytes) -> str:
    """文档主键：源文件字节的 sha256 前 16 位。

    同一份 PDF 无论重跑多少次、换什么模型，doc_id 恒定 ——
    这让「重跑覆盖入库」成为幂等操作。
    """
    return hashlib.sha256(pdf_bytes).hexdigest()[:16]


def clamp_bbox(bbox: Optional[List[float]]) -> Optional[List[float]]:
    """校验并夹紧 bbox 到 [0,1]，顺序 [x0, y0, x1, y1]，原点左上。

    返回 None 表示 bbox 不可用（调用方应回退到整页显示，而不是崩溃）。
    """
    if not bbox or len(bbox) != 4:
        return None
    try:
        vals = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return None
    if any(v != v for v in vals):  # NaN
        return None
    x0, y0, x1, y1 = vals
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(1.0, x1), min(1.0, y1)
    # 退化成线或点的框没有裁图价值
    if x1 - x0 < 1e-4 or y1 - y0 < 1e-4:
        return None
    return [x0, y0, x1, y1]


def bbox_area(bbox: Optional[List[float]]) -> float:
    """归一化 bbox 占整页的面积比例，0..1。bbox 为 None 时返回 0。"""
    if not bbox or len(bbox) != 4:
        return 0.0
    x0, y0, x1, y1 = bbox
    return max(0.0, (x1 - x0)) * max(0.0, (y1 - y0))


def gemini_box_to_bbox(box_2d: List[float]) -> Optional[List[float]]:
    """Gemini 的 box_2d 是 [ymin, xmin, ymax, xmax]，取值 0..1000。

    坐标顺序和量纲都和我们的约定不同，这里集中转换 ——
    这是最容易写错且错了不会报错（只是裁图裁歪）的地方，所以单独成函数并有测试。
    """
    if not box_2d or len(box_2d) != 4:
        return None
    try:
        ymin, xmin, ymax, xmax = (float(v) for v in box_2d)
    except (TypeError, ValueError):
        return None
    return clamp_bbox([xmin / 1000.0, ymin / 1000.0, xmax / 1000.0, ymax / 1000.0])


@dataclass
class Option:
    """复选框组里的单个选项。"""

    label: str
    checked: bool = False


@dataclass
class Field:
    field_id: str
    page: int
    seq: int
    label: str
    label_norm: str
    type: str = "text"
    value: str = ""
    options: List[Option] = field(default_factory=list)
    bbox: Optional[List[float]] = None
    is_handwritten: bool = False
    confidence: float = 0.0
    needs_review: bool = False
    review_reason: str = ""
    source: str = "vlm"  # vlm | vlm_retry | human
    crop_data_uri: str = ""  # 仅在生成 review.html 时填充，不写进 fields.json

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("crop_data_uri", None)
        return d


def decide_review(f: Field, threshold: float) -> Tuple[bool, str]:
    """判定一个字段是否必须进人工复核队列，并给出理由。

    理由会显示在校对界面上，让校对的人知道该重点看什么。
    """
    reasons = []
    if ILLEGIBLE_PATTERNS.search(f.value or ""):
        reasons.append("模型自述无法辨认")
    if f.is_handwritten:
        reasons.append("手写")
    if f.confidence < threshold:
        reasons.append(f"置信度 {f.confidence:.2f} < {threshold:.2f}")
    if f.bbox is None:
        reasons.append("无 bbox，无法定位")
    elif bbox_area(f.bbox) > BBOX_MAX_AREA:
        reasons.append(f"bbox 覆盖 {bbox_area(f.bbox):.0%} 页面，定位不可信")
    if f.type != "checkbox_group" and not (f.value or "").strip():
        reasons.append("值为空")
    return (bool(reasons), "；".join(reasons))
