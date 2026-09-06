import sys
from types import SimpleNamespace

import pytest

from lexoid.core import conversion_utils
from lexoid.core.recognition.paddle import (
    PaddleLayoutAdapter, PaddleTableAdapter, PaddleTextAdapter, PaddleVlFallbackAdapter,
)


@pytest.mark.parametrize("adapter", [
    PaddleTextAdapter, PaddleLayoutAdapter, PaddleTableAdapter, PaddleVlFallbackAdapter,
])
def test_arm_cpu_adapters_disable_crashing_pir_path(monkeypatch, adapter):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    calls = []
    pipeline = object()

    def factory(**kwargs):
        calls.append(kwargs)
        return pipeline

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(
        **{adapter.pipeline_name: factory},
    ))
    instance = adapter(device="cpu")
    assert instance.pipeline is instance.pipeline is pipeline
    assert len(calls) == 1
    assert calls[0]["engine_config"]["paddle_static"]["enable_new_ir"] is False
    assert calls[0]["engine_config"]["paddle_static"]["run_mode"] == "paddle"
    assert "engine" not in calls[0]  # VL still selects its dynamic model engine.


def test_arm_cpu_orientation_uses_same_compatible_runtime(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    monkeypatch.setattr(conversion_utils, "_DOC_ORIENTATION_CLASSIFIER", None)
    calls = []
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(
        DocImgOrientationClassification=lambda **kwargs: calls.append(kwargs) or object(),
    ))
    conversion_utils._get_doc_orientation_classifier()
    assert calls[0]["engine_config"]["paddle_static"]["enable_new_ir"] is False


def test_gpu_keeps_provider_runtime_defaults(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    calls = []
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(
        PaddleOCR=lambda **kwargs: calls.append(kwargs) or object(),
    ))
    PaddleTextAdapter(device="gpu:0").pipeline
    assert calls[0]["device"] == "gpu:0"
    assert "engine_config" not in calls[0]


def test_layout_loads_only_the_structure_layout_model(monkeypatch):
    calls = []
    monkeypatch.setenv("PADDLE_LAYOUT_MODEL", "test-layout-model")
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(
        LayoutDetection=lambda **kwargs: calls.append(kwargs) or object(),
    ))
    PaddleLayoutAdapter().pipeline
    assert calls[0]["model_name"] == "test-layout-model"
    assert "use_doc_orientation_classify" not in calls[0]
