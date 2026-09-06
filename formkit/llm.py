"""视觉模型调用层。

刻意不复用 lexoid 的 create_response()：那个函数在 OpenAI 分支上会丢弃
temperature/max_tokens，也没有 JSON mode，而本模块**必须**拿到可解析的 JSON。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Dict, Optional

# 模型名 → provider 的粗粒度路由
_PROVIDER_HINTS = (
    ("gemini", "gemini"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude", "anthropic"),
)


class LLMError(RuntimeError):
    pass


def guess_provider(model: str) -> str:
    low = model.lower()
    for prefix, provider in _PROVIDER_HINTS:
        if low.startswith(prefix) or prefix in low:
            return provider
    raise LLMError(f"无法从模型名推断 provider，请显式传 --api：{model}")


def _split_data_uri(data_uri: str):
    """'data:image/jpeg;base64,xxx' -> ('image/jpeg', b'...')"""
    if not data_uri.startswith("data:"):
        raise LLMError("期望 data URI 格式的图片")
    header, b64 = data_uri.split(",", 1)
    mime = header.split(":", 1)[1].split(";", 1)[0]
    return mime, base64.b64decode(b64)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_loose(text: str) -> Dict[str, Any]:
    """尽最大努力从模型输出里抠出 JSON。

    即使开了 JSON mode，模型偶尔还是会加 markdown 围栏或前导说明。
    这里逐级降级，全部失败才抛错。
    """
    if not text or not text.strip():
        raise LLMError("模型返回空内容")
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    m = _FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 退到第一个 { 和最后一个 } 之间
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise LLMError(f"无法解析为 JSON，原始输出前 300 字符：{text[:300]}")


def _call_gemini(model: str, system: str, user: str, image_data_uri: str,
                 temperature: float, max_tokens: int) -> str:
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("缺少 GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    mime, raw = _split_data_uri(image_data_uri)

    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=raw, mime_type=mime),
            types.Part.from_text(text=user),
        ],
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        ),
    )
    return resp.text or ""


def _call_openai(model: str, system: str, user: str, image_data_uri: str,
                 temperature: float, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI()
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": user},
            ],
        },
    ]
    params: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    from lexoid.core.model_config import completion_options
    params.update(completion_options(model, max_tokens, temperature))
    try:
        resp = client.chat.completions.create(**params)
    except Exception as e:
        # GPT-5 系列部分模型不接受 response_format / temperature，退化重试一次
        if "response_format" in str(e) or "unsupported" in str(e).lower():
            params.pop("response_format", None)
            resp = client.chat.completions.create(**params)
        else:
            raise
    return resp.choices[0].message.content or ""


def _call_anthropic(model: str, system: str, user: str, image_data_uri: str,
                    temperature: float, max_tokens: int) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    mime, raw = _split_data_uri(image_data_uri)
    resp = client.messages.create(
        model=model,
        system=system,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": mime,
                                             "data": base64.b64encode(raw).decode()}},
                {"type": "text", "text": user},
            ],
        }],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


_DISPATCH = {"gemini": _call_gemini, "openai": _call_openai, "anthropic": _call_anthropic}


def vision_json(
    model: str,
    system: str,
    user: str,
    image_data_uri: str,
    api: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 8192,
    retries: int = 2,
) -> Dict[str, Any]:
    """调用视觉模型并返回解析好的 JSON dict。

    失败会重试（指数退避）。retries 次仍失败则抛 LLMError ——
    调用方负责决定这一页是跳过还是整体中止。
    """
    provider = (api or guess_provider(model)).lower()
    if provider not in _DISPATCH:
        raise LLMError(f"不支持的 provider: {provider}")

    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            text = _DISPATCH[provider](
                model, system, user, image_data_uri, temperature, max_tokens
            )
            return parse_json_loose(text)
        except Exception as e:  # noqa: BLE001 — 这里就是要兜住所有provider异常
            last = e
            if attempt < retries:
                time.sleep(1.5 * (2 ** attempt))
    raise LLMError(f"{provider}/{model} 调用失败（重试 {retries} 次）：{last}") from last
