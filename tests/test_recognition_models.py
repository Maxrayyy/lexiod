from __future__ import annotations

import pytest

from lexoid.core.recognition.models import (
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


def _page_evidence() -> PageEvidence:
    return PageEvidence(
        schema="recognition/v1",
        page=21,
        render=RenderMetadata(dpi=240, width=1983, height=2804),
        ocr_blocks=(
            OcrBlock(text="operator", bbox=(10, 20, 80, 44), score=0.91),
        ),
        tables=(
            TableEvidence(
                page=21,
                bbox=(42, 180, 1130, 1540),
                rows=1,
                columns=2,
                cells=(
                    TableCell(
                        row=0,
                        column=0,
                        rowspan=1,
                        colspan=2,
                        text="operator",
                        bbox=(45, 320, 280, 390),
                        score=0.98,
                    ),
                ),
                source="table_v2",
            ),
        ),
        fields=(
            FieldEvidence(
                field_id="LEX-P0021-V0003",
                label="operator",
                bbox=(620, 1180, 910, 1260),
                value="Zhang",
                paddle_text="Chang",
                model_guess="Zhang",
                confidence=0.54,
                needs_review=True,
                history=(
                    FieldHistory(
                        stage="recognize",
                        from_value="Chang",
                        to_value="Zhang",
                        reason="source image is clearer",
                    ),
                ),
            ),
        ),
        degraded_adapters=("paddle_layout",),
    )


def test_page_evidence_round_trips_without_provider_objects() -> None:
    evidence = _page_evidence()

    restored = PageEvidence.from_dict(evidence.to_dict())

    assert restored == evidence
    assert restored.to_dict() == {
        "schema": "recognition/v1",
        "page": 21,
        "render": {"dpi": 240, "width": 1983, "height": 2804},
        "ocr_blocks": [
            {"text": "operator", "bbox": [10, 20, 80, 44], "score": 0.91},
        ],
        "tables": [
            {
                "page": 21,
                "bbox": [42, 180, 1130, 1540],
                "rows": 1,
                "columns": 2,
                "cells": [
                    {
                        "row": 0,
                        "column": 0,
                        "rowspan": 1,
                        "colspan": 2,
                        "text": "operator",
                        "bbox": [45, 320, 280, 390],
                        "score": 0.98,
                    },
                ],
                "source": "table_v2",
                "fallback_reason": None,
            },
        ],
        "fields": [
            {
                "field_id": "LEX-P0021-V0003",
                "label": "operator",
                "bbox": [620, 1180, 910, 1260],
                "value": "Zhang",
                "paddle_text": "Chang",
                "model_guess": "Zhang",
                "confidence": 0.54,
                "needs_review": True,
                "history": [
                    {
                        "stage": "recognize",
                        "from": "Chang",
                        "to": "Zhang",
                        "reason": "source image is clearer",
                    },
                ],
            },
        ],
        "degraded_adapters": ["paddle_layout"],
    }


def test_page_evidence_rejects_unknown_schema() -> None:
    payload = _page_evidence().to_dict()
    payload["schema"] = "recognition/v9"

    with pytest.raises(ValueError, match="Unsupported recognition schema"):
        PageEvidence.from_dict(payload)


def test_page_evidence_rejects_field_box_outside_render() -> None:
    payload = _page_evidence().to_dict()
    payload["fields"][0]["bbox"] = [620, 1180, 2200, 1260]

    with pytest.raises(ValueError, match="field LEX-P0021-V0003 bbox"):
        PageEvidence.from_dict(payload)


@pytest.mark.parametrize("device", ["cpu", "gpu", "gpu:0", "gpu:7"])
def test_recognition_config_accepts_supported_devices(device: str) -> None:
    assert RecognitionConfig(device=device).device == device


def test_recognition_config_defaults_to_cpu() -> None:
    assert RecognitionConfig().device == "cpu"


@pytest.mark.parametrize("device", ["cuda", "gpu:", "gpu:-1", "mps"])
def test_recognition_config_rejects_unsupported_devices(device: str) -> None:
    with pytest.raises(ValueError, match="Unsupported OCR device"):
        RecognitionConfig(device=device)


def test_recognition_config_rejects_retry_dpi_below_initial_dpi() -> None:
    with pytest.raises(ValueError, match="retry_crop_dpi"):
        RecognitionConfig(initial_render_dpi=480, retry_crop_dpi=240)


def test_result_contracts_are_immutable() -> None:
    evidence = _page_evidence()
    vision = VisionPageResult(page=21, latex="page tex", fields=evidence.fields)
    result = PageRecognitionResult(
        page=21,
        latex=vision.latex,
        evidence=evidence,
        cache_key="abc123",
    )

    with pytest.raises(AttributeError):
        result.page = 22  # type: ignore[misc]


def test_rendered_page_validates_image_dimensions() -> None:
    class ImageStub:
        size = (100, 200)

    with pytest.raises(ValueError, match="image dimensions"):
        RenderedPage(
            page=1,
            dpi=240,
            width=101,
            height=200,
            image=ImageStub(),
        )


def test_layout_region_rejects_invalid_score() -> None:
    with pytest.raises(ValueError, match="score"):
        LayoutRegion(kind="table", bbox=(0, 0, 10, 10), score=1.1)
