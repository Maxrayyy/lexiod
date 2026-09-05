from __future__ import annotations

from PIL import Image

from lexoid.core.recognition.rendering import (
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
