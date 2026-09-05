"""Single-page PDF rendering for the hybrid recognition pipeline."""

from __future__ import annotations

from typing import Any, Callable

from PIL import Image

from .models import RenderedPage


DocumentFactory = Callable[[str], Any]
OrientationNormalizer = Callable[[Image.Image], Image.Image]


def _default_document_factory(path: str) -> Any:
    import pypdfium2 as pdfium

    return pdfium.PdfDocument(path)


def _default_orientation_normalizer(image: Image.Image) -> Image.Image:
    from lexoid.core.conversion_utils import normalize_document_orientation

    return normalize_document_orientation(image)


def render_pdf_page_from_document(
    document: Any,
    page_index: int,
    dpi: int,
    auto_orient: bool = True,
    orientation_normalizer: OrientationNormalizer | None = None,
) -> RenderedPage:
    """Render one zero-based PDF page from an already-open document."""
    if page_index < 0 or page_index >= len(document):
        raise ValueError(
            f"page_index must be between 0 and {len(document) - 1}, got {page_index}"
        )
    if not 72 <= dpi <= 600:
        raise ValueError(f"dpi must be between 72 and 600, got {dpi}")

    image = document[page_index].render(scale=dpi / 72.0).to_pil().convert("RGB")
    if auto_orient:
        normalizer = orientation_normalizer or _default_orientation_normalizer
        image = normalizer(image).convert("RGB")

    image = image.copy()
    width, height = image.size
    return RenderedPage(
        page=page_index + 1,
        dpi=dpi,
        width=width,
        height=height,
        image=image,
    )


def render_pdf_page(
    path: str,
    page: int,
    dpi: int,
    auto_orient: bool = True,
    document_factory: DocumentFactory | None = None,
    orientation_normalizer: OrientationNormalizer | None = None,
) -> RenderedPage:
    """Render one one-based physical PDF page and close the document."""
    factory = document_factory or _default_document_factory
    document = factory(path)
    try:
        page_count = len(document)
        if page < 1 or page > page_count:
            raise ValueError(f"page must be between 1 and {page_count}, got {page}")
        return render_pdf_page_from_document(
            document,
            page_index=page - 1,
            dpi=dpi,
            auto_orient=auto_orient,
            orientation_normalizer=orientation_normalizer,
        )
    finally:
        document.close()
