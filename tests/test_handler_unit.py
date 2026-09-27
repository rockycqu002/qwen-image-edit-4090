"""CPU-only unit tests for the handler's input handling, graph construction and output encoding.

Run:  python -m pytest tests/test_handler_unit.py -q
"""
import base64, io, os, sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import handler as h  # noqa: E402


def b64_of(im, fmt, **kw):
    buf = io.BytesIO(); im.save(buf, format=fmt, **kw)
    return base64.b64encode(buf.getvalue()).decode()


def test_decode_formats():
    im = Image.new("RGB", (640, 480), (10, 200, 30))
    for fmt, magic in (("JPEG", "jpeg"), ("PNG", "png"), ("WEBP", "webp")):
        out, meta = h.decode_image("image", b64_of(im, fmt), 1)
        assert meta["format"] == magic and out.size == (640, 480) and out.mode == "RGB"


def test_data_url_prefix_and_padding():
    im = Image.new("RGB", (64, 64))
    raw = b64_of(im, "PNG")
    assert h.decode_image("image", "data:image/png;base64," + raw, 1)[1]["format"] == "png"
    assert h.decode_image("image", raw.rstrip("="), 1)[1]["format"] == "png"


def test_rejects_garbage_and_wrong_format():
    with pytest.raises(h.BadInput):
        h.decode_image("image", "not base64!!", 1)
    with pytest.raises(h.BadInput):
        h.decode_image("image", base64.b64encode(b"GIF89a" + b"\0" * 100).decode(), 1)
    with pytest.raises(h.BadInput):
        h.decode_image("image", base64.b64encode(b"\xff\xd8\xff" + b"\0" * 50).decode(), 1)  # truncated jpeg


def test_size_limit():
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (h.MAX_INPUT_BYTES + 1)).decode()
    with pytest.raises(h.BadInput, match="8 MiB"):
        h.decode_image("image", big, 1)


def test_exif_transpose_and_alpha():
    im = Image.new("RGB", (100, 50), (255, 0, 0))
    buf = io.BytesIO()
    exif = Image.Exif(); exif[0x0112] = 6  # rotate 90 CW on display
    im.save(buf, format="JPEG", exif=exif.tobytes())
    out, _ = h.decode_image("image", base64.b64encode(buf.getvalue()).decode(), 1)
    assert out.size == (50, 100)
    rgba = Image.new("RGBA", (32, 32), (0, 0, 255, 0))  # fully transparent → white after compositing
    out, _ = h.decode_image("image", b64_of(rgba, "PNG"), 1)
    assert out.getpixel((0, 0)) == (255, 255, 255)


def test_animated_webp_first_frame_only():
    frames = [Image.new("RGB", (48, 48), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO(); frames[0].save(buf, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    out, _ = h.decode_image("image", base64.b64encode(buf.getvalue()).decode(), 1)
    assert out.size == (48, 48) and out.getpixel((1, 1))[0] > 200


def test_parse_job_defaults_and_ranges():
    img = b64_of(Image.new("RGB", (64, 64)), "PNG")
    p = h.parse_job({"image": img, "prompt": "make it blue"})
    assert p["steps"] == 20 and p["resolution"] == 1024 and 0 <= p["seed"] < 2**32 and len(p["images"]) == 1
    p = h.parse_job({"image": img, "prompt": "x", "steps": "8", "resolution": 1000, "seed": 7, "ref_image": img, "ref_images": [img, img]})
    assert p["steps"] == 8 and p["resolution"] == 992 and p["seed"] == 7 and len(p["images"]) == 4
    for bad in ({"prompt": "x"}, {"image": img}, {"image": img, "prompt": "x", "steps": 99}, {"image": img, "prompt": "x", "resolution": 512},
                {"image": img, "prompt": "x", "ref_images": [img] * 6}):
        with pytest.raises(h.BadInput):
            h.parse_job(bad)


def test_graph_shape():
    g = h.build_graph({"prompt": "p", "steps": 20, "resolution": 1024, "seed": 3}, ["a.png", "b.png"], "job/x")
    enc = g["5"]["inputs"]
    assert enc["images.image_1"] == ["41", 0] and enc["images.image_2"] == ["42", 0] and enc["resolution"] == 1024
    ks = g["7"]["inputs"]
    assert ks["cfg"] == 1.0 and ks["sampler_name"] == "euler" and ks["scheduler"] == "simple" and ks["steps"] == 20 and ks["seed"] == 3
    assert g["1"]["inputs"]["unet_name"].endswith("int8_convrot.safetensors") and g["9"]["inputs"]["filename_prefix"] == "job/x"


def test_encode_jpeg_quality_ladder():
    im = Image.effect_noise((1280, 1280), 64).convert("RGB")
    b64, q, nbytes = h.encode_jpeg(im)
    assert q in h.JPEG_QUALITIES and len(b64) <= h.MAX_RETURN_BYTES and base64.b64decode(b64)[:3] == b"\xff\xd8\xff"
