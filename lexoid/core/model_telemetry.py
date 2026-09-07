"""Record provider usage without logging prompts, images or credentials."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import sys
import time
import uuid


CALL_CONTEXT = ContextVar("model_call_context", default={})


def emit(event):
    line = json.dumps(event, ensure_ascii=True)
    print("[LLM_CALL] " + line, file=sys.stderr, flush=True)
    path = os.getenv("LEXOID_MODEL_CALL_LOG")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            print("[LLM_CALL_LOG_ERROR] Unable to append model call log", file=sys.stderr)


@contextmanager
def call_context(**values):
    token = CALL_CONTEXT.set(values)
    try:
        yield
    finally:
        CALL_CONTEXT.reset(token)


def trace_response(function):
    signature = inspect.signature(function)

    @wraps(function)
    def traced(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        context = CALL_CONTEXT.get()
        attempt = context.get("attempt", 1)
        event = {"call_id": uuid.uuid4().hex, "stage": "recognize", **context,
                 "model": bound.arguments.get("model"), "attempt": attempt,
                 "max_tokens": bound.arguments.get("max_tokens"),
                 "retry_count": attempt - 1,
                 "started_at": datetime.now(timezone.utc).isoformat()}
        if bound.arguments.get("reasoning_effort") is not None:
            event["reasoning_effort"] = bound.arguments["reasoning_effort"]
        started = time.monotonic()
        emit({**event, "event": "start"})
        result = None
        error = None
        try:
            result = function(*args, **kwargs)
            return result
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            usage = None if (result or {}).get("usage_missing") else (result or {}).get("usage")
            emit({**event, "event": "finish", "seconds": time.monotonic() - started,
                  "finished_at": datetime.now(timezone.utc).isoformat(),
                  "status": "error" if error else "ok", "error_type": error,
                  "finish_reason": (result or {}).get("finish_reason"),
                  "response_id": (result or {}).get("response_id"),
                  "usage": usage, "usage_raw": (result or {}).get("usage_raw")})
    return traced
