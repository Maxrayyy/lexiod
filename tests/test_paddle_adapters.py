import json
from pathlib import Path

from PIL import Image
import pytest

from lexoid.core.recognition.models import (
    LayoutRegion, OcrBlock, RenderedPage, TableCell, TableEvidence,
)
from lexoid.core.recognition.paddle import (
    PaddleLayoutAdapter, PaddleTableAdapter, PaddleTextAdapter, PaddleVlFallbackAdapter,
    table_needs_vl,
)


class Pipeline:
    def __init__(self, results):
        self.results = results
        self.inputs = []

    def predict(self, input, **kwargs):
        self.inputs.append(input)
        return iter(self.results)


def provider_fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / "recognition" / name).read_text("utf-8"))


@pytest.fixture
def page():
    return RenderedPage(1, 240, 300, 400, Image.new("RGB", (300, 400)))


def test_text_normalizes_real_paddle_arrays(page):
    import numpy as np

    raw = provider_fixture("paddle_text.json")
    raw["rec_scores"], raw["rec_boxes"] = np.asarray(raw["rec_scores"]), np.asarray(raw["rec_boxes"])
    pipeline = Pipeline([raw])
    result = PaddleTextAdapter(pipeline=pipeline).predict_batch([page])
    assert result == {1: (OcrBlock("Batch ID", (42, 81, 233, 126), 0.98),)}


def test_text_rejects_missing_page_results(page):
    with pytest.raises(ValueError, match="page"):
        PaddleTextAdapter(pipeline=Pipeline([])).predict_batch([page])


def test_layout_reads_mapping_results(page):
    pipeline = Pipeline([provider_fixture("ppstructure_table.json")])
    assert PaddleLayoutAdapter(pipeline=pipeline).predict(page) == (
        LayoutRegion("table", (10, 20, 200, 300), 0.99),
    )


def test_table_html_spans_and_crop_coordinates(page):
    pipeline = Pipeline([provider_fixture("table_pipeline.json")])
    tables = PaddleTableAdapter(pipeline=pipeline).predict(
        page, [LayoutRegion("table", (10, 20, 200, 300), 0.99)]
    )
    assert pipeline.inputs[0].shape[:2] == (280, 190)
    assert (tables[0].rows, tables[0].columns) == (2, 2)
    assert tables[0].cells[0].colspan == 2
    assert tables[0].cells[0].bbox == (10, 20, 190, 40)
    assert not table_needs_vl(tables[0])


def test_topology_detects_gaps_and_overlaps():
    a = TableCell(0, 0, 1, 1, "A", (1, 1, 10, 10), 0.9)
    b = TableCell(0, 1, 1, 1, "B", (10, 1, 20, 10), 0.9)
    def table(cells):
        return TableEvidence(1, (0, 0, 30, 20), 1, 2, cells, "test")
    assert table_needs_vl(table(()))
    assert table_needs_vl(table((a,)))
    assert table_needs_vl(table((a, a)))
    assert not table_needs_vl(table((a, b)))


def test_vl_reads_provider_json_and_retains_original_table(page):
    class VlResult(dict):
        @property
        def json(self):
            return {"res": {"parsing_res_list": [{
                "block_label": "table", "block_content": "<table><tr><td>A</td></tr></table>",
            }]}}

    original = TableEvidence(1, (10, 20, 200, 300), 1, 1, (),
                             "table_pipeline", "missing_table_structure")
    result = PaddleVlFallbackAdapter(pipeline=Pipeline([
        VlResult(parsing_res_list=[object()]),
    ])).predict(page, original)
    assert result.source == "paddle_vl"
    assert result.cells[0].text == "A"
    assert result.original_table == original
    assert result.fallback_reason == "missing_table_structure"
    assert TableEvidence.from_dict(result.to_dict()) == result
