import base64
import io

from PIL import Image

from lexoid.core.conversion_utils import convert_pdf_page_to_base64


class _RenderedPage:
    def __init__(self, image):
        self._image = image

    def to_pil(self):
        return self._image.copy()


class _Page:
    def __init__(self, image):
        self._image = image

    def render(self, scale=1):
        assert scale == 1
        return _RenderedPage(self._image)


class _Document:
    def __init__(self, image):
        self._page = _Page(image)

    def __getitem__(self, page_number):
        assert page_number == 0
        return self._page


def _decode_png(encoded):
    return Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")


def _fixture_image():
    image = Image.new("RGB", (2, 3))
    image.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (0, 0, 0),
            (255, 255, 255),
        ]
    )
    return image


def test_auto_orient_rotates_a_270_degree_page_upright():
    encoded = convert_pdf_page_to_base64(
        _Document(_fixture_image()),
        0,
        auto_orient=True,
        orientation_detector=lambda _image: (270, 0.99),
    )

    result = _decode_png(encoded)
    assert result.size == (3, 2)
    assert list(result.getdata()) == [
        (0, 0, 0),
        (0, 0, 255),
        (255, 0, 0),
        (255, 255, 255),
        (255, 255, 0),
        (0, 255, 0),
    ]


def test_auto_orient_leaves_an_upright_page_unchanged():
    source = _fixture_image()
    encoded = convert_pdf_page_to_base64(
        _Document(source),
        0,
        auto_orient=True,
        orientation_detector=lambda _image: (0, 0.99),
    )

    result = _decode_png(encoded)
    assert result.size == source.size
    assert list(result.getdata()) == list(source.getdata())


def test_low_confidence_orientation_does_not_rotate_page():
    source = _fixture_image()
    encoded = convert_pdf_page_to_base64(
        _Document(source),
        0,
        auto_orient=True,
        orientation_detector=lambda _image: (270, 0.50),
    )

    result = _decode_png(encoded)
    assert result.size == source.size
    assert list(result.getdata()) == list(source.getdata())
