from copy import deepcopy

import pytest

from lexoid.core.latex_template import (
    LatexTemplateError,
    fill_latex,
    organize_latex,
)


ANNOTATED_TEX = r"""\documentclass{article}
\newcommand{\fieldvalue}[1]{#1}
\newcommand{\handwritten}[1]{#1}
\newcommand{\checkboxfield}[1]{#1}
\begin{document}
% #FIELD_VALUE: 姓名
% #HANDWRITTEN: 张三
\fieldvalue{\handwritten{张三}}
% #FIELD_VALUE: 产品批号
\fieldvalue{AB{12}-3}
% #FIELD_VALUE: 日期
% #TODO #HANDWRITTEN: 2026-08-24; 月份不清
\fieldvalue{\handwritten{2026-08-24}}
% #FIELD_VALUE: 合格
\fieldvalue{\checkboxfield{checked}} 合格
\end{document}
"""


def test_organize_and_fill_round_trip():
    template, manifest = organize_latex(ANNOTATED_TEX, "input.tex")

    assert manifest["value_count"] == 4
    assert [record["label"] for record in manifest["values"]] == [
        "姓名",
        "产品批号",
        "日期",
        "合格",
    ]
    assert manifest["values"][0]["is_handwritten"] is True
    assert manifest["values"][0]["needs_review"] is False
    assert manifest["values"][2]["needs_review"] is True
    assert manifest["values"][3]["field_type"] == "checkbox"
    assert manifest["values"][3]["value"] == "checked"

    edited = deepcopy(manifest)
    edited["values"][0]["value"] = "李四"
    filled = fill_latex(template, edited)

    assert r"\fieldvalue{\handwritten{李四}}" in filled
    assert r"\fieldvalue{AB{12}-3}" in filled
    assert r"\fieldvalue{\checkboxfield{checked}}" in filled
    assert "@@LEXOID_VALUE_" not in filled


def test_rejects_unannotated_latex():
    with pytest.raises(LatexTemplateError, match="No annotated"):
        organize_latex(r"\begin{document}plain text\end{document}")


def test_fill_rejects_missing_value():
    template, manifest = organize_latex(ANNOTATED_TEX)
    manifest["values"] = manifest["values"][:-1]
    with pytest.raises(LatexTemplateError, match="Missing value"):
        fill_latex(template, manifest)


def test_handwritten_checkbox_round_trip_preserves_both_wrappers():
    source = r"""\begin{document}
% #VALUE_ID: LEX-P0001-V0001
% #FIELD_VALUE: 是否合格
% #HANDWRITTEN: checked
\fieldvalue{\handwritten{\checkboxfield{checked}}} 合格
\end{document}
"""

    template, manifest = organize_latex(source)
    record = manifest["values"][0]

    assert record["value"] == "checked"
    assert record["field_type"] == "checkbox"
    assert record["is_handwritten"] is True
    assert (
        r"\fieldvalue{\handwritten{\checkboxfield{checked}}}"
        in fill_latex(template, manifest)
    )
