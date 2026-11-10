"""在控件树读取的同时预取开票画面。"""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from . import vision
from .adb import AdbError
from .timeutil import now_ms

log = logging.getLogger("crackshot.opening")


class OpeningProbe:
    def __init__(self, capture, sale_at_ms: int):
        self.capture = capture
        self.sale_at_ms = sale_at_ms
        self.stop = Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opening-frame")
        self.before = self.pool.submit(self._read)
        self.after = self.pool.submit(self._read_at_opening)

    def _read(self):
        start = now_ms()
        try:
            bbox, glyphs = vision.opening_frame(self.capture())
            return start, now_ms(), bbox, glyphs
        except (AdbError, OSError, ValueError) as exc:
            log.debug("开票预取失败: %s", exc)
            return None

    def _read_at_opening(self):
        delay = max(0, self.sale_at_ms - now_ms()) / 1000
        if not self.stop.wait(delay):
            return self._read()
        return None

    def result(self, bar):
        """只接受到点后捕获、仍新鲜且按钮文案确有变化的画面。"""
        if not self.before.done():
            return None
        before = self.before.result()
        if not before or before[1] >= self.sale_at_ms or not self._inside_bar(before[2], bar):
            return None
        try:
            after = self.after.result(timeout=2.1)
        except TimeoutError:
            return None
        if not after:
            return None
        start, end, bbox, glyphs = after
        if (start < self.sale_at_ms or now_ms() - end > 350 or not self._inside_bar(bbox, bar)
                or not vision.button_text_changed(before[3], glyphs)):
            return None
        return bbox, end - start

    @staticmethod
    def _inside_bar(bbox, bar) -> bool:
        if not bbox:
            return False
        x, y = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
        return bar[0] <= x <= bar[2] and bar[1] <= y <= bar[3]

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
