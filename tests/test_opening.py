import unittest
from concurrent.futures import Future
from unittest.mock import patch

import numpy as np

from crackshot import opening


class OpeningProbeTests(unittest.TestCase):
    BAR = (300, 2200, 1150, 2370)
    BUTTON = (320, 2230, 1130, 2360)

    def probe(self, after_start, after_end, changed=True):
        probe = opening.OpeningProbe.__new__(opening.OpeningProbe)
        probe.sale_at_ms = 10000
        probe.before, probe.after = Future(), Future()
        before = np.zeros((32, 96), dtype=bool)
        before[8:12, 30:70] = True
        after = np.zeros_like(before)
        after[16:20, 30:70] = True
        probe.before.set_result((8500, 9300, self.BUTTON, before))
        probe.after.set_result((after_start, after_end, self.BUTTON, after if changed else before))
        return probe

    @patch.object(opening, "now_ms", return_value=10900)
    def test_fresh_changed_frame_is_available_without_second_capture(self, _):
        self.assertEqual(self.probe(10000, 10800).result(self.BAR), (self.BUTTON, 800))

    def test_pending_frame_does_not_block_control_path(self):
        probe = self.probe(10000, 10800)
        probe.after = Future()
        with patch.object(probe.after, "result", side_effect=AssertionError("不应等待截图")):
            self.assertIsNone(probe.result(self.BAR))

    @patch.object(opening, "now_ms", return_value=10900)
    def test_unmodified_early_and_stale_frames_are_rejected(self, _):
        self.assertIsNone(self.probe(10000, 10800, changed=False).result(self.BAR))
        self.assertIsNone(self.probe(9999, 10800).result(self.BAR))
        self.assertIsNone(self.probe(10000, 10500).result(self.BAR))
        self.assertIsNone(self.probe(10000, 10800).result((0, 0, 100, 100)))
