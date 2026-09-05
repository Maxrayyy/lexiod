"""PDF 渲染与裁图。

不依赖 lexoid —— 只用 pypdfium2 + Pillow，这样 formkit 可以独立于 lexoid 的
重依赖（torch/paddle）运行，也方便单独测试。
"""

from __future__ import annotations

import base64
import io
from typing import List, Optional, Tuple

from PIL import Image

# 渲染 DPI。表单上的手写字较小，150 以下裁出来的小格子会糊到没法看。
DEFAULT_DPI = 200

# 裁图时向外扩的边距（占 bbox 宽高的比例）。
# 模型给的 bbox 经常偏紧或偏移一点，留边可以保证人还是能看清上下文。
CROP_PAD_RATIO = 0.12
CROP_PAD_MIN_PX = 10

# 裁出来的小图放大到至少这么高，方便肉眼辨认手写
CROP_MIN_HEIGHT = 120
CROP_MAX_WIDTH = 900


def render_pages(pdf_path: str, dpi: int = DEFAULT_DPI) -> List[Image.Image]:
    """把 PDF 每页渲染成 PIL Image。"""
    import pypdfium2 as pdfium

    scale = dpi / 72.0
    pages: List[Image.Image] = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for i in range(len(doc)):
            page = doc[i]
            bitmap = page.render(scale=scale)
            pages.append(bitmap.to_pil().convert("RGB"))
    finally:
        doc.close()
    return pages


def crop_bbox(
    img: Image.Image,
    bbox: Optional[List[float]],
    pad_ratio: float = CROP_PAD_RATIO,
) -> Image.Image:
    """按归一化 bbox 裁图并放大。

    bbox 为 None 时返回整页缩略图 —— 刻意不抛异常：
    bbox 拿不到是常态（模型偶尔不给），此时让人看整页总好过整个流程崩掉。
    """
    W, H = img.size
    if bbox is None:
        out = img.copy()
        out.thumbnail((CROP_MAX_WIDTH, 1400))
        return out

    x0, y0, x1, y1 = bbox
    px0, py0, px1, py1 = x0 * W, y0 * H, x1 * W, y1 * H
    pad_x = max(CROP_PAD_MIN_PX, (px1 - px0) * pad_ratio)
    pad_y = max(CROP_PAD_MIN_PX, (py1 - py0) * pad_ratio)

    box = (
        int(max(0, px0 - pad_x)),
        int(max(0, py0 - pad_y)),
        int(min(W, px1 + pad_x)),
        int(min(H, py1 + pad_y)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        out = img.copy()
        out.thumbnail((CROP_MAX_WIDTH, 1400))
        return out

    out = img.crop(box)
    # 太矮的条状裁图放大，否则手写内容看不清
    if out.height < CROP_MIN_HEIGHT:
        factor = min(4.0, CROP_MIN_HEIGHT / max(1, out.height))
        out = out.resize(
            (int(out.width * factor), int(out.height * factor)), Image.LANCZOS
        )
    if out.width > CROP_MAX_WIDTH:
        ratio = CROP_MAX_WIDTH / out.width
        out = out.resize((CROP_MAX_WIDTH, int(out.height * ratio)), Image.LANCZOS)
    return out


def to_png_bytes(img: Image.Image, optimize: bool = True) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


def to_jpeg_bytes(img: Image.Image, quality: int = 78) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_data_uri(img: Image.Image, prefer_jpeg: bool = True) -> str:
    """内嵌进 HTML 用。校对页可能有上百张裁图，用 JPEG 控制体积。"""
    if prefer_jpeg:
        raw, mime = to_jpeg_bytes(img), "image/jpeg"
    else:
        raw, mime = to_png_bytes(img), "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def page_to_data_uri(img: Image.Image, max_dim: int = 1600) -> str:
    """整页图，送给 VLM 用。过大的图既慢又贵，先降到 max_dim。"""
    work = img.copy()
    work.thumbnail((max_dim, max_dim), Image.LANCZOS)
    return to_data_uri(work, prefer_jpeg=True)


def upscale_for_ocr(img: Image.Image, factor: float = 3.0, max_dim: int = 1600) -> Image.Image:
    """手写重识别专用：把小裁图放大后再喂给模型。

    小图直接送过去，模型看到的有效像素太少；放大 3 倍在实践中能明显提升手写识别率。
    """
    w, h = img.size
    nw, nh = int(w * factor), int(h * factor)
    if max(nw, nh) > max_dim:
        ratio = max_dim / max(nw, nh)
        nw, nh = int(nw * ratio), int(nh * ratio)
    if nw <= w:
        return img
    return img.resize((nw, nh), Image.LANCZOS)
