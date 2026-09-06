"""Lazy PaddleOCR adapters; provider results never escape this module."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

import numpy as np
from bs4 import BeautifulSoup

from lexoid.core.paddle_runtime import paddle_runtime_options
from lexoid.core.model_config import resolve_model

from .models import LayoutRegion, OcrBlock, RenderedPage, TableCell, TableEvidence


def _box(raw, width, height, offset=(0, 0)):
    points = np.asarray(raw, dtype=float)
    if points.size == 4:
        x0, y0, x1, y1 = points.reshape(-1)
    else:
        points = points.reshape(-1, 2)
        x0, y0 = points.min(axis=0)
        x1, y1 = points.max(axis=0)
    if not np.isfinite([x0, y0, x1, y1]).all():
        raise ValueError("Non-finite Paddle bounding box")
    return (max(0, int(x0) + offset[0]), max(0, int(y0) + offset[1]),
            min(width, int(np.ceil(x1)) + offset[0]),
            min(height, int(np.ceil(y1)) + offset[1]))


def _image(page):
    return np.asarray(page.image.convert("RGB"))[:, :, ::-1].copy()


class _Adapter:
    pipeline_name = ""
    model_variable = None
    preprocessing = {"use_doc_orientation_classify": False, "use_doc_unwarping": False}
    options = {}

    def __init__(self, device="cpu", pipeline=None):
        self.device = device
        self._pipeline = pipeline

    @property
    def pipeline(self):
        if self._pipeline is None:
            import paddleocr

            options = dict(self.options)
            if self.model_variable:
                options["model_name"] = resolve_model(self.model_variable)
            self._pipeline = getattr(paddleocr, self.pipeline_name)(
                **paddle_runtime_options(self.device), **self.preprocessing, **options,
            )
        return self._pipeline


class PaddleTextAdapter(_Adapter):
    pipeline_name = "PaddleOCR"
    options = {"use_textline_orientation": False}

    def predict_batch(self, pages: Sequence[RenderedPage]):
        if not pages:
            return {}
        results = list(self.pipeline.predict(input=[_image(page) for page in pages]))
        if len(results) != len(pages):
            raise ValueError("Paddle text returned an unexpected page count")
        output = {}
        for page, result in zip(pages, results):
            texts, scores = result["rec_texts"], result["rec_scores"]
            boxes = result.get("rec_boxes")
            if boxes is None:
                boxes = result["rec_polys"]
            if not len(texts) == len(scores) == len(boxes):
                raise ValueError("Paddle text arrays have different lengths")
            output[page.page] = tuple(
                OcrBlock(str(text), _box(box, page.width, page.height), float(score))
                for text, score, box in zip(texts, scores, boxes)
            )
        return output


class PaddleLayoutAdapter(_Adapter):
    # Use PPStructureV3's layout model without repeating its full-page OCR.
    pipeline_name = "LayoutDetection"
    model_variable = "PADDLE_LAYOUT_MODEL"
    preprocessing = {}
    options = {"layout_nms": True}

    def predict(self, page):
        result = next(iter(self.pipeline.predict(input=_image(page))))
        return tuple(LayoutRegion(
            str(item["label"]), _box(item["coordinate"], page.width, page.height),
            float(item["score"]),
        ) for item in result["boxes"])


def _table_from_html(result, page, bbox, source):
    html = result.get("pred_html", "")
    rows = BeautifulSoup(html, "html.parser").select("tr")
    raw_boxes = result.get("cell_box_list", [])
    occupied, cells, index, columns = set(), [], 0, 0
    for row_index, row in enumerate(rows):
        column = 0
        for node in row.find_all(["td", "th"], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            rowspan, colspan = int(node.get("rowspan", 1)), int(node.get("colspan", 1))
            if rowspan < 1 or colspan < 1 or row_index + rowspan > len(rows):
                raise ValueError("Invalid Paddle table span")
            covered = {(r, c) for r in range(row_index, row_index + rowspan)
                       for c in range(column, column + colspan)}
            if covered & occupied:
                raise ValueError("Overlapping Paddle table cells")
            occupied.update(covered)
            # Missing geometric evidence stays at the table region, never invented.
            box = (_box(raw_boxes[index], page.width, page.height, bbox[:2])
                   if index < len(raw_boxes) else bbox)
            cells.append(TableCell(row_index, column, rowspan, colspan,
                                   node.get_text(" ", strip=True), box, 0.0))
            index += 1
            column += colspan
            columns = max(columns, column)
    return TableEvidence(page.page, bbox, max(1, len(rows)), max(1, columns),
                         tuple(cells), source,
                         None if rows and cells else "missing_table_structure")


def table_needs_vl(table):
    occupied = set()
    for cell in table.cells:
        covered = {(r, c) for r in range(cell.row, cell.row + cell.rowspan)
                   for c in range(cell.column, cell.column + cell.colspan)}
        if occupied & covered:
            return True
        occupied.update(covered)
    return occupied != {(r, c) for r in range(table.rows) for c in range(table.columns)}


class PaddleTableAdapter(_Adapter):
    pipeline_name = "TableRecognitionPipelineV2"
    options = {"use_layout_detection": False}

    def predict(self, page, regions):
        output = []
        for region in regions:
            if region.kind != "table":
                continue
            crop = _image(page)[region.bbox[1]:region.bbox[3], region.bbox[0]:region.bbox[2]]
            result = next(iter(self.pipeline.predict(input=crop)))
            tables = result.get("table_res_list", [])
            if not tables:
                output.append(_table_from_html({}, page, region.bbox, "table_pipeline"))
            for raw in tables:
                try:
                    table = _table_from_html(raw, page, region.bbox, "table_pipeline")
                except (TypeError, ValueError):
                    table = _table_from_html({}, page, region.bbox, "table_pipeline")
                    table = replace(table, fallback_reason="invalid_table_structure")
                output.append(table)
        return tuple(output)


class PaddleVlFallbackAdapter(_Adapter):
    pipeline_name = "PaddleOCRVL"

    def predict(self, page, table):
        crop = _image(page)[table.bbox[1]:table.bbox[3], table.bbox[0]:table.bbox[2]]
        result = next(iter(self.pipeline.predict(input=crop)))
        if hasattr(result, "json"):
            result = result.json["res"]
        for block in result.get("parsing_res_list", []):
            if block.get("block_label") == "table":
                candidate = _table_from_html({"pred_html": block["block_content"]},
                                             page, table.bbox, "paddle_vl")
                if not table_needs_vl(candidate):
                    return replace(candidate, original_table=table,
                                   fallback_reason=table.fallback_reason
                                   or "inconsistent_table_topology")
        return replace(table, fallback_reason="vl_unresolved_table")
