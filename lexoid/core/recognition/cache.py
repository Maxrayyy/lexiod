"""Durable atomic artifacts for page-level recognition stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

from .models import RecognitionConfig


_CACHE_STAGES = frozenset({"render", "ocr", "tables", "draft", "reconciled"})
_CACHE_KEY = re.compile(r"[A-Za-z0-9._-]+\Z")


def build_cache_key(
    pdf_sha256: str,
    config: RecognitionConfig,
    paddle_versions: Mapping[str, str],
    prompt_version: str,
    vision_model: str,
) -> str:
    """Hash every input that can change recognition output."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", pdf_sha256):
        raise ValueError("pdf_sha256 must contain 64 hexadecimal characters")
    recognition_config = asdict(config)
    # Retry policy is not a model input. Preserve existing primary drafts when
    # the pipeline reserves its second attempt for a different model.
    recognition_config.pop("max_page_attempts")
    payload = {
        "pdf_sha256": pdf_sha256.lower(),
        "recognition_config": recognition_config,
        "paddle_versions": dict(sorted(paddle_versions.items())),
        "prompt_version": prompt_version,
        "vision_model": vision_model,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class RecognitionCache:
    """Read and atomically publish artifacts below one recognition cache key."""

    def __init__(self, root: Path, key: str) -> None:
        if not _CACHE_KEY.fullmatch(key):
            raise ValueError("cache key contains unsupported characters")
        self.root = Path(root)
        self.key = key

    def path(self, stage: str, page: int, suffix: str) -> Path:
        if stage not in _CACHE_STAGES:
            raise ValueError(f"Unsupported cache stage: {stage}")
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError("cache page must be a positive integer")
        if not re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
            raise ValueError("cache suffix must be a simple file extension")
        return self.root / stage / self.key / f"{page}{suffix}"

    def load_json(self, stage: str, page: int) -> object | None:
        path = self.path(stage, page, ".json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def save_json(self, stage: str, page: int, payload: object) -> Path:
        path = self.path(stage, page, ".json")
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_write(path, encoded)
        return path

    def load_text(self, stage: str, page: int) -> str | None:
        path = self.path(stage, page, ".tex")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def save_text(self, stage: str, page: int, content: str) -> Path:
        if not isinstance(content, str):
            raise TypeError("cache text content must be a string")
        path = self.path(stage, page, ".tex")
        _atomic_write(path, content.encode("utf-8"))
        return path

    def invalidate(self, stage: str, page: int, suffix: str) -> None:
        self.path(stage, page, suffix).unlink(missing_ok=True)
