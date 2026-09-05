from __future__ import annotations

from dataclasses import replace

from lexoid.core.recognition.cache import RecognitionCache, build_cache_key
from lexoid.core.recognition.models import RecognitionConfig


PDF_SHA = "a" * 64
CONFIG = RecognitionConfig()


def test_cache_key_is_independent_of_mapping_order() -> None:
    first = build_cache_key(
        PDF_SHA,
        CONFIG,
        {"paddleocr": "3.7.0", "paddlex": "3.7.2"},
        "hybrid-latex-v1",
        "gpt-5.6-luna",
    )
    second = build_cache_key(
        PDF_SHA,
        CONFIG,
        {"paddlex": "3.7.2", "paddleocr": "3.7.0"},
        "hybrid-latex-v1",
        "gpt-5.6-luna",
    )

    assert first == second
    assert len(first) == 64


def test_cache_key_changes_when_recognition_input_changes() -> None:
    original = build_cache_key(
        PDF_SHA, CONFIG, {"paddleocr": "3.7.0"}, "p1", "vision"
    )
    changed = build_cache_key(
        PDF_SHA,
        replace(CONFIG, initial_render_dpi=300),
        {"paddleocr": "3.7.0"},
        "p1",
        "vision",
    )

    assert original != changed


def test_cache_uses_stage_key_and_physical_page_paths(tmp_path) -> None:
    cache = RecognitionCache(tmp_path, "abc")

    assert cache.path("ocr", 21, ".json") == tmp_path / "ocr" / "abc" / "21.json"


def test_cache_round_trips_json_and_text_atomically(tmp_path) -> None:
    cache = RecognitionCache(tmp_path, "abc")

    cache.save_json("ocr", 1, {"page": 1, "blocks": []})
    cache.save_text("draft", 1, "page tex")

    assert cache.load_json("ocr", 1) == {"page": 1, "blocks": []}
    assert cache.load_text("draft", 1) == "page tex"
    assert not list(tmp_path.rglob("*.tmp"))


def test_cache_rejects_truncated_json(tmp_path) -> None:
    cache = RecognitionCache(tmp_path, "abc")
    path = cache.path("ocr", 1, ".json")
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    assert cache.load_json("ocr", 1) is None


def test_cache_invalidate_removes_only_requested_stage_page(tmp_path) -> None:
    cache = RecognitionCache(tmp_path, "abc")
    cache.save_json("ocr", 1, {"page": 1})
    cache.save_json("ocr", 2, {"page": 2})
    cache.save_text("draft", 1, "page tex")

    cache.invalidate("ocr", 1, ".json")

    assert cache.load_json("ocr", 1) is None
    assert cache.load_json("ocr", 2) == {"page": 2}
    assert cache.load_text("draft", 1) == "page tex"
