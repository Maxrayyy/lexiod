"""Turn annotated Lexoid LaTeX into an editable value template and fill it."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple


FIELD_COMMAND = r"\fieldvalue{"
HANDWRITTEN_COMMAND = r"\handwritten{"
CHECKBOX_COMMAND = r"\checkboxfield{"
PLACEHOLDER_RE = re.compile(r"@@LEXOID_([A-Za-z0-9_-]+)@@")
VALUE_ID_RE = re.compile(r"(?m)% #VALUE_ID:\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s*$")
FIELD_LABEL_RE = re.compile(r"(?m)% #FIELD_VALUE:\s*(.*?)\s*$")
TODO_HANDWRITTEN_RE = re.compile(r"(?m)% #TODO #HANDWRITTEN:")


class LatexTemplateError(ValueError):
    pass


def _balanced_argument(text: str, open_brace: int) -> Tuple[str, int]:
    """Return a braced argument and the index immediately after its closing brace."""
    depth = 0
    i = open_brace
    while i < len(text):
        char = text[i]
        escaped = i > 0 and text[i - 1] == "\\"
        if char == "{" and not escaped:
            depth += 1
        elif char == "}" and not escaped:
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : i], i + 1
        i += 1
    raise LatexTemplateError("Unclosed \\fieldvalue{...} argument")


def _unwrap_handwritten(value_latex: str) -> Tuple[str, bool]:
    stripped = value_latex.strip()
    if not stripped.startswith(HANDWRITTEN_COMMAND):
        return stripped, False
    inner, end = _balanced_argument(stripped, len(HANDWRITTEN_COMMAND) - 1)
    if stripped[end:].strip():
        return stripped, True
    return inner, True


def _unwrap_checkbox(value_latex: str) -> Tuple[str, bool]:
    stripped = value_latex.strip()
    if not stripped.startswith(CHECKBOX_COMMAND):
        return stripped, False
    inner, end = _balanced_argument(stripped, len(CHECKBOX_COMMAND) - 1)
    if stripped[end:].strip():
        return stripped, False
    return inner.strip(), True


def organize_latex(tex: str, source_name: str = "") -> Tuple[str, Dict]:
    """Replace annotated field values with stable tokens and return their manifest."""
    replacements: List[Tuple[int, int, str]] = []
    values: List[Dict] = []
    cursor = 0

    while True:
        start = tex.find(FIELD_COMMAND, cursor)
        if start < 0:
            break
        value_latex, end = _balanced_argument(tex, start + len(FIELD_COMMAND) - 1)
        context = tex[cursor:start]
        declared_ids = list(VALUE_ID_RE.finditer(context))
        value_id = (
            declared_ids[-1].group(1)
            if declared_ids
            else f"VALUE_{len(values) + 1:03d}"
        )
        if any(record["id"] == value_id for record in values):
            raise LatexTemplateError(f"Duplicate value ID: {value_id}")
        labels = list(FIELD_LABEL_RE.finditer(context))
        label = labels[-1].group(1).strip() if labels else ""
        after_last_label = context[labels[-1].end() :] if labels else context
        plain_value, handwritten = _unwrap_handwritten(value_latex)
        plain_value, checkbox = _unwrap_checkbox(plain_value)
        values.append(
            {
                "id": value_id,
                "label": label,
                "value": plain_value,
                "value_latex": value_latex.strip(),
                "is_handwritten": handwritten,
                "field_type": "checkbox" if checkbox else "text",
                "needs_review": bool(TODO_HANDWRITTEN_RE.search(after_last_label)),
            }
        )
        replacements.append((start, end, f"@@LEXOID_{value_id}@@"))
        cursor = end

    if not values:
        raise LatexTemplateError(
            "No annotated \\fieldvalue{...} values found. Rebuild Lexoid and "
            "regenerate the LaTeX with the field-value annotation prompt."
        )

    pieces = []
    cursor = 0
    for start, end, token in replacements:
        pieces.extend((tex[cursor:start], token))
        cursor = end
    pieces.append(tex[cursor:])
    manifest = {
        "schema_version": "1.0",
        "source_tex": source_name,
        "value_count": len(values),
        "values": values,
    }
    return "".join(pieces), manifest


def fill_latex(template: str, manifest: Dict) -> str:
    """Fill every template token from an edited values manifest."""
    records = manifest.get("values")
    if not isinstance(records, list):
        raise LatexTemplateError("values manifest must contain a values list")
    by_id = {record.get("id"): record for record in records if isinstance(record, dict)}

    def replace(match: re.Match) -> str:
        value_id = match.group(1)
        record = by_id.get(value_id)
        if record is None:
            raise LatexTemplateError(f"Missing value for placeholder {value_id}")
        value = str(record.get("value", ""))
        if record.get("field_type") == "checkbox":
            value = rf"\checkboxfield{{{value}}}"
        if record.get("is_handwritten"):
            value = rf"\handwritten{{{value}}}"
        return rf"\fieldvalue{{{value}}}"

    result = PLACEHOLDER_RE.sub(replace, template)
    remaining = PLACEHOLDER_RE.findall(result)
    if remaining:
        raise LatexTemplateError(f"Unfilled placeholders remain: {remaining[:5]}")
    return result


def organize_file(input_path: Path, template_path: Path, values_path: Path) -> int:
    template, manifest = organize_latex(
        input_path.read_text(encoding="utf-8"), input_path.name
    )
    template_path.write_text(template, encoding="utf-8")
    values_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest["value_count"]


def fill_file(template_path: Path, values_path: Path, output_path: Path) -> None:
    manifest = json.loads(values_path.read_text(encoding="utf-8"))
    output_path.write_text(
        fill_latex(template_path.read_text(encoding="utf-8"), manifest),
        encoding="utf-8",
    )
