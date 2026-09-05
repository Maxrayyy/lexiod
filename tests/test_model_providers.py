from types import SimpleNamespace

import openai
import pytest

from lexoid.core.parse_type.llm_parser import create_response


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
