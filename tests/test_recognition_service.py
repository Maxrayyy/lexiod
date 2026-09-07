import json
import time
import threading
from dataclasses import replace

from PIL import Image
import pytest

from lexoid.core.recognition.models import RecognitionConfig, RenderedPage, VisionPageResult
from lexoid.core.recognition.service import (
    AdaptiveConcurrency, DocumentRecognizer, IncompleteDocumentError, _page_count,
)
from lexoid.core.recognition.vision import VisionLatexAdapter


def test_page_count_uses_installed_pdfium_api():
    from pathlib import Path
    assert _page_count(Path("examples/inputs/test_1.pdf")) > 0


def render(path, page, dpi, auto_orient=True):
    return RenderedPage(page, dpi, 100, 200, Image.new("RGB", (100, 200)))


class Text:
    def __init__(self, fail=False):
        self.pages = []
        self.fail = fail

    def predict_batch(self, pages):
        self.pages.extend(p.page for p in pages)
        if self.fail:
            raise RuntimeError("OCR unavailable")
        return {p.page: () for p in pages}


class Layout:
    def predict(self, page):
        return ()


class Tables:
    def predict(self, page, regions):
        return ()


class Vision:
    model = "gpt-6-astra"

    def __init__(self, fail_page=None):
        self.pages = []
        self.fail_page = fail_page
        self.attempts = {}

    def recognize(self, page, evidence, page_count):
        self.attempts[page.page] = self.attempts.get(page.page, 0) + 1
        if page.page == self.fail_page:
            raise ValueError("Invalid page")
        time.sleep((4 - page.page) * 0.005)
        self.pages.append(page.page)
        return VisionPageResult(page.page, f"Page {page.page}\n"
                                f"% LEXOID_PAGE_COMPLETED: {page.page}/{page_count}", ())


class VisionInvalidOnce(Vision):
    def __init__(self, invalid_page):
        super().__init__()
        self.invalid_page = invalid_page

    def recognize(self, page, evidence, page_count):
        if page.page == self.invalid_page and self.attempts.get(page.page, 0) == 0:
            self.attempts[page.page] = 1
            raise ValueError("Generated page contains a numbered section command")
        return super().recognize(page, evidence, page_count)


class APIConnectionError(Exception):
    pass


class VisionConnectionErrorOnce(Vision):
    def __init__(self, failed_page):
        super().__init__()
        self.failed_page = failed_page

    def recognize(self, page, evidence, page_count):
        if page.page == self.failed_page and self.attempts.get(page.page, 0) == 0:
            self.attempts[page.page] = 1
            raise APIConnectionError("Connection error")
        return super().recognize(page, evidence, page_count)


def recognizer(tmp_path, text=None, vision=None):
    return DocumentRecognizer(
        model="gpt-6-astra", config=RecognitionConfig(enable_vl_fallback=False),
        cache_dir=tmp_path / "cache", renderer=render, page_counter=lambda path: 3,
        text_adapter=text or Text(), layout_adapter=Layout(), table_adapter=Tables(),
        vision_adapter=vision or Vision(),
    )


def test_callbacks_and_results_stay_ordered_despite_concurrent_completion(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    vision = Vision()
    callbacks = []
    results = recognizer(tmp_path, vision=vision).recognize(
        pdf, page_callback=lambda page, total, text: callbacks.append(page))
    assert vision.pages != [1, 2, 3]
    assert callbacks == [1, 2, 3]
    assert [result.page for result in results] == [1, 2, 3]


def test_cached_rerun_calls_neither_renderer_nor_models(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    recognizer(tmp_path).recognize(pdf)
    text, vision = Text(), Vision()
    run = recognizer(tmp_path, text, vision)
    def fail(*args, **kwargs):
        raise AssertionError("Rendered a cached page")
    run.renderer = fail
    results = run.recognize(pdf)
    assert all(result.from_cache for result in results)
    assert text.pages == vision.pages == []


def test_paddle_failure_is_recorded_and_vision_still_completes(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    results = recognizer(tmp_path, text=Text(fail=True)).recognize(pdf)
    assert all("paddle_text" in r.evidence.degraded_adapters for r in results)


def test_page_that_stays_invalid_is_retried_once_then_degraded(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    callbacks = []
    vision = Vision(fail_page=2)

    results = recognizer(tmp_path, vision=vision).recognize(
        pdf, page_callback=lambda page, total, text: callbacks.append(page))

    assert [result.page for result in results] == [1, 2, 3]
    assert callbacks == [1, 2, 3]
    assert vision.attempts[2] == 2
    assert "vision" in results[1].evidence.degraded_adapters


def test_primary_can_reserve_its_only_retry_for_an_external_fallback(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    vision = Vision(fail_page=2)
    run = recognizer(tmp_path, vision=vision)
    run.config = replace(run.config, max_page_attempts=1)
    results = run.recognize(pdf)
    assert vision.attempts[2] == 1
    assert "LEXOID_RECOGNITION_FALLBACK" in results[1].latex
    assert [r.page for r in results] == [1, 2, 3]


def test_page_processing_overlaps_later_recognition_and_preserves_delivery_order(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    check_started = threading.Event()
    later_recognized = threading.Event()

    class StreamingVision(Vision):
        def recognize(self, page, evidence, page_count):
            if page.page == 2:
                assert check_started.wait(3)
                later_recognized.set()
            return super().recognize(page, evidence, page_count)

    processed, delivered = [], []

    def process(result, total, gate):
        if result.page == 1:
            check_started.set()
            assert later_recognized.wait(3)
        processed.append(result.page)
        return replace(result, latex="processed " + result.latex)

    run = recognizer(tmp_path, vision=StreamingVision())
    run.page_processor = process
    results = run.recognize(pdf, page_callback=lambda p, n, t: delivered.append((p, t)))
    assert processed == [1, 2, 3]
    assert [p for p, _ in delivered] == [1, 2, 3]
    assert all(t.startswith("processed ") for _, t in delivered)
    assert all(r.latex.startswith("processed ") for r in results)


def test_primary_and_page_processor_share_one_model_concurrency_gate(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    active = maximum = 0
    lock = threading.Lock()

    def request():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.02)
        with lock:
            active -= 1

    class CountedVision(Vision):
        def recognize(self, *args):
            request()
            return super().recognize(*args)

    def process(result, total, gate):
        with gate.slot():
            request()
        return result

    run = recognizer(tmp_path, vision=CountedVision())
    run.config = replace(run.config, vision_concurrency=2)
    run.page_processor = process
    assert len(run.recognize(pdf)) == 3
    assert maximum == 2


def test_invalid_model_layout_is_retried_before_page_fails(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    vision = VisionInvalidOnce(invalid_page=2)
    results = recognizer(tmp_path, vision=vision).recognize(pdf)
    assert [result.page for result in results] == [1, 2, 3]
    assert vision.attempts[2] == 2


def test_api_connection_error_is_retried_once(tmp_path, monkeypatch):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"pdf")
    vision = VisionConnectionErrorOnce(failed_page=2)
    monkeypatch.setattr("lexoid.core.recognition.service.time.sleep", lambda _seconds: None)

    results = recognizer(tmp_path, vision=vision).recognize(pdf)

    assert [result.page for result in results] == [1, 2, 3]
    assert vision.attempts[2] == 2
    assert "vision" not in results[1].evidence.degraded_adapters


def _one_page_payload(tex_value="A12", model_guess="A12"):
    return {
        "latex": (
            "\\documentclass{article}\n"
            "\\newcommand{\\fieldvalue}[1]{#1}\n"
            "\\newcommand{\\handwritten}[1]{#1}\n"
            "\\begin{document}\n"
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: Batch\n"
            f"\\fieldvalue{{{tex_value}}}\n"
            "\\end{document}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1"
        ),
        "fields": [{
            "field_id": "LEX-P0001-V0001", "label": "Batch",
            "bbox": [10, 20, 90, 40], "model_guess": model_guess,
            "confidence": 0.9, "needs_review": False,
        }],
    }


def _recognizer_with_responses(tmp_path, responses):
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    adapter = VisionLatexAdapter("gpt-6-astra", response_factory=respond)
    run = DocumentRecognizer(
        model="gpt-6-astra", config=RecognitionConfig(ocr="none"),
        cache_dir=tmp_path / "cache", renderer=render, page_counter=lambda path: 1,
        vision_adapter=adapter,
    )
    source = tmp_path / "input.pdf"
    source.write_bytes(b"pdf")
    return run.recognize(source)[0], calls


def test_printed_value_mismatch_retries_once_then_uses_latest_tex(tmp_path):
    first = _one_page_payload("A12", "A17")
    second = _one_page_payload("B34", "B39")

    result, calls = _recognizer_with_responses(
        tmp_path,
        [{"response": json.dumps(first)}, {"response": json.dumps(second)}],
    )

    assert len(calls) == 2
    assert result.evidence.fields[0].value == "B34"
    assert result.evidence.fields[0].model_guess == "B34"
    assert r"\fieldvalue{B34}" in result.latex


@pytest.mark.parametrize("damage", ["page_marker", "field_id", "table_rows"])
def test_structural_errors_retry_once_then_keep_visible_tex(tmp_path, damage):
    payload = _one_page_payload()
    if damage == "page_marker":
        payload["latex"] += "\n% LEXOID_PAGE_COMPLETED: 9/9"
    elif damage == "field_id":
        payload["latex"] = payload["latex"].replace(
            "LEX-P0001-V0001", "LEX-P0009-V0001", 1
        )
    else:
        payload["latex"] = payload["latex"].replace(
            r"\fieldvalue{A12}",
            r"\fieldvalue{A12}"
            "\n\\begin{tabular}{l}first\\\\\\end{tabular}\n"
            "\\begin{tabular}{l}second\\\\\\end{tabular}",
        )

    result, calls = _recognizer_with_responses(
        tmp_path, [{"response": json.dumps(payload)}] * 2
    )

    assert len(calls) == 2
    assert r"\fieldvalue{A12}" in result.latex
    assert result.latex.count("LEXOID_PAGE_COMPLETED") == 1
    assert result.evidence.fields == ()
    assert "vision" in result.evidence.degraded_adapters


def test_empty_responses_retry_once_then_emit_a_complete_page(tmp_path):
    result, calls = _recognizer_with_responses(
        tmp_path, [{"response": ""}, {"response": ""}]
    )

    assert len(calls) == 2
    assert r"\begin{document}" in result.latex
    assert "% LEXOID_PAGE_COMPLETED: 1/1" in result.latex
    assert "vision" in result.evidence.degraded_adapters


@pytest.mark.parametrize("damage", ["missing_id", "bbox", "confidence"])
def test_metadata_errors_keep_tex_without_retry_and_log_reason(tmp_path, capsys, damage):
    payload = _one_page_payload()
    payload.pop("fields")
    payload["field_meta"] = {"V0001": [[10, 20, 80, 50], 0.9, False]}
    if damage == "missing_id":
        payload["field_meta"] = {}
    elif damage == "bbox":
        payload["field_meta"]["V0001"][0] = [10, 20, "bad", 50]
    else:
        payload["field_meta"]["V0001"][1] = 2.0

    result, calls = _recognizer_with_responses(
        tmp_path, [{"response": json.dumps(payload)}]
    )

    assert len(calls) == 1
    assert r"\fieldvalue{A12}" in result.latex
    assert result.evidence.fields == ()
    assert "vision" in result.evidence.degraded_adapters
    events = [json.loads(line.removeprefix("[LLM_CALL] "))
              for line in capsys.readouterr().err.splitlines()
              if line.startswith("[LLM_CALL] ")]
    error = next(event for event in events
                 if event["event"] == "validation_or_request_error")
    assert error["error_stage"] == "field_metadata"
    assert error["error_message"]
    assert error["action"] == "use_tex"
    assert error["has_fallback_tex"] is True


def test_layout_error_logs_reason_and_retry_decision(tmp_path, capsys):
    payload = _one_page_payload()
    payload["latex"] += "\n% LEXOID_PAGE_COMPLETED: 9/9"
    _recognizer_with_responses(tmp_path, [{"response": json.dumps(payload)}])
    events = [json.loads(line.removeprefix("[LLM_CALL] "))
              for line in capsys.readouterr().err.splitlines()
              if line.startswith("[LLM_CALL] ")]
    errors = [event for event in events
              if event["event"] == "validation_or_request_error"]
    assert [event["action"] for event in errors] == ["retry", "use_tex"]
    assert all(event["error_stage"] == "latex_validation" for event in errors)
    assert "completion marker" in errors[0]["error_message"]


def test_visual_row_warning_preserves_fields_without_retry(tmp_path, capsys):
    payload = _one_page_payload()
    payload["latex"] = payload["latex"].replace(
        "% #VALUE_ID: LEX-P0001-V0001", "\\begin{tabular}{l}\n"
        "% #VALUE_ID: LEX-P0001-V0001"
    ).replace(
        r"\fieldvalue{A12}", r"\fieldvalue{A12}\\" + "\n"
        "% #VALUE_ID: LEX-P0001-V0002\n% #FIELD_VALUE: Other\n"
        "\\fieldvalue{B34}\\\\\n\\end{tabular}"
    )
    payload.pop("fields")
    payload["field_meta"] = {
        "V0001": [[10, 20, 40, 40], 0.9, False],
        "V0002": [[50, 20, 90, 40], 0.8, False],
    }
    result, calls = _recognizer_with_responses(
        tmp_path, [{"response": json.dumps(payload)}]
    )
    assert len(calls) == 1
    assert result.latex == payload["latex"]
    assert [(f.field_id, f.value) for f in result.evidence.fields] == [
        ("LEX-P0001-V0001", "A12"), ("LEX-P0001-V0002", "B34")]
    assert result.evidence.degraded_adapters == ()
    events = [json.loads(line.removeprefix("[LLM_CALL] "))
              for line in capsys.readouterr().err.splitlines()
              if line.startswith("[LLM_CALL] ")]
    assert not any(event["event"] == "validation_or_request_error" for event in events)
    warning = next(event for event in events if event["event"] == "validation_warning")
    assert warning["code"] == "visual_table_row_mismatch"
    assert warning["page"] == 1
    assert warning["field_ids"] == ["LEX-P0001-V0001", "LEX-P0001-V0002"]
    assert warning["action"] == "keep_tex_and_fields"


def test_rate_limit_reduces_capacity_then_recovers_slowly():
    gate = AdaptiveConcurrency(initial=4)
    gate.on_rate_limit()
    assert gate.limit == 2
    for _ in range(gate.successes_per_recovery):
        gate.on_success()
    assert gate.limit == 3


def test_vision_only_never_constructs_paddle_adapters_and_resumes(tmp_path, monkeypatch):
    from lexoid.core.recognition import service

    def forbidden(*args, **kwargs):
        raise AssertionError("Pure vision must not initialize Paddle adapters")

    for name in ("PaddleTextAdapter", "PaddleLayoutAdapter", "PaddleTableAdapter",
                 "PaddleVlFallbackAdapter"):
        monkeypatch.setattr(service, name, forbidden)
    source = tmp_path / "input.pdf"
    source.write_bytes(b"pdf")
    vision = Vision()
    run = DocumentRecognizer(
        model="gpt-6-astra", config=RecognitionConfig(ocr="none"),
        cache_dir=tmp_path / "cache", renderer=render, page_counter=lambda path: 3,
        vision_adapter=vision,
    )
    results = run.recognize(source)
    assert len(results) == 3
    assert all(not result.evidence.ocr_blocks and not result.evidence.tables
               and not result.evidence.degraded_adapters for result in results)
    vision.pages.clear()
    run.renderer = forbidden
    assert all(result.from_cache for result in run.recognize(source))
    assert vision.pages == []
