from lexoid.core.prompt_templates import (
    LATEX_CHECKBOX_FIELD_COMMAND, LATEX_COMMON_PROMPT,
    LATEX_FIRST_PAGE_PROMPT,
    latex_page_value_id_prompt,
)
from lexoid.core.latex_template import fill_latex, organize_latex


def test_common_latex_prompt_requires_one_stable_id_per_logical_value():
    assert "% #VALUE_ID:" in LATEX_COMMON_PROMPT
    assert "exactly one stable" in LATEX_COMMON_PROMPT
    assert "MUST share one ID" in LATEX_COMMON_PROMPT


def test_latex_prompt_prefers_synctex_safe_tables_and_checkbox_fields():
    assert "prefer the SyncTeX-safe `tabular`" in LATEX_COMMON_PROMPT
    assert "`tabularx` only" in LATEX_COMMON_PROMPT
    assert r"\fieldvalue{\checkboxfield{checked}}" in LATEX_COMMON_PROMPT
    assert r"\newcommand{\checkboxfield}" in LATEX_FIRST_PAGE_PROMPT
    assert r"\checkmark" in LATEX_CHECKBOX_FIELD_COMMAND
    assert r"\lexoidcheckboxbox{x}" not in LATEX_CHECKBOX_FIELD_COMMAND
    assert "Do not insert a blank line before field metadata" in LATEX_COMMON_PROMPT


def test_page_value_id_prompt_uses_physical_page_and_fixed_width_sequence():
    prompt = latex_page_value_id_prompt(12, 134)

    assert "physical PDF page 12 of 134" in prompt
    assert "LEX-P0012-V0001" in prompt
    assert "LEX-P0012-V####" in prompt
    assert "visual reading order" in prompt
    assert "Never place `% #VALUE_ID` at the end of a table" in prompt


def test_page_value_id_prefix_is_stable_when_resuming_same_page():
    first = latex_page_value_id_prompt(37, 134)
    resumed = latex_page_value_id_prompt(37, 134)

    assert first == resumed
    assert "LEX-P0037-V0001" in first


def test_prompt_keeps_form_rules_outside_editable_values():
    assert r"\underline{\makebox[2cm][c]{\fieldvalue{\handwritten{1632504}}}}" in LATEX_COMMON_PROMPT
    assert "Do not add underlines to fields without a visible source rule" in LATEX_COMMON_PROMPT


def test_filling_underlined_field_keeps_rule_width_and_units():
    source = (
        "% #VALUE_ID: LEX-P0001-V0001\n"
        "% #FIELD_VALUE: Incubator ID\n"
        "% #HANDWRITTEN: 1632504\n"
        r"\underline{\makebox[2cm][c]{\fieldvalue{\handwritten{1632504}}}}; 20-25 C"
    )
    template, manifest = organize_latex(source)
    assert manifest["values"][0]["value"] == "1632504"
    manifest["values"][0]["value"] = "1632501"
    assert fill_latex(template, manifest) == source.replace("{1632504}", "{1632501}")


def test_prompt_value_id_survives_template_organization_and_fill():
    source = r"""\documentclass{article}
\newcommand{\fieldvalue}[1]{#1}
\begin{document}
% #VALUE_ID: LEX-P0012-V0007
% #FIELD_VALUE: 批号
\fieldvalue{A3722205}
\end{document}
"""

    template, manifest = organize_latex(source, "input.tex")

    assert manifest["values"][0]["id"] == "LEX-P0012-V0007"
    assert "@@LEXOID_LEX-P0012-V0007@@" in template
    manifest["values"][0]["value"] = "A3722206"
    assert r"\fieldvalue{A3722206}" in fill_latex(template, manifest)
