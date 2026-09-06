"""Resolve workload models from the environment at the point of use."""

import os

from dotenv import load_dotenv

load_dotenv()


def resolve_model(variable: str, model: str | None = None) -> str:
    value = (model if model is not None else os.getenv(variable, "")).strip()
    if not value:
        raise ValueError(f"Set {variable} in .env/environment or pass an explicit model")
    return value


def completion_options(model, max_tokens, temperature=0.0):
    if model.lower().startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens, "temperature": temperature}
