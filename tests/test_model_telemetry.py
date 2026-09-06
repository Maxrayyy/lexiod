import json

import pytest

from lexoid.core.model_telemetry import call_context, trace_response


def test_usage_and_retry_context_are_logged_without_content(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("LEXOID_MODEL_CALL_LOG", str(log))

    @trace_response
    def call(model, prompt):
        return {"response": "private response", "usage": {
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            "usage_raw": {"completion_tokens_details": {"reasoning_tokens": 20}}}

    with call_context(stage="recognize", page=20, attempt=2):
        call("test", "private prompt")
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["event"] for e in events] == ["start", "finish"]
    end = events[-1]
    assert end["retry_count"] == 1
    assert end["page"] == 20
    assert end["usage"]["total_tokens"] == 150
    assert end["usage_raw"]["completion_tokens_details"]["reasoning_tokens"] == 20
    assert end["seconds"] >= 0
    assert "private" not in log.read_text()


def test_failed_call_keeps_usage_unknown_and_does_not_leak_error(capsys):
    @trace_response
    def call(model):
        raise RuntimeError("secret URL or credential")

    with pytest.raises(RuntimeError):
        call("test")
    output = capsys.readouterr().err
    event = json.loads(output.splitlines()[-1].removeprefix("[LLM_CALL] "))
    assert event["status"] == "error"
    assert event["usage"] is None
    assert "secret" not in output


def test_missing_provider_usage_is_not_reported_as_zero(capsys):
    @trace_response
    def call(model):
        return {"response": "ok", "usage_missing": True,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}

    call("test")
    event = json.loads(capsys.readouterr().err.splitlines()[-1].removeprefix("[LLM_CALL] "))
    assert event["status"] == "ok"
    assert event["usage"] is None
