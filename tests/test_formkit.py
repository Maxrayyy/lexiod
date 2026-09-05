import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from formkit import merge as M  # noqa: E402
from formkit import schema as S  # noqa: E402
from formkit.review import build_html  # noqa: E402


# ---------- field_id 稳定性：这是入库正确性的地基 ----------

def test_field_id_stable_across_runs():
    """同样的 (页码, label 序列) 必须产出同样的 id —— 否则重跑会污染数据库。"""
    def run():
        taken = {}
        return [S.make_field_id(1, l, taken) for l in ["样品名称", "样品代码", "样品名称"]]
    assert run() == run()


def test_field_id_collision_suffix():
    taken = {}
    ids = [S.make_field_id(3, "检验项目", taken) for _ in range(3)]
    assert ids == ["p03.检验项目", "p03.检验项目.2", "p03.检验项目.3"]


def test_field_id_independent_of_value():
    """id 不能依赖 value —— 人工改了值之后 id 必须不变。"""
    a, b = {}, {}
    assert S.make_field_id(2, "样品批号", a) == S.make_field_id(2, "样品批号 ", b)


def test_normalize_label_fullwidth_and_punct():
    assert S.normalize_label("样品名称：") == "样品名称"
    assert S.normalize_label("ＡＢＣ  123") == "abc123"
    assert S.normalize_label("") == "unnamed"
    assert S.normalize_label("   ") == "unnamed"


# ---------- bbox 坐标转换：错了不报错，只是裁歪，必须测 ----------

def test_gemini_box_order_and_scale():
    # Gemini: [ymin, xmin, ymax, xmax] 0..1000
    assert S.gemini_box_to_bbox([100, 200, 300, 800]) == [0.2, 0.1, 0.8, 0.3]


def test_gemini_box_invalid():
    assert S.gemini_box_to_bbox(None) is None
    assert S.gemini_box_to_bbox([1, 2, 3]) is None
    assert S.gemini_box_to_bbox(["a", "b", "c", "d"]) is None


def test_clamp_bbox_swaps_and_clips():
    assert S.clamp_bbox([0.9, 0.9, 0.1, 0.1]) == [0.1, 0.1, 0.9, 0.9]
    assert S.clamp_bbox([-0.5, -0.5, 2.0, 2.0]) == [0.0, 0.0, 1.0, 1.0]


def test_clamp_bbox_rejects_degenerate():
    assert S.clamp_bbox([0.5, 0.5, 0.5, 0.5]) is None
    assert S.clamp_bbox([0.5, 0.1, 0.50001, 0.9]) is None
    assert S.clamp_bbox(float("nan") and None) is None


# ---------- 复核判定 ----------

def _f(**kw):
    base = dict(field_id="p01.x", page=1, seq=1, label="x", label_norm="x",
                value="v", confidence=0.9, bbox=[0.1, 0.1, 0.2, 0.2])
    base.update(kw)
    return S.Field(**base)


def test_illegible_text_forces_review():
    """模型写"难以辨认"必须被拦下 —— 这正是当前 LaTeX 输出的失败模式。"""
    need, why = S.decide_review(_f(value="（手写内容，部分难以辨认）"), 0.75)
    assert need and "无法辨认" in why


@pytest.mark.parametrize("v", ["illegible", "[handwritten]", "无法辨认", "看不清"])
def test_illegible_variants(v):
    assert S.decide_review(_f(value=v), 0.75)[0]


def test_low_confidence_and_handwritten():
    assert S.decide_review(_f(confidence=0.3), 0.75)[0]
    assert S.decide_review(_f(is_handwritten=True), 0.75)[0]


def test_missing_bbox_flagged():
    need, why = S.decide_review(_f(bbox=None), 0.75)
    assert need and "bbox" in why


def test_empty_value_flagged_but_checkbox_exempt():
    assert S.decide_review(_f(value=""), 0.75)[0]
    ok, _ = S.decide_review(
        _f(value="", type="checkbox_group",
           options=[S.Option("初验", True)], confidence=0.95), 0.75)
    assert not ok


def test_clean_field_not_flagged():
    assert not S.decide_review(_f(), 0.75)[0]


# ---------- merge ----------

FIELDS = {
    "schema_version": "1.0", "doc_id": "abc123", "source_pdf": "p.pdf",
    "created_at": "2026-01-01T00:00:00Z", "model": "gemini-2.5-flash",
    "page_count": 1, "pages": [{"page": 1, "form_code": "REC-1", "form_title": "样品请验单",
                                "width": 1000, "height": 1400}],
    "fields": [
        {"field_id": "p01.样品名称", "page": 1, "seq": 1, "label": "样品名称",
         "label_norm": "样品名称", "type": "text", "value": "旧值", "options": [],
         "bbox": [0.1, 0.1, 0.5, 0.15], "is_handwritten": True, "confidence": 0.3,
         "needs_review": True, "review_reason": "手写", "source": "vlm"},
        {"field_id": "p01.检验类型", "page": 1, "seq": 2, "label": "检验类型",
         "label_norm": "检验类型", "type": "checkbox_group", "value": "",
         "options": [{"label": "初验", "checked": True}, {"label": "复验", "checked": False}],
         "bbox": [0.1, 0.2, 0.9, 0.25], "is_handwritten": False, "confidence": 0.95,
         "needs_review": False, "review_reason": "", "source": "vlm"},
    ],
}

VALUES = {
    "schema_version": "1.0", "doc_id": "abc123", "reviewed_at": "2026-01-02T00:00:00Z",
    "reviewer": "lanyu",
    "values": {
        "p01.样品名称": {"value": "新值A", "options": {}, "confirmed": True,
                       "note": "字迹被印章遮挡", "edited": True},
        "p01.检验类型": {"value": "", "options": {"初验": False, "复验": True},
                       "confirmed": True, "note": "", "edited": True},
    },
}


def test_merge_applies_human_value_and_marks_source():
    out = M.merge(FIELDS, VALUES)
    r = {x["field_id"]: x for x in out["records"]}
    assert r["p01.样品名称"]["value"] == "新值A"
    assert r["p01.样品名称"]["source"] == "human"
    assert r["p01.样品名称"]["note"] == "字迹被印章遮挡"
    # 溯源信息必须保留
    assert r["p01.样品名称"]["bbox"] == [0.1, 0.1, 0.5, 0.15]
    assert r["p01.样品名称"]["confidence"] == 0.3


def test_merge_checkbox_options_updated():
    out = M.merge(FIELDS, VALUES)
    r = {x["field_id"]: x for x in out["records"]}
    opts = {o["label"]: o["checked"] for o in r["p01.检验类型"]["value_json"]}
    assert opts == {"初验": False, "复验": True}


def test_merge_flat_view():
    out = M.merge(FIELDS, VALUES)
    assert out["flat"]["page_01"]["样品名称"] == "新值A"
    assert out["flat"]["page_01"]["检验类型"] == {"初验": False, "复验": True}


def test_merge_stats():
    s = M.merge(FIELDS, VALUES)["stats"]
    assert s == {"fields_total": 2, "fields_edited": 2,
                 "fields_confirmed": 2, "fields_unreviewed": 0}


def test_merge_rejects_doc_id_mismatch():
    bad = dict(VALUES, doc_id="zzz")
    with pytest.raises(M.MergeError, match="doc_id 不匹配"):
        M.merge(FIELDS, bad)


def test_merge_rejects_orphan_field_ids():
    bad = dict(VALUES, values=dict(VALUES["values"], **{"p09.幽灵": {"value": "x"}}))
    with pytest.raises(M.MergeError, match="不在 fields.json"):
        M.merge(FIELDS, bad)
    out = M.merge(FIELDS, bad, strict=False)   # lenient 模式应放行
    assert out["stats"]["fields_total"] == 2


def test_merge_partial_review_keeps_model_value():
    partial = dict(VALUES, values={"p01.样品名称": VALUES["values"]["p01.样品名称"]})
    out = M.merge(FIELDS, partial)
    r = {x["field_id"]: x for x in out["records"]}
    assert r["p01.检验类型"]["value_json"][0]["checked"] is True   # 回落到模型原值
    assert out["stats"]["fields_unreviewed"] == 1


# ---------- HTML 生成 ----------

def test_build_html_escapes_label_markup():
    doc = json.loads(json.dumps(FIELDS))
    doc["fields"][0]["label"] = '<img src=x onerror=alert(1)>'
    doc["fields"][0]["crop_data_uri"] = "data:image/jpeg;base64,AAAA"
    html = build_html(doc)
    assert "<img src=x" not in html      # 不得出现未转义的标签
    assert "&lt;img src=x" in html
    assert "data:image/jpeg;base64,AAAA" in html
    assert "p01.检验类型" in html


def test_build_html_label_cannot_break_out_of_script():
    """OCR 从表单上读出 '</script>' 就会提前闭合脚本块 —— 必须转义。"""
    doc = json.loads(json.dumps(FIELDS))
    doc["fields"][0]["label"] = '</script><script>alert(1)</script>'
    html = build_html(doc)
    # 页面自身只应有两个 <script> 开标签（DOC 定义 + 主逻辑）
    assert html.count("<script>") == 2
    assert html.count("</script>") == 2
    assert "\\u003c/script" in html


def test_build_html_self_contained_no_external_hosts():
    doc = json.loads(json.dumps(FIELDS))
    html = build_html(doc)
    for proto in ("http://", "https://", "//cdn"):
        assert proto not in html


def test_build_html_renders_checkbox_options():
    doc = json.loads(json.dumps(FIELDS))
    html = build_html(doc)
    assert 'data-opt="初验"' in html and 'data-opt="复验"' in html


def test_build_html_without_bbox_shows_fallback():
    doc = json.loads(json.dumps(FIELDS))
    doc["fields"][0]["crop_data_uri"] = ""
    assert "无 bbox" in build_html(doc)


# ---------- 回归：fixture 暴露出的两个缺陷 ----------

@pytest.mark.parametrize("v", [
    "handwritten value 00",     # 正文里合法出现 handwritten，不该误报
    "signature required by SOP",
    "unclear weather data",
])
def test_bare_english_words_are_not_placeholders(v):
    need, why = S.decide_review(_f(value=v), 0.75)
    assert not need, f"误报：{v} -> {why}"


@pytest.mark.parametrize("v", [
    "[handwritten signature]", "（handwritten）", "【illegible】", "(blank)",
])
def test_bracketed_placeholders_are_caught(v):
    assert S.decide_review(_f(value=v), 0.75)[0]


def test_oversized_bbox_flagged():
    """模型返回覆盖整页的框 = 没定位到，裁图无意义，必须提醒人工。"""
    need, why = S.decide_review(_f(bbox=[0.0, 0.0, 1.0, 1.0]), 0.75)
    assert need and "定位不可信" in why


def test_normal_bbox_not_flagged_by_area():
    assert not S.decide_review(_f(bbox=[0.29, 0.10, 0.70, 0.17]), 0.75)[0]


def test_bbox_area():
    assert S.bbox_area([0.0, 0.0, 0.5, 0.5]) == pytest.approx(0.25)
    assert S.bbox_area(None) == 0.0
