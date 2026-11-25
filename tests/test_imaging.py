import json
import os
import stat
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monkeytester_ai import imaging

FIXTURE = Path(__file__).parent / "fixtures" / "target.png"


class SizeTests(unittest.TestCase):
    def test_png(self):
        self.assertEqual(imaging.image_size(FIXTURE.read_bytes()), ("png", 1170, 2532))

    def test_jpeg(self):
        data = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"ab"
                + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 200, 300) + b"\0" * 12)
        self.assertEqual(imaging.image_size(data), ("jpeg", 300, 200))

    def test_rejects_other_data(self):
        for data in (b"", b"GIF89a....", b"\xff\xd8\x00"):
            with self.assertRaises(imaging.ImageError):
                imaging.image_size(data)

    def test_rejects_absurd_dimensions(self):
        header = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + struct.pack(">II", 100000, 100000)
        with self.assertRaises(imaging.ImageError):
            imaging.image_size(header)


class SmartResizeTests(unittest.TestCase):
    def test_multiples_of_factor_within_limit(self):
        for size in ((1170, 2532), (1080, 2400), (2532, 1170), (1, 5000), (5000, 1), (28, 28)):
            w, h = imaging.smart_resize(*size, 1_000_000)
            self.assertEqual(w % 28, 0)
            self.assertEqual(h % 28, 0)
            self.assertLessEqual(w * h, 1_000_000)
            self.assertGreaterEqual(w * h, 28 * 28)

    def test_phone_screenshot_keeps_aspect_ratio(self):
        w, h = imaging.smart_resize(1170, 2532, 1_000_000)
        self.assertAlmostEqual(w / h, 1170 / 2532, delta=0.02)

    def test_small_conforming_image_is_unchanged(self):
        self.assertEqual(imaging.smart_resize(560, 560, 1_000_000), (560, 560))


class RescaleTests(unittest.TestCase):
    def rescale(self, content, sx=2.0, sy=3.0, w=1000, h=2000):
        return imaging.rescale_reply(content, sx, sy, w, h)

    def test_point_and_box(self):
        self.assertEqual(json.loads(self.rescale('{"coordinates": [10, 20]}')), {"coordinates": [20, 60]})
        self.assertEqual(json.loads(self.rescale('{"bbox": [1, 2, 3, 4]}')), {"bbox": [2, 6, 6, 12]})

    def test_coordinates_become_box_center(self):
        # The real model returns the box's top-left corner as `coordinates`.
        reply = json.loads(self.rescale('{"coordinates": [10, 20], "bbox": [10, 20, 30, 40], "confidence": 0.5}'))
        self.assertEqual(reply, {"coordinates": [40, 90], "bbox": [20, 60, 60, 120], "confidence": 0.5})

    def test_point_on_box_edge_becomes_box_center(self):
        # Just inside the corner still counts as the corner answer.
        reply = json.loads(self.rescale('{"coordinates": [11, 21], "bbox": [10, 20, 30, 40]}'))
        self.assertEqual(reply["coordinates"], [40, 90])

    def test_point_outside_box_becomes_box_center(self):
        reply = json.loads(self.rescale('{"coordinates": [5, 50], "bbox": [10, 20, 30, 40]}'))
        self.assertEqual(reply["coordinates"], [40, 90])

    def test_point_well_inside_a_container_box_is_kept(self):
        # Seen on a Light / Dark / Auto control: the box is the whole control,
        # the point is on "Light"; the box centre would tap "Dark".
        reply = json.loads(self.rescale('{"coordinates": [112, 277], "bbox": [33, 236, 614, 326]}',
                                        sx=1.0, sy=1.0))
        self.assertEqual(reply["coordinates"], [112, 277])

    def test_nested_positions(self):
        reply = json.loads(self.rescale('{"elements": [{"text": "a", "position": [5, 5]}]}'))
        self.assertEqual(reply["elements"][0]["position"], [10, 15])

    def test_top_level_list_with_qwen_native_key(self):
        reply = json.loads(self.rescale('[{"bbox_2d": [1, 1, 2, 2]}]'))
        self.assertEqual(reply[0]["bbox_2d"], [2, 3, 4, 6])

    def test_code_fence_is_kept(self):
        out = self.rescale('```json\n{"coordinates": [1, 1]}\n```')
        self.assertTrue(out.startswith("```json\n") and out.endswith("\n```"))
        self.assertIn("[2, 3]", out)

    def test_clamped_to_image(self):
        reply = json.loads(self.rescale('{"coordinates": [9999, -5]}'))
        self.assertEqual(reply["coordinates"], [999, 0])

    def test_other_values_untouched(self):
        original = {"found": False, "coordinates": None, "bbox": [1, 2], "position": ["a", "b"], "text": "[1, 2]"}
        self.assertEqual(json.loads(self.rescale(json.dumps(original))), original)

    def test_non_json_passes_through(self):
        for text in ("Hello there", "broken {json", "} backwards {", ""):
            self.assertEqual(self.rescale(text), text)


class SipsTests(unittest.TestCase):
    def run_with_fake_sips(self, script):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "sips"
            fake.write_text(script)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}):
                return imaging.resize_with_sips(b"input", "png", 100, 200)

    def test_passes_height_then_width(self):
        out = self.run_with_fake_sips('#!/bin/sh\nprintf "$2 $3" > "$6"\n')
        self.assertEqual(out, b"200 100")

    def test_failure_is_image_error(self):
        with self.assertRaises(imaging.ImageError):
            self.run_with_fake_sips("#!/bin/sh\nexit 1\n")


if __name__ == "__main__":
    unittest.main()
