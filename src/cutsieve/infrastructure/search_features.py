"""CPU screenshot retrieval using a versioned spatial colour descriptor."""

from colorsys import rgb_to_hsv
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


PIXEL_SPACE = "spatial-rgb-8x8-v1"
MAX_IMAGE_BYTES = 8 * 1024**2
MAX_PIXELS = 16_000_000
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def read_query_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("查询图片为空或超过 8 MiB")
    try:
        with Image.open(BytesIO(data)) as image:
            mime = IMAGE_TYPES.get(image.format)
            if not mime or image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("仅支持不超过 1600 万像素的单张 JPEG、PNG、WebP")
            return mime, ImageOps.exif_transpose(image).convert("RGB")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("无法解码查询图片") from exc


def descriptor(image):
    return image.resize((8, 8), Image.Resampling.BOX).convert("RGB").tobytes()


@lru_cache(maxsize=8192)
def frame_descriptors(path, digest):
    # The immutable digest pins the decoder input. Crops keep partial screenshots useful.
    with Image.open(path) as source:
        frame = source.convert("RGB")
    w, h = frame.size
    variants = [descriptor(frame)]
    for fraction in (0.5, 0.75):
        cw, ch = max(1, round(w * fraction)), max(1, round(h * fraction))
        for x, y in ((0, 0), (w - cw, 0), ((w - cw) // 2, (h - ch) // 2), (0, h - ch), (w - cw, h - ch)):
            variants.append(descriptor(frame.crop((x, y, x + cw, y + ch))))
    return variants


def similarity(query, variants):
    distance = min(sum(abs(a - b) for a, b in zip(query, sample)) for sample in variants)
    return 1 - distance / (len(query) * 255)


def hue_coverage(rgb, center, radius):
    """Measure the fraction of a frame occupied by a sufficiently saturated hue."""
    matching = 0
    for r, g, b in zip(rgb[::3], rgb[1::3], rgb[2::3]):
        hue, saturation, value = rgb_to_hsv(r, g, b)
        distance = abs(hue * 360 - center)
        if saturation >= 0.35 and value >= 40 and min(distance, 360 - distance) <= radius:
            matching += 1
    return matching / (len(rgb) // 3)
