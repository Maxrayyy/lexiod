from pathlib import Path

from lexoid.cli import write_latex_page


def check_end_document_is_only_written_on_final_page(directory: Path):
    output = directory / "multi-page.tex"
    write_latex_page(
        output,
        1,
        2,
        "\\documentclass{article}\n\\begin{document}\nPage 1\n\\end{document}",
    )
    assert "\\end{document}" not in output.read_text(encoding="utf-8")

    write_latex_page(
        output,
        2,
        2,
        "\\newpage\nPage 2\n\\end{document}\n\\end{document}",
    )
    result = output.read_text(encoding="utf-8")
    assert result.count("\\end{document}") == 1
    assert result.index("Page 2") < result.index("\\end{document}")


def test_end_document_is_only_written_on_final_page(tmp_path):
    check_end_document_is_only_written_on_final_page(tmp_path)
