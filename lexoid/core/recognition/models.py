"""Provider-independent contracts for hybrid page recognition."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


RECOGNITION_SCHEMA = "recognition/v1"
BoundingBox = tuple[int, int, int, int]
_FIELD_ID = re.compile(r"LEX-P\d{4}-(?:V|C)\d{4}\Z")
_GPU_DEVICE = re.compile(r"gpu(?::\d+)?\Z")


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _score(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} score must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} score must be between 0 and 1")
    return result


def _bbox(value: object, name: str) -> BoundingBox:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} bbox must contain four integers")
    if len(value) != 4:
        raise ValueError(f"{name} bbox must contain four integers")
    coordinates = tuple(_non_negative_int(item, f"{name} bbox") for item in value)
    x0, y0, x1, y1 = coordinates
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"{name} bbox must have positive width and height")
    return coordinates  # type: ignore[return-value]


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _items(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be an array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _inside_render(box: BoundingBox, render: "RenderMetadata") -> bool:
    return box[2] <= render.width and box[3] <= render.height


@dataclass(frozen=True)
class RecognitionConfig:
    """Configuration shared by recognition adapters and orchestration."""

    ocr: str = "paddleocr"
    device: str = "cpu"
    initial_render_dpi: int = 240
    retry_crop_dpi: int = 480
    render_workers: int = 2
    paddle_batch_size: int = 4
    vision_concurrency: int = 4
    max_page_attempts: int = 2
    min_output_tokens: int = 2048
    max_output_tokens: int = 8192
    paddle_low_score: float = 0.70
    enable_vl_fallback: bool = True

    def __post_init__(self) -> None:
        if self.ocr not in {"none", "paddleocr"}:
            raise ValueError(f"Unsupported OCR mode: {self.ocr}")
        if self.device != "cpu" and not _GPU_DEVICE.fullmatch(self.device):
            raise ValueError(f"Unsupported OCR device: {self.device}")
        if not 72 <= self.initial_render_dpi <= 600:
            raise ValueError("initial_render_dpi must be between 72 and 600")
        if not self.initial_render_dpi <= self.retry_crop_dpi <= 600:
            raise ValueError(
                "retry_crop_dpi must be between initial_render_dpi and 600"
            )
        for name in (
            "render_workers",
            "paddle_batch_size",
            "vision_concurrency",
            "min_output_tokens",
            "max_output_tokens",
        ):
            _positive_int(getattr(self, name), name)
        if self.min_output_tokens > self.max_output_tokens:
            raise ValueError("min_output_tokens must not exceed max_output_tokens")
        _score(self.paddle_low_score, "paddle_low")
        if type(self.max_page_attempts) is not int or self.max_page_attempts not in (1, 2):
            raise ValueError("max_page_attempts must be 1 or 2")


@dataclass(frozen=True)
class RenderMetadata:
    """Dimensions of the page image used to produce recognition evidence."""

    dpi: int
    width: int
    height: int
    rotation: int = 0

    def __post_init__(self) -> None:
        _positive_int(self.dpi, "dpi")
        _positive_int(self.width, "width")
        _positive_int(self.height, "height")
        if type(self.rotation) is not int or self.rotation not in (0, 90, 180, 270):
            raise ValueError("render rotation must be 0, 90, 180, or 270")

    def to_dict(self) -> dict[str, int]:
        result = {"dpi": self.dpi, "width": self.width, "height": self.height}
        if self.rotation:
            result["rotation"] = self.rotation
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RenderMetadata":
        return cls(
            dpi=_positive_int(payload.get("dpi"), "render dpi"),
            width=_positive_int(payload.get("width"), "render width"),
            height=_positive_int(payload.get("height"), "render height"),
            rotation=payload.get("rotation", 0),
        )


@dataclass(frozen=True)
class RenderedPage:
    """One rendered physical PDF page and its in-memory image."""

    page: int
    dpi: int
    width: int
    height: int
    image: Any
    rotation: int = 0

    def __post_init__(self) -> None:
        _positive_int(self.page, "page")
        _positive_int(self.dpi, "dpi")
        _positive_int(self.width, "width")
        _positive_int(self.height, "height")
        if type(self.rotation) is not int or self.rotation not in (0, 90, 180, 270):
            raise ValueError("page rotation must be 0, 90, 180, or 270")
        if getattr(self.image, "size", None) != (self.width, self.height):
            raise ValueError("image dimensions do not match width and height")


@dataclass(frozen=True)
class OcrBlock:
    text: str
    bbox: BoundingBox
    score: float

    def __post_init__(self) -> None:
        _text(self.text, "OCR text")
        object.__setattr__(self, "bbox", _bbox(self.bbox, "OCR block"))
        object.__setattr__(self, "score", _score(self.score, "OCR block"))

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "bbox": list(self.bbox), "score": self.score}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "OcrBlock":
        return cls(
            text=_text(payload.get("text"), "OCR text"),
            bbox=_bbox(payload.get("bbox"), "OCR block"),
            score=_score(payload.get("score"), "OCR block"),
        )


@dataclass(frozen=True)
class LayoutRegion:
    kind: str
    bbox: BoundingBox
    score: float

    def __post_init__(self) -> None:
        if not _text(self.kind, "layout kind").strip():
            raise ValueError("layout kind must not be empty")
        object.__setattr__(self, "bbox", _bbox(self.bbox, "layout region"))
        object.__setattr__(self, "score", _score(self.score, "layout region"))


@dataclass(frozen=True)
class TableCell:
    row: int
    column: int
    rowspan: int
    colspan: int
    text: str
    bbox: BoundingBox
    score: float

    def __post_init__(self) -> None:
        _non_negative_int(self.row, "cell row")
        _non_negative_int(self.column, "cell column")
        _positive_int(self.rowspan, "cell rowspan")
        _positive_int(self.colspan, "cell colspan")
        _text(self.text, "cell text")
        object.__setattr__(self, "bbox", _bbox(self.bbox, "table cell"))
        object.__setattr__(self, "score", _score(self.score, "table cell"))

    def to_dict(self) -> dict[str, object]:
        return {
            "row": self.row,
            "column": self.column,
            "rowspan": self.rowspan,
            "colspan": self.colspan,
            "text": self.text,
            "bbox": list(self.bbox),
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TableCell":
        return cls(
            row=_non_negative_int(payload.get("row"), "cell row"),
            column=_non_negative_int(payload.get("column"), "cell column"),
            rowspan=_positive_int(payload.get("rowspan"), "cell rowspan"),
            colspan=_positive_int(payload.get("colspan"), "cell colspan"),
            text=_text(payload.get("text"), "cell text"),
            bbox=_bbox(payload.get("bbox"), "table cell"),
            score=_score(payload.get("score"), "table cell"),
        )


@dataclass(frozen=True)
class TableEvidence:
    page: int
    bbox: BoundingBox
    rows: int
    columns: int
    cells: tuple[TableCell, ...]
    source: str
    fallback_reason: str | None = None
    original_table: TableEvidence | None = None

    def __post_init__(self) -> None:
        _positive_int(self.page, "table page")
        object.__setattr__(self, "bbox", _bbox(self.bbox, "table"))
        _positive_int(self.rows, "table rows")
        _positive_int(self.columns, "table columns")
        object.__setattr__(self, "cells", tuple(self.cells))
        if not _text(self.source, "table source").strip():
            raise ValueError("table source must not be empty")
        _optional_text(self.fallback_reason, "table fallback_reason")
        if self.original_table is not None:
            if (not isinstance(self.original_table, TableEvidence)
                    or self.original_table.page != self.page
                    or self.original_table.bbox != self.bbox
                    or self.original_table.original_table is not None):
                raise ValueError("Original table must describe the same region without nesting")
        for cell in self.cells:
            if cell.row + cell.rowspan > self.rows:
                raise ValueError("table cell row span exceeds table dimensions")
            if cell.column + cell.colspan > self.columns:
                raise ValueError("table cell column span exceeds table dimensions")

    def to_dict(self) -> dict[str, object]:
        result = {
            "page": self.page,
            "bbox": list(self.bbox),
            "rows": self.rows,
            "columns": self.columns,
            "cells": [cell.to_dict() for cell in self.cells],
            "source": self.source,
            "fallback_reason": self.fallback_reason,
        }
        if self.original_table is not None:
            result["original_table"] = self.original_table.to_dict()
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TableEvidence":
        return cls(
            page=_positive_int(payload.get("page"), "table page"),
            bbox=_bbox(payload.get("bbox"), "table"),
            rows=_positive_int(payload.get("rows"), "table rows"),
            columns=_positive_int(payload.get("columns"), "table columns"),
            cells=tuple(
                TableCell.from_dict(_mapping(item, "table cell"))
                for item in _items(payload.get("cells"), "table cells")
            ),
            source=_text(payload.get("source"), "table source"),
            fallback_reason=_optional_text(
                payload.get("fallback_reason"), "table fallback_reason"
            ),
            original_table=cls.from_dict(_mapping(payload["original_table"], "original table"))
            if payload.get("original_table") is not None else None,
        )


@dataclass(frozen=True)
class FieldHistory:
    stage: str
    from_value: str
    to_value: str
    reason: str

    def __post_init__(self) -> None:
        for name, value in (
            ("stage", self.stage),
            ("from", self.from_value),
            ("to", self.to_value),
            ("reason", self.reason),
        ):
            _text(value, f"field history {name}")

    def to_dict(self) -> dict[str, str]:
        return {
            "stage": self.stage,
            "from": self.from_value,
            "to": self.to_value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FieldHistory":
        return cls(
            stage=_text(payload.get("stage"), "field history stage"),
            from_value=_text(payload.get("from"), "field history from"),
            to_value=_text(payload.get("to"), "field history to"),
            reason=_text(payload.get("reason"), "field history reason"),
        )


@dataclass(frozen=True)
class FieldEvidence:
    field_id: str
    label: str
    bbox: BoundingBox
    value: str
    paddle_text: str
    model_guess: str
    confidence: float
    needs_review: bool
    history: tuple[FieldHistory, ...] = ()

    def __post_init__(self) -> None:
        if not _FIELD_ID.fullmatch(self.field_id):
            raise ValueError(f"Invalid field_id: {self.field_id}")
        for name, value in (
            ("label", self.label),
            ("value", self.value),
            ("paddle_text", self.paddle_text),
            ("model_guess", self.model_guess),
        ):
            _text(value, f"field {name}")
        object.__setattr__(self, "bbox", _bbox(self.bbox, f"field {self.field_id}"))
        object.__setattr__(
            self, "confidence", _score(self.confidence, f"field {self.field_id}")
        )
        _bool(self.needs_review, f"field {self.field_id} needs_review")
        object.__setattr__(self, "history", tuple(self.history))

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "bbox": list(self.bbox),
            "value": self.value,
            "paddle_text": self.paddle_text,
            "model_guess": self.model_guess,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "history": [entry.to_dict() for entry in self.history],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FieldEvidence":
        field_id = _text(payload.get("field_id"), "field_id")
        return cls(
            field_id=field_id,
            label=_text(payload.get("label"), f"field {field_id} label"),
            bbox=_bbox(payload.get("bbox"), f"field {field_id}"),
            value=_text(payload.get("value"), f"field {field_id} value"),
            paddle_text=_text(
                payload.get("paddle_text"), f"field {field_id} paddle_text"
            ),
            model_guess=_text(
                payload.get("model_guess"), f"field {field_id} model_guess"
            ),
            confidence=_score(
                payload.get("confidence"), f"field {field_id} confidence"
            ),
            needs_review=_bool(
                payload.get("needs_review"), f"field {field_id} needs_review"
            ),
            history=tuple(
                FieldHistory.from_dict(_mapping(item, "field history"))
                for item in _items(payload.get("history"), "field history")
            ),
        )


@dataclass(frozen=True)
class PageEvidence:
    schema: str
    page: int
    render: RenderMetadata
    ocr_blocks: tuple[OcrBlock, ...] = ()
    tables: tuple[TableEvidence, ...] = ()
    fields: tuple[FieldEvidence, ...] = ()
    degraded_adapters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != RECOGNITION_SCHEMA:
            raise ValueError(f"Unsupported recognition schema: {self.schema}")
        _positive_int(self.page, "page")
        if not isinstance(self.render, RenderMetadata):
            raise ValueError("render must be RenderMetadata")
        object.__setattr__(self, "ocr_blocks", tuple(self.ocr_blocks))
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "degraded_adapters", tuple(self.degraded_adapters))

        for block in self.ocr_blocks:
            if not _inside_render(block.bbox, self.render):
                raise ValueError("OCR block bbox exceeds render dimensions")
        for table in self.tables:
            if table.page != self.page:
                raise ValueError("table page does not match page evidence")
            if not _inside_render(table.bbox, self.render):
                raise ValueError("table bbox exceeds render dimensions")
            for cell in table.cells:
                if not _inside_render(cell.bbox, self.render):
                    raise ValueError("table cell bbox exceeds render dimensions")

        field_ids: set[str] = set()
        for field in self.fields:
            if not _inside_render(field.bbox, self.render):
                raise ValueError(
                    f"field {field.field_id} bbox exceeds render dimensions"
                )
            if field.field_id in field_ids:
                raise ValueError(f"Duplicate field_id: {field.field_id}")
            field_ids.add(field.field_id)
        for adapter in self.degraded_adapters:
            if not _text(adapter, "degraded adapter").strip():
                raise ValueError("degraded adapter must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "page": self.page,
            "render": self.render.to_dict(),
            "ocr_blocks": [block.to_dict() for block in self.ocr_blocks],
            "tables": [table.to_dict() for table in self.tables],
            "fields": [field.to_dict() for field in self.fields],
            "degraded_adapters": list(self.degraded_adapters),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PageEvidence":
        schema = _text(payload.get("schema"), "schema")
        if schema != RECOGNITION_SCHEMA:
            raise ValueError(f"Unsupported recognition schema: {schema}")
        return cls(
            schema=schema,
            page=_positive_int(payload.get("page"), "page"),
            render=RenderMetadata.from_dict(_mapping(payload.get("render"), "render")),
            ocr_blocks=tuple(
                OcrBlock.from_dict(_mapping(item, "OCR block"))
                for item in _items(payload.get("ocr_blocks"), "ocr_blocks")
            ),
            tables=tuple(
                TableEvidence.from_dict(_mapping(item, "table"))
                for item in _items(payload.get("tables"), "tables")
            ),
            fields=tuple(
                FieldEvidence.from_dict(_mapping(item, "field"))
                for item in _items(payload.get("fields"), "fields")
            ),
            degraded_adapters=tuple(
                _text(item, "degraded adapter")
                for item in _items(
                    payload.get("degraded_adapters"), "degraded_adapters"
                )
            ),
        )


@dataclass(frozen=True)
class VisionPageResult:
    page: int
    latex: str
    fields: tuple[FieldEvidence, ...] = ()

    def __post_init__(self) -> None:
        _positive_int(self.page, "page")
        _text(self.latex, "latex")
        object.__setattr__(self, "fields", tuple(self.fields))


@dataclass(frozen=True)
class PageRecognitionResult:
    page: int
    latex: str
    evidence: PageEvidence
    cache_key: str
    from_cache: bool = False

    def __post_init__(self) -> None:
        _positive_int(self.page, "page")
        _text(self.latex, "latex")
        if self.page != self.evidence.page:
            raise ValueError("result page does not match evidence page")
        if not _text(self.cache_key, "cache_key").strip():
            raise ValueError("cache_key must not be empty")
        _bool(self.from_cache, "from_cache")
