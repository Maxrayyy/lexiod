"""CLI: python -m formkit extract|review|merge"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

_KEYS = {
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
}


def _p(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_env() -> None:
    """从仓库根目录的 .env 读取 API key。

    formkit 通常直接在宿主机上跑（不进 Docker），此时没有 compose 的 env_file
    帮忙注入，必须自己读。已存在的环境变量优先，方便临时覆盖。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, ".env"), ".env"):
        if os.path.isfile(candidate):
            load_dotenv(candidate, override=False)
            return


def _require_key(api: Optional[str], model: str) -> None:
    """在渲染 PDF、烧掉几十秒和一堆 token 之前，先把缺 key 的情况拦下来。"""
    from .llm import guess_provider

    provider = (api or guess_provider(model)).lower()
    needed = _KEYS.get(provider, ())
    if needed and not any(os.environ.get(k) for k in needed):
        names = " 或 ".join(needed)
        raise SystemExit(
            f"✗ 缺少 {names}（provider={provider}）。\n"
            "  请在仓库根目录的 .env 里设置，或 export 到环境变量。"
        )


def cmd_extract(a) -> int:
    from .extract import extract, write_fields_json
    from lexoid.core.model_config import resolve_model

    try:
        a.model = resolve_model("FORMKIT_MODEL", a.model)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _require_key(a.api, a.model)
    r = extract(a.input, model=a.model, api=a.api, threshold=a.threshold,
                reread=not a.no_reread, max_workers=a.workers, dpi=a.dpi, progress=_p)
    write_fields_json(r, a.output)
    _p(f"✓ 写入 {a.output}")
    if r["failed_pages"]:
        _p(f"⚠ {len(r['failed_pages'])} 页抽取失败，见 fields.json 的 failed_pages")
        return 2
    return 0


def cmd_review(a) -> int:
    from .merge import load
    from .review import build_review

    doc = load(a.fields)
    html = build_review(doc, a.pdf, dpi=a.dpi, progress=_p)
    with open(a.output, "w", encoding="utf-8") as fh:
        fh.write(html)
    mb = len(html.encode()) / 1024 / 1024
    _p(f"✓ 写入 {a.output}（{mb:.1f} MB）")
    _p("  用浏览器打开它，校对完点右上角「导出 values.json」")
    return 0


def cmd_merge(a) -> int:
    from .merge import MergeError, dump, load, merge

    try:
        out = merge(load(a.fields), load(a.values), strict=not a.lenient)
    except MergeError as e:
        _p(f"✗ {e}")
        return 1
    dump(out, a.output)
    s = out["stats"]
    _p(f"✓ 写入 {a.output}")
    _p(f"  共 {s['fields_total']} 字段 / 人工改过 {s['fields_edited']} / "
       f"已确认 {s['fields_confirmed']} / 未校对 {s['fields_unreviewed']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="formkit", description="表单抽取 → 人工校对 → 入库 JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="PDF → fields.json")
    e.add_argument("input")
    e.add_argument("-o", "--output", default="fields.json")
    e.add_argument("-m", "--model", default=None,
                   help="默认读取环境变量 FORMKIT_MODEL")
    e.add_argument("--api", default=None, choices=["gemini", "openai", "anthropic"])
    e.add_argument("--threshold", type=float, default=0.75,
                   help="低于此置信度进人工复核队列（默认 0.75）")
    e.add_argument("--no-reread", action="store_true", help="跳过手写重识别第二遍")
    e.add_argument("--workers", type=int, default=4)
    e.add_argument("--dpi", type=int, default=200)
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("review", help="fields.json + PDF → review.html")
    r.add_argument("fields")
    r.add_argument("pdf")
    r.add_argument("-o", "--output", default="review.html")
    r.add_argument("--dpi", type=int, default=200)
    r.set_defaults(func=cmd_review)

    g = sub.add_parser("merge", help="fields.json + values.json → final.json")
    g.add_argument("fields")
    g.add_argument("values")
    g.add_argument("-o", "--output", default="final.json")
    g.add_argument("--lenient", action="store_true",
                   help="容忍 values.json 中的未知 field_id")
    g.set_defaults(func=cmd_merge)

    _load_env()
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
