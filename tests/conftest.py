import pytest


@pytest.fixture(autouse=True)
def lexoid_model_settings(monkeypatch):
    for variable, model in {
        "LEXOID_MODEL": "gpt-test-vision",
        "LEXOID_SCHEMA_MODEL": "gpt-test-schema",
        "DEFAULT_LLM": "gpt-test-parser",
        "FORMKIT_MODEL": "gpt-test-formkit",
        "DEFAULT_LOCAL_LM": "test/local-model",
        "PADDLE_LAYOUT_MODEL": "PP-DocLayout_plus-L",
        "LEXOID_CLIP_MODEL": "test/clip-model",
    }.items():
        monkeypatch.setenv(variable, model)
