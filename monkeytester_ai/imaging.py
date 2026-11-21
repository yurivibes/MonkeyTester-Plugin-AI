"""Image geometry: what size the model sees, and mapping its answers back."""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import tempfile
from pathlib import Path

FACTOR = 28  # Qwen2.5-VL patch size (14) x spatial merge size (2)
MIN_PIXELS = 56 * 56
MAX_SOURCE_PIXELS = 100_000_000

POINT_KEYS = {"coordinates", "position"}
BOX_KEYS = {"bbox", "bbox_2d"}


class ImageError(ValueError):
    pass


def image_size(data: bytes):
    """Return (format, width, height) for PNG or JPEG bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return _checked("png", width, height)
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    raise ImageError("image must be PNG or JPEG")


def _jpeg_size(data: bytes):
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return _checked("jpeg", width, height)
        pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
    raise ImageError("could not read JPEG dimensions")


def _checked(fmt, width, height):
    if width < 1 or height < 1 or width * height > MAX_SOURCE_PIXELS:
        raise ImageError(f"unsupported image dimensions {width}x{height}")
    return fmt, width, height


def smart_resize(width: int, height: int, max_pixels: int):
    """Qwen's smart_resize: nearest multiples of 28 within the pixel limits."""
    new_w = max(FACTOR, round(width / FACTOR) * FACTOR)
    new_h = max(FACTOR, round(height / FACTOR) * FACTOR)
    if new_w * new_h > max_pixels:
        beta = math.sqrt(width * height / max_pixels)
        new_w = max(FACTOR, math.floor(width / beta / FACTOR) * FACTOR)
        new_h = max(FACTOR, math.floor(height / beta / FACTOR) * FACTOR)
    elif new_w * new_h < MIN_PIXELS:
        beta = math.sqrt(MIN_PIXELS / (width * height))
        new_w = math.ceil(width * beta / FACTOR) * FACTOR
        new_h = math.ceil(height * beta / FACTOR) * FACTOR
    return new_w, new_h


def resize_with_sips(data: bytes, fmt: str, width: int, height: int) -> bytes:
    """Resize with the sips tool built into macOS."""
    with tempfile.TemporaryDirectory(prefix="vtai-") as tmp:
        src = Path(tmp) / f"in.{fmt}"
        dst = Path(tmp) / f"out.{fmt}"
        src.write_bytes(data)
        try:
            subprocess.run(
                ["sips", "-z", str(int(height)), str(int(width)), str(src), "--out", str(dst)],
                check=True, capture_output=True, timeout=30,
                env={k: v for k, v in os.environ.items() if k != "VTAI_TOKEN"},
            )
            return dst.read_bytes()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageError(f"could not resize image: {exc}") from exc


def rescale_reply(content: str, sx: float, sy: float, width: int, height: int) -> str:
    """Scale coordinates in a JSON reply to source pixels.

    Text around the JSON, such as a code fence, is kept. A reply with no
    parseable JSON is returned unchanged.
    """
    starts = [i for i in (content.find("{"), content.find("[")) if i >= 0]
    if not starts:
        return content
    start = min(starts)
    end = content.rfind("}" if content[start] == "{" else "]")
    if end <= start:
        return content
    try:
        parsed = json.loads(content[start:end + 1])
    except ValueError:
        return content
    scaled = _walk(parsed, sx, sy, width, height)
    return content[:start] + json.dumps(scaled) + content[end + 1:]


def _walk(value, sx, sy, width, height):
    if isinstance(value, list):
        return [_walk(item, sx, sy, width, height) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in POINT_KEYS and _numbers(item, 2):
            out[key] = _scale_xy(item, sx, sy, width, height)
        elif key in BOX_KEYS and _numbers(item, 4):
            out[key] = (_scale_xy(item[:2], sx, sy, width, height)
                        + _scale_xy(item[2:], sx, sy, width, height))
        else:
            out[key] = _walk(item, sx, sy, width, height)
    # Callers tap `coordinates`, but the model often puts the box's top-left
    # corner there, which sits on or outside the element's edge.
    box = next((out[k] for k in BOX_KEYS if k in value and _numbers(value[k], 4)), None)
    if box and _numbers(value.get("coordinates"), 2):
        out["coordinates"] = [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2]
    return out


def _numbers(item, count):
    return (isinstance(item, list) and len(item) == count
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in item))


def _scale_xy(point, sx, sy, width, height):
    x = min(max(round(point[0] * sx), 0), width - 1)
    y = min(max(round(point[1] * sy), 0), height - 1)
    return [x, y]
