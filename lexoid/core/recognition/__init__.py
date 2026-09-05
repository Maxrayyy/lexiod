"""Hybrid recognition contracts and service boundary."""

from .models import (
    RECOGNITION_SCHEMA,
    BoundingBox,
    FieldEvidence,
    FieldHistory,
    LayoutRegion,
    OcrBlock,
    PageEvidence,
    PageRecognitionResult,
    RecognitionConfig,
    RenderMetadata,
    RenderedPage,
    TableCell,
    TableEvidence,
    VisionPageResult,
)
from .rendering import render_pdf_page, render_pdf_page_from_document

__all__ = [
    "RECOGNITION_SCHEMA",
    "BoundingBox",
    "FieldEvidence",
    "FieldHistory",
    "LayoutRegion",
    "OcrBlock",
    "PageEvidence",
    "PageRecognitionResult",
    "RecognitionConfig",
    "RenderMetadata",
    "RenderedPage",
    "TableCell",
    "TableEvidence",
    "VisionPageResult",
    "render_pdf_page",
    "render_pdf_page_from_document",
]
