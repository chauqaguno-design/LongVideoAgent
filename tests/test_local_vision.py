import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import av
import numpy as np

from cinetrace.local_vision import describe, representative_frame


def write_clip(path, gray_levels):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("mpeg4", rate=4)
        stream.width, stream.height = 96, 64
        for level in gray_levels:
            pixels = np.full((64, 96, 3), level, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            output.mux(stream.encode(frame))
        output.mux(stream.encode())


class LocalVisionTests(unittest.TestCase):
    def test_static_clip_needs_no_model_to_rule_out_visible_motion(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "still.mp4"
            write_clip(path, [16, 16, 16])
            self.assertIn("画面基本静止", describe(path, "unused"))

    def test_representative_frame_comes_from_middle_of_a_moving_shot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "changing.mp4"
            write_clip(path, [16, 128, 240])
            frame = representative_frame(path)
            self.assertAlmostEqual(frame.getpixel((32, 32))[0], 128, delta=4)
