import pytest

from lexoid import api


def test_latex_api_reads_environment_per_call(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "convert_doc_to_base64_images", lambda *a, **kw: [(0, "image")])
    def respond(**kwargs):
        calls.append(kwargs["model"])
        return {"response": "tex"}
    monkeypatch.setattr(api, "create_response", respond)
    for model in ("gpt-test-first", "gpt-test-second"):
        monkeypatch.setenv("LEXOID_MODEL", model)
        assert api.parse_to_latex("input.pdf") == "tex"
    api.parse_to_latex("input.pdf", model="gpt-explicit")
    assert calls == ["gpt-test-first", "gpt-test-second", "gpt-explicit"]


@pytest.mark.parametrize("value", [None, "", "  "])
def test_latex_requires_role_configuration_before_rendering(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LEXOID_MODEL", raising=False)
    else:
        monkeypatch.setenv("LEXOID_MODEL", value)
    monkeypatch.setenv("DEFAULT_LLM", "gpt-unrelated-role")
    with pytest.raises(ValueError, match="LEXOID_MODEL"):
        api.parse_to_latex("missing.pdf")


def test_latex_cli_uses_environment_and_allows_override(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from lexoid import cli

    source = tmp_path / "input.pdf"
    source.write_bytes(b"pdf")
    calls = []
    def convert(*args, **kwargs):
        calls.append(kwargs["model"])
        return "tex"
    monkeypatch.setattr(cli, "api_parse_to_latex", convert)
    monkeypatch.setattr(cli, "ensure_api_key", lambda *a, **kw: True)
    monkeypatch.setenv("LEXOID_MODEL", "gpt-test-cli")
    for extra in ([], ["--model", "gpt-explicit"]):
        result = CliRunner().invoke(cli.app, ["latex", "-i", str(source), *extra])
        assert result.exit_code == 0, result.output
    assert calls == ["gpt-test-cli", "gpt-explicit"]


def test_schema_api_uses_schema_role(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "convert_doc_to_base64_images", lambda *a, **kw: [(0, "image")])
    def respond(**kwargs):
        calls.append(kwargs["model"])
        return {"response": '{"field": "value"}'}
    monkeypatch.setattr(api, "create_response", respond)
    monkeypatch.setenv("LEXOID_MODEL", "gpt-vision")
    monkeypatch.setenv("LEXOID_SCHEMA_MODEL", "gpt-schema")
    api.parse_with_schema("input.pdf", {"type": "object"})
    api.parse_with_schema("input.pdf", {"type": "object"}, model="gpt-explicit")
    assert calls == ["gpt-schema", "gpt-explicit"]


def test_generic_parser_resolves_model_with_explicit_provider(monkeypatch):
    from lexoid.core.parse_type import llm_parser

    monkeypatch.setattr(llm_parser, "get_file_type", lambda path: "application/pdf")
    monkeypatch.setattr(llm_parser, "parse_with_api", lambda path, **kw: kw)
    monkeypatch.setenv("DEFAULT_LLM", "gpt-parser")
    assert llm_parser.parse_llm_doc("input.pdf", title="input", api_provider="openai")["model"] == "gpt-parser"
    assert llm_parser.parse_llm_doc("input.pdf", title="input", model="gpt-explicit")["model"] == "gpt-explicit"
