from types import SimpleNamespace

import openai
import pytest

from lexoid.core.parse_type.llm_parser import create_response
from lexoid.core.model_telemetry import call_context


def test_page_calls_disable_hidden_sdk_retries_and_keep_raw_usage(monkeypatch):
    options = []
    raw = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
           "completion_tokens_details": {"reasoning_tokens": 10}}
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150,
                            model_dump=lambda: raw)

    def client(**kwargs):
        options.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(usage=usage, choices=[SimpleNamespace(
                message=SimpleNamespace(content="{}"), finish_reason="stop")]))))

    monkeypatch.setattr(openai, "OpenAI", client)
    with call_context(stage="recognize", page=1, attempt=1):
        result = create_response("openai", "test", user_prompt="page")
    assert options == [{"max_retries": 0}]
    assert result["usage_raw"] == raw


@pytest.mark.parametrize(
    "provider,key_var,base_var,base_url,model",
    [
        (
            "deepseek",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "https://deepseek.example/v1",
            "deepseek-v4-flash",
        ),
        (
            "minimax",
            "MINIMAX_API_KEY",
            "MINIMAX_BASE_URL",
            "https://minimax.example/v1",
            "MiniMax-M2.7",
        ),
    ],
)
def test_openai_compatible_provider_configuration(
    monkeypatch, provider, key_var, base_var, base_url, model
):
    clients = []

    class FakeCompletions:
        def create(self, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=2,
                    completion_tokens=3,
                    total_tokens=5,
                ),
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="response"))
                ],
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv(key_var, "test-key")
    monkeypatch.setenv(base_var, base_url)

    response = create_response(
        api=provider,
        model=model,
        system_prompt="system",
        user_prompt="user",
    )

    assert clients == [{"base_url": base_url, "api_key": "test-key"}]
    assert response["response"] == "response"
    assert response["usage"]["total_tokens"] == 5


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol"])
def test_reasoning_model_honors_token_budget_and_reports_truncation(monkeypatch, model):
    captured = {}
    def complete(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content="partial"), finish_reason="length")])
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete))))
    result = create_response("openai", model, user_prompt="page", max_tokens=8192)
    assert captured["max_completion_tokens"] == 8192
    assert "temperature" not in captured
    assert result["finish_reason"] == "length"
    assert result["usage_missing"] is True
    assert result["usage"]["total_tokens"] == 0
