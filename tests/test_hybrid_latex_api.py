from types import SimpleNamespace

from PIL import Image
import pytest

from lexoid.api import parse_to_latex
from lexoid.core.recognition.models import PageEvidence, PageRecognitionResult, RenderMetadata


def test_first_page_prompt_uses_inline_heading_without_title_page_commands():
    from lexoid.core.prompt_templates import LATEX_FIRST_PAGE_PROMPT

    assert r"\maketitle" not in LATEX_FIRST_PAGE_PROMPT
    assert r"\begin{center}" in LATEX_FIRST_PAGE_PROMPT


@pytest.mark.parametrize("ocr", ["paddleocr", "none"])
def test_hybrid_api_writes_complete_evidence_atomically(tmp_path, monkeypatch, ocr):
    import json
    from lexoid.core.recognition import service

    calls = []
    class Recognizer:
        document_sha256 = "a" * 64

        def __init__(self, **kwargs):
            calls.append(kwargs)

        def recognize(self, path, **kwargs):
            ev = PageEvidence("recognition/v1", 1, RenderMetadata(240, 100, 200))
            return [PageRecognitionResult(1, "page", ev, "cache")]

    monkeypatch.setattr(service, "DocumentRecognizer", Recognizer)
    output = tmp_path / "page.recognition.json"
    result = parse_to_latex("input.pdf", ocr=ocr, evidence_output=str(output),
                            cache_dir=str(tmp_path / "cache"), vision_concurrency=2)
    assert result == "page"
    assert calls[0]["config"].vision_concurrency == 2
    assert calls[0]["config"].ocr == ocr
    if ocr == "none":
        assert calls[0]["config"].min_output_tokens == 8192
        assert calls[0]["config"].enable_vl_fallback is False
    assert json.loads(output.read_text())["pages"][0]["page"] == 1


def test_hybrid_cli_forwards_settings(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from lexoid import cli

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    seen = {}
    def convert(path, **kwargs):
        seen.update(kwargs)
        return "tex"
    monkeypatch.setattr(cli, "api_parse_to_latex", convert)
    monkeypatch.setattr(cli, "ensure_api_key", lambda *args, **kwargs: True)
    result = CliRunner().invoke(cli.app, ["latex", "-i", str(source),
        "-o", str(tmp_path / "raw.tex"), "--ocr", "paddleocr", "--render-dpi", "300",
        "--vision-concurrency", "2", "--no-resume"])
    assert result.exit_code == 0, result.output
    assert seen["render_dpi"] == 300
    assert seen["ocr"] == "paddleocr"
    assert seen["resume"] is False
    assert seen["evidence_output"].endswith("raw.recognition.json")


@pytest.mark.parametrize("indent", ["", "  "])
def test_evidence_cli_saves_each_recognized_page_checkpoint_once(tmp_path, monkeypatch, indent):
    from click.testing import CliRunner
    from lexoid import cli

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    output = tmp_path / "raw.tex"
    pages = [
        "\\documentclass{article}\n\\begin{document}\nfirst\n"
        f"{indent}% LEXOID_PAGE_COMPLETED: 1/2\n",
        "second\n\\end{document}\n"
        f"{indent}% LEXOID_PAGE_COMPLETED: 2/2\n",
    ]

    def convert(path, **kwargs):
        for number, page in enumerate(pages, 1):
            kwargs["page_callback"](number, 2, page)
        return "\n".join(pages)

    monkeypatch.setattr(cli, "api_parse_to_latex", convert)
    monkeypatch.setattr(cli, "ensure_api_key", lambda *args, **kwargs: True)
    result = CliRunner().invoke(cli.app, ["latex", "-i", str(source), "-o", str(output),
        "--ocr", "none", "--evidence-output", str(output.with_suffix(".recognition.json"))])
    assert result.exit_code == 0, result.output
    text = output.read_text()
    assert text.count("LEXOID_PAGE_COMPLETED:") == 2
    assert cli.get_latex_checkpoint(output) == (2, 2)
    assert text.count(r"\begin{document}") == text.count(r"\end{document}") == 1


def test_page_writer_rejects_a_checkpoint_for_another_page(tmp_path):
    import click
    from lexoid.cli import write_latex_page

    output = tmp_path / "raw.tex"
    output.write_text("existing content")
    with pytest.raises(click.ClickException, match="checkpoint"):
        write_latex_page(output, 1, 2,
            "\\begin{document}\nwrong page\n% LEXOID_PAGE_COMPLETED: 2/2\n")
    assert output.read_text() == "existing content"
