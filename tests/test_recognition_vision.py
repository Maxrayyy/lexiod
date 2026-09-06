import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest

from lexoid.core.recognition.models import OcrBlock, PageEvidence, RenderedPage, RenderMetadata
from lexoid.core.recognition.vision import (
    VisionLatexAdapter, compact_evidence, fallback_page_latex,
    normalize_field_annotation_spacing, output_token_budget, validate_page_latex,
)


def evidence(text="Batch", page=1):
    return PageEvidence("recognition/v1", page, RenderMetadata(240, 300, 400),
                        ocr_blocks=(OcrBlock(text, (10, 20, 100, 50), 0.98),))


def reply(page=1):
    return {"latex": f"% #VALUE_ID: LEX-P{page:04d}-V0001\n"
            + "% #FIELD_VALUE: Batch\n\\fieldvalue{A12}\n"
            + f"% LEXOID_PAGE_COMPLETED: {page}/3",
            "fields": [{"field_id": f"LEX-P{page:04d}-V0001", "label": "Batch",
                        "bbox": [10, 20, 100, 50], "model_guess": "A12",
                        "confidence": 0.92, "needs_review": False}]}


def test_compact_metadata_derives_values_and_labels_from_tex():
    payload = reply()
    payload.pop("fields")
    payload["field_meta"] = {"V0001": [[10, 20, 100, 50], 0.92, False]}
    payload["latex"] = payload["latex"].replace(
        r"\fieldvalue{A12}", r"\underline{\makebox[2cm]{\fieldvalue{\handwritten{A12}}}}")
    page = RenderedPage(1, 240, 300, 400, Image.new("RGB", (300, 400)))
    result = VisionLatexAdapter("test", response_factory=lambda **kw: {
        "response": json.dumps(payload)}).recognize(page, evidence("A12"), 3)
    assert result.fields[0].label == "Batch"
    assert result.fields[0].value == "A12"
    assert result.fields[0].bbox == (10, 20, 100, 50)
    assert result.latex == payload["latex"]


@pytest.mark.parametrize("metadata", [
    {}, {"V0002": [[10, 20, 100, 50], 0.9, False]},
    {"V0001": [[10, 20, 100, 50]]},
])
def test_compact_metadata_does_not_shift_missing_field_associations(metadata):
    payload = reply()
    payload.pop("fields")
    payload["field_meta"] = metadata
    page = RenderedPage(1, 240, 300, 400, Image.new("RGB", (300, 400)))
    with pytest.raises(ValueError):
        VisionLatexAdapter("test", response_factory=lambda **kw: {
            "response": json.dumps(payload)}).recognize(page, evidence(), 3)


@pytest.mark.parametrize("latex, expected", [
    (r"\handwritten{\checkboxfield{checked}}", "checked"),
    (r"\handwritten{$\checkmark$}", "\u2713"),
    (r"\handwritten{\textbf{A\&B}}", "A&B"),
    (r"\handwritten{$10^{2}$}", "10^2"),
])
def test_compact_values_preserve_symbols_and_formatting(latex, expected):
    from lexoid.core.recognition.vision import expand_field_metadata
    payload = reply()
    payload["latex"] = payload["latex"].replace("{A12}", "{" + latex + "}")
    payload["field_meta"] = {"V0001": [[10, 20, 100, 50], 0.9, False]}
    assert expand_field_metadata(payload, 1)["fields"][0]["model_guess"] == expected


def test_compact_ids_bind_metadata_even_when_json_order_changes():
    from lexoid.core.recognition.vision import expand_field_metadata
    payload = reply()
    payload["latex"] += "\n% #VALUE_ID: LEX-P0001-V0002\n% #FIELD_VALUE: Other\n\\fieldvalue{B}"
    payload["field_meta"] = {"V0002": [[110, 20, 200, 50], 0.8, True],
                             "V0001": [[10, 20, 100, 50], 0.9, False]}
    fields = expand_field_metadata(payload, 1)["fields"]
    assert fields[0]["bbox"] == [10, 20, 100, 50]
    assert fields[1]["model_guess"] == "B"
    assert fields[1]["needs_review"] is True


def test_budget_grows_with_evidence_and_stays_bounded():
    assert 2048 <= output_token_budget(evidence("a" * 300)) < output_token_budget(
        evidence("a" * 5000)) <= 8192


def test_compact_evidence_remains_valid_json_under_budget():
    text = compact_evidence(evidence("a" * 30000), max_chars=1000)
    assert len(text) <= 1000
    assert json.loads(text)["page"] == 1


def test_vision_keeps_field_contract_and_source_image():
    calls = []
    def respond(**kwargs):
        calls.append(kwargs)
        return {"response": json.dumps(reply())}
    page = RenderedPage(1, 240, 300, 400, Image.new("RGB", (300, 400)))
    adapter = VisionLatexAdapter("gpt-6-astra", response_factory=respond)
    result = adapter.recognize(page, evidence("A12"), page_count=3)
    assert result.fields[0].bbox == (10, 20, 100, 50)
    assert result.fields[0].paddle_text == "A12"
    assert calls[0]["image_url"].startswith("data:image/png;base64,")
    assert calls[0]["max_tokens"] >= 2048
    assert calls[0]["model"] == "gpt-6-astra"


@pytest.mark.parametrize("change", ["missing_marker", "duplicate", "wrong_page", "empty", "missing_field"])
def test_invalid_pages_are_never_accepted(change):
    payload = reply()
    if change == "missing_marker":
        payload["latex"] = payload["latex"].split("% LEXOID_PAGE_COMPLETED")[0]
    elif change == "duplicate":
        payload["latex"] += "\n% #VALUE_ID: LEX-P0001-V0001"
    elif change == "wrong_page":
        payload = reply(page=2)
    elif change == "empty":
        payload["latex"] = ""
    else:
        payload["fields"] = []
    with pytest.raises(ValueError):
        validate_page_latex(payload["latex"], payload["fields"], 1, 3)


def test_draft_rejects_json_values_that_differ_from_tex():
    payload = reply()
    payload["fields"][0]["model_guess"] = "Wrong batch"
    with pytest.raises(ValueError, match="value"):
        validate_page_latex(payload["latex"], payload["fields"], 1, 3)


def test_draft_accepts_equivalent_scientific_notation():
    payload = reply()
    payload["latex"] = payload["latex"].replace("{A12}", r"{$6.0\times10^{10}$}")
    payload["fields"][0]["model_guess"] = "6.0\u00d710\u00b9\u2070"
    validate_page_latex(payload["latex"], payload["fields"], 1, 3)


def test_handwritten_value_mismatch_uses_visible_tex_without_retry():
    calls = []
    payload = reply()
    payload["latex"] = payload["latex"].replace(
        r"\fieldvalue{A12}", r"\fieldvalue{\handwritten{A12}}"
    )
    payload["fields"][0]["model_guess"] = "A17"

    def respond(**kwargs):
        calls.append(kwargs)
        return {"response": json.dumps(payload)}

    page = RenderedPage(1, 240, 300, 400, Image.new("RGB", (300, 400)))
    result = VisionLatexAdapter("gpt-6-astra", response_factory=respond).recognize(
        page, evidence("A12"), page_count=3
    )

    assert len(calls) == 1
    assert result.fields[0].value == "A12"
    assert result.fields[0].model_guess == "A12"


def test_empty_response_fallback_compiles_with_ocr_text(tmp_path):
    if not shutil.which("xelatex"):
        pytest.skip("xelatex is not installed")
    source = Path(tmp_path) / "fallback.tex"
    source.write_text(fallback_page_latex(1, evidence("Batch A_12"), 1), "utf-8")

    completed = subprocess.run(
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", source.name],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )

    assert completed.returncode == 0, completed.stdout
    assert source.with_suffix(".pdf").is_file()


def test_hybrid_prompt_wraps_standalone_handwriting_as_a_field():
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return {"response": json.dumps(reply(page=2))}

    page = RenderedPage(2, 240, 300, 400, Image.new("RGB", (300, 400)))
    VisionLatexAdapter("gpt-6-astra", response_factory=respond).recognize(
        page, evidence(page=2), page_count=3)
    standalone = next(line for line in calls[0]["system_prompt"].splitlines()
                      if "For standalone handwriting" in line)
    assert r"\fieldvalue{\handwritten{...}}" in standalone


def test_inline_field_annotations_do_not_create_paragraph_breaks():
    latex = (
        "\\noindent 测试日期：\n\n"
        "% #VALUE_ID: LEX-P0001-V0001\n"
        "% #FIELD_VALUE: 年\n"
        "\\fieldvalue{2023}年\n\n"
        "% #VALUE_ID: LEX-P0001-V0002\n"
        "% #FIELD_VALUE: 月\n"
        "\\fieldvalue{07}月\\\\\n"
        "下一行：\n\n"
        "% #VALUE_ID: LEX-P0001-V0003\n"
        "% #FIELD_VALUE: 批号\n"
        "\\fieldvalue{A31P}\n"
    )

    normalized = normalize_field_annotation_spacing(latex)

    assert "测试日期：\n% #VALUE_ID" in normalized
    assert "年\n% #VALUE_ID" in normalized
    assert "月\\\\\n下一行：\n% #VALUE_ID" in normalized
    assert "\n\n% #VALUE_ID" not in normalized


def test_page_rejects_numbered_section_commands_that_invent_layout():
    latex = (
        r"\section{Form title}" "\n"
        "% LEXOID_PAGE_COMPLETED: 2/3"
    )
    with pytest.raises(ValueError, match="numbered section"):
        validate_page_latex(latex, [], 2, 3)


def test_page_rejects_consecutive_tables_in_the_same_paragraph():
    latex = (
        r"\begin{tabular}{ll}A&B\\\end{tabular}" "\n"
        "% continuation table\n"
        r"\begin{tabular}{ll}C&D\\\end{tabular}" "\n"
        "% LEXOID_PAGE_COMPLETED: 2/3"
    )
    with pytest.raises(ValueError, match="consecutive tables"):
        validate_page_latex(latex, [], 2, 3)


def test_page_rejects_visual_row_split_across_latex_table_rows():
    latex = (
        r"\begin{tabular}{ll}" "\n"
        "% #VALUE_ID: LEX-P0002-V0001\n"
        "% #FIELD_VALUE: product\n"
        r"\fieldvalue{A} & fixed \\" "\n"
        "% #VALUE_ID: LEX-P0002-V0002\n"
        "% #FIELD_VALUE: quantity\n"
        r"\fieldvalue{25} & fixed \\" "\n"
        r"\end{tabular}" "\n"
        "% LEXOID_PAGE_COMPLETED: 2/3"
    )
    fields = [
        {"field_id": "LEX-P0002-V0001", "model_guess": "A",
         "bbox": [10, 100, 40, 130]},
        {"field_id": "LEX-P0002-V0002", "model_guess": "25",
         "bbox": [80, 102, 110, 132]},
    ]
    with pytest.raises(ValueError, match="visual row"):
        validate_page_latex(latex, fields, 2, 3)


def test_page_accepts_visual_row_kept_in_one_latex_table_row():
    latex = (
        r"\begin{tabular}{ll}" "\n"
        "% #VALUE_ID: LEX-P0002-V0001\n"
        "% #FIELD_VALUE: product\n"
        r"\fieldvalue{A} &" "\n"
        "% #VALUE_ID: LEX-P0002-V0002\n"
        "% #FIELD_VALUE: quantity\n"
        r"\fieldvalue{25} \\" "\n"
        r"\end{tabular}" "\n"
        "% LEXOID_PAGE_COMPLETED: 2/3"
    )
    fields = [
        {"field_id": "LEX-P0002-V0001", "model_guess": "A",
         "bbox": [10, 100, 40, 130]},
        {"field_id": "LEX-P0002-V0002", "model_guess": "25",
         "bbox": [80, 102, 110, 132]},
    ]
    validate_page_latex(latex, fields, 2, 3)
