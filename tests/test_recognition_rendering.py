from __future__ import annotations

from PIL import Image
import pytest

from lexoid.core.recognition.rendering import (
    _normalize_page_orientation,
    render_pdf_page,
    render_pdf_page_from_document,
)


class _RenderedPage:
    def __init__(self, image: Image.Image) -> None:
        self._image = image

    def to_pil(self) -> Image.Image:
        return self._image.copy()


class _Page:
    def __init__(self, image: Image.Image) -> None:
        self._image = image
        self.render_scale: float | None = None

    def render(self, scale: float = 1) -> _RenderedPage:
        self.render_scale = scale
        return _RenderedPage(self._image)


class _Document:
    def __init__(self, images: list[Image.Image]) -> None:
        self.pages = [_Page(image) for image in images]
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, page_index: int) -> _Page:
        return self.pages[page_index]

    def close(self) -> None:
        self.closed = True


def _image(width: int = 100, height: int = 200) -> Image.Image:
    return Image.new("RGB", (width, height), color="white")


def test_orientation_keeps_page_upright_when_top_quarter_is_upright() -> None:
    source = _image()
    calls: list[tuple[int, int]] = []

    def detect(image: Image.Image) -> tuple[int, float]:
        calls.append(image.size)
        return (270, 0.87) if image.size == source.size else (0, 0.92)

    result = _normalize_page_orientation(source, detector=detect)

    assert calls == [(100, 200), (100, 50)]
    assert result.size == (100, 200)
    assert result.info["lexoid_rotation"] == 0


def test_orientation_rotates_page_when_top_quarter_agrees() -> None:
    source = _image()
    calls: list[tuple[int, int]] = []

    def detect(image: Image.Image) -> tuple[int, float]:
        calls.append(image.size)
        return 90, 0.92

    result = _normalize_page_orientation(source, detector=detect)

    assert calls == [(100, 200), (100, 50)]
    assert result.size == (200, 100)
    assert result.info["lexoid_rotation"] == 90


def test_render_uses_requested_dpi_scale() -> None:
    document = _Document([_image()])

    rendered = render_pdf_page_from_document(
        document,
        page_index=0,
        dpi=240,
        auto_orient=False,
    )

    assert document.pages[0].render_scale == 240 / 72
    assert (rendered.page, rendered.dpi) == (1, 240)
    assert (rendered.width, rendered.height) == (100, 200)


def test_render_applies_orientation_once_before_recording_dimensions() -> None:
    document = _Document([_image(width=100, height=200)])
    calls: list[tuple[int, int]] = []

    def rotate(image: Image.Image) -> Image.Image:
        calls.append(image.size)
        return image.rotate(90, expand=True)

    rendered = render_pdf_page_from_document(
        document,
        page_index=0,
        dpi=240,
        auto_orient=True,
        orientation_normalizer=rotate,
    )

    assert calls == [(100, 200)]
    assert (rendered.width, rendered.height) == (200, 100)
    assert rendered.image.size == (200, 100)


def test_public_render_uses_one_based_page_and_closes_document() -> None:
    document = _Document([_image(), _image(width=300, height=400)])
    opened: list[str] = []

    def document_factory(path: str) -> _Document:
        opened.append(path)
        return document

    rendered = render_pdf_page(
        "record.pdf",
        page=2,
        dpi=240,
        auto_orient=False,
        document_factory=document_factory,
    )

    assert opened == ["record.pdf"]
    assert rendered.page == 2
    assert rendered.image.size == (300, 400)
    assert document.closed is True


def test_public_render_rejects_page_outside_document() -> None:
    document = _Document([_image()])

    try:
        render_pdf_page(
            "record.pdf",
            page=2,
            dpi=240,
            auto_orient=False,
            document_factory=lambda _path: document,
        )
    except ValueError as error:
        assert str(error) == "page must be between 1 and 1, got 2"
    else:
        raise AssertionError("render_pdf_page accepted a page outside the document")

    assert document.closed is True


def _slanted_form(angle):
    from PIL import ImageDraw
    image = Image.new('RGB', (1000, 1000), 'white')
    draw = ImageDraw.Draw(image)
    for y in range(180, 821, 80):
        draw.line((100, y, 900, y), fill='black', width=3)
    for x in (100, 280, 900):
        draw.line((x, 180, x, 820), fill='black', width=3)
    # Mark the label and far-right option belonging to the SAME source row.
    for x in (180, 800):
        draw.ellipse((x-8, 370-8, x+8, 370+8), fill='blue')
    return image.rotate(angle, expand=True, fillcolor='white')


@pytest.mark.parametrize("angle", [-18, -12, -3, 3, 12, 18])
def test_render_levels_same_row_fields_before_vision(angle):
    import numpy as np
    source = _slanted_form(angle)
    rendered = render_pdf_page_from_document(
        _Document([source]), 0, 240,
        orientation_normalizer=lambda image: image.copy(),
    )
    pixels = np.asarray(rendered.image)
    ys, xs = np.where((pixels[:, :, 2] > 200) & (pixels[:, :, 0] < 50))
    middle = (xs.min() + xs.max()) / 2
    assert abs(ys[xs < middle].mean() - ys[xs > middle].mean()) < 3
    assert abs(rendered.deskew + angle) < 0.5


def test_render_no_auto_orient_preserves_slanted_source():
    source = _slanted_form(-12)
    rendered = render_pdf_page_from_document(_Document([source]), 0, 240, auto_orient=False)
    assert rendered.image.tobytes() == source.tobytes()


def test_deskew_preserves_upright_grid_and_blank_page():
    from lexoid.core.recognition.table_geometry import deskew_table
    for source in (_slanted_form(0), Image.new("RGB", (1000, 1000), "white")):
        result, angle = deskew_table(source)
        assert angle == 0
        assert result.tobytes() == source.tobytes()


def test_deskew_rejects_parallel_lines_without_column_boundaries():
    from PIL import ImageDraw
    from lexoid.core.recognition.table_geometry import deskew_table
    source = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(source)
    for y in range(100, 901, 80):
        draw.line((100, y, 900, y), fill="black", width=3)
    source = source.rotate(-12, expand=True, fillcolor="white")
    result, angle = deskew_table(source)
    assert angle == 0
    assert result.tobytes() == source.tobytes()


def test_deskew_retains_quarter_turn_metadata_and_pixels_at_edges():
    import numpy as np
    from PIL import ImageDraw
    source = _slanted_form(-12)
    ImageDraw.Draw(source).rectangle((0, 0, 10, 10), fill="red")
    rotated = source.rotate(-90, expand=True)
    result = render_pdf_page_from_document(
        _Document([rotated]), 0, 240,
        orientation_normalizer=lambda image: _normalize_page_orientation(image, lambda _: (90, 1.0)),
    )
    assert result.rotation == 90
    assert abs(result.deskew-12) < .5
    pixels = np.asarray(result.image)
    assert ((pixels[:, :, 0] > 200) & (pixels[:, :, 1] < 50)).sum() >= 90
