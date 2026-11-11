"""无设备冒烟测试：用假设备驱动抢票状态机走通关键路径。

运行: .venv/bin/python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crackshot.adb import Adb
from crackshot.config import TaskConfig, load_config
from crackshot.device import Element
from crackshot.grabber import GrabOutcome, Grabber
from crackshot.notify import SilentNotifier
from crackshot.pages import recognize  # noqa: F401 —— 确认可导入
from crackshot.tiers import Selection


class World:
    """页面剧本：每次 tap 推进到下一幕。"""

    def __init__(self, scenes):
        self.scenes = scenes
        self.index = 0
        self.taps = []
        self.backs = 0

    @property
    def scene(self):
        return self.scenes[min(self.index, len(self.scenes) - 1)]

    def advance(self):
        if self.index < len(self.scenes) - 1:
            self.index += 1


def make_scene(elements: dict, extra_texts=()):
    """elements: 文字 → (bounds, clickable, enabled, contains_text[, resource_id])"""
    return {
        "exact": {t: e[3] or t for t, e in elements.items()},
        "elements": {
            t: Element(text=(e[3] or t), bounds=e[0], clickable=e[1], enabled=e[2],
                       resource_id=(e[4] if len(e) > 4 else ""))
            for t, e in elements.items()
        },
        "extra": set(extra_texts),
    }


class FakeDevice:
    def __init__(self, world):
        self.world = world

    def refresh(self):
        pass  # 假设备按剧本推进，无需快照失效

    def elements(self):
        return list(self.world.scene["elements"].values())

    def find_by_id(self, substr):
        for el in self.world.scene["elements"].values():
            if substr in el.resource_id:
                return el
        return None

    def has_checked_checkbox(self):
        return True  # 场景默认观演人已预选

    def find_all(self, texts):
        wanted = set(texts)
        return [n for n in self.world.scene["elements"].values()
                if n.text in wanted or n.description in wanted]

    def exists_any(self, texts):
        scene = self.world.scene
        return any(t in scene["exact"] or t in scene["extra"] for t in texts)

    def exists_contains_any(self, texts):
        scene = self.world.scene
        for t in texts:
            if t in scene["extra"] or self.find_contains(t):
                return True
        return False

    def find(self, texts):
        scene = self.world.scene
        for t in texts:
            if t in scene["elements"]:
                return scene["elements"][t]
        return None

    def find_contains(self, text):
        for el in self.world.scene["elements"].values():
            if text in el.text:
                return el
        return None


class FakeAdb:
    def __init__(self, world):
        self.world = world
        self.min_tap_interval_ms = 0
        self._last_tap_ms = 0

    def screencap_bytes(self):
        return b"png"

    def tap(self, x, y):
        self.world.taps.append((x, y))
        self.world.advance()

    def back(self):
        self.world.backs += 1
        self.world.advance()

    def screencap(self, path):
        Path(path).write_bytes(b"png")
        return Path(path)

    def shell(self, *a, **k):
        return ""


def build_task():
    cfg = load_config(Path(__file__).resolve().parent.parent / "tasks.example.yaml")
    return cfg.tasks[0]


def run_grabber(scenes, task):
    world = World(scenes)
    def fake_selection(nodes, _capture, _task):
        if any(n.text == "缺货登记" for n in nodes):
            return Selection("sold_out", reason="目标票档缺货")
        confirm = next(n for n in nodes if n.text == "确定")
        return Selection("confirm", confirm.center, quantity=1)
    grabber = Grabber(
        adb=FakeAdb(world),
        device=FakeDevice(world),
        task=task,
        notifier=SilentNotifier(),
        evidence_dir=Path("/tmp/crackshot-test-evidence"),
        dry_run=False,
        tier_reader=fake_selection,
    )
    clock = 10000

    def advance(seconds):
        nonlocal clock
        clock += round(seconds * 1000)

    grabber.deadline_ms = clock + 30000
    with patch("crackshot.grabber.now_ms", lambda: clock), \
         patch("crackshot.grabber.time", SimpleNamespace(sleep=advance)), \
         patch("crackshot.grabber.sleep_jitter", lambda interval: advance(interval[0] / 1000)):
        for _ in range(100):
            outcome = grabber._attempt()
            if outcome:
                return world, outcome
    return world, "no-outcome"


def test_happy_path():
    task = build_task()
    button_bbox = (340, 2240, 1124, 2368)
    scenes = [
        # 详情页：立即预订可点
        make_scene({"演出标题": ((100, 1800, 400, 1900), True, True, "演出标题",
                                "cn.damai:id/info_v2_title"),
                    "立即预订": (button_bbox, True, True, "立即预订")}),
        # 选票页：预约票档已预选，直接点确定
        make_scene(
            {"¥688": ((80, 900, 300, 960), True, True, "¥688"),
             "确定": ((800, 2000, 1000, 2060), True, True, "确定"),
             "票档": ((80, 880, 200, 930), False, True, "票档")},
            extra_texts=["每笔订单限购"],
        ),
        # 确认订单页：立即提交 + 观演人已预选（checkbox 选中由 has_checked_checkbox 提供）
        make_scene(
            {"立即提交": ((600, 2200, 1000, 2280), True, True, "立即提交"),
             "张三": ((80, 800, 400, 880), False, True, "张三", "cn.damai:id/text_name")},
            extra_texts=["观演人"],
        ),
        # 成功页
        make_scene({}, extra_texts=["订单提交成功", "立即支付"]),
    ]
    world, outcome = run_grabber(scenes, task)
    assert outcome == GrabOutcome.SUCCESS, outcome
    # 点击序列：可购按钮 → 确定 → 提交订单
    assert len(world.taps) == 3, world.taps
    bx = world.taps[0]
    assert button_bbox[0] <= bx[0] <= button_bbox[2], world.taps  # 第一点落在按钮内
    print("✓ 成功路径: 购买控件 → 确定 → 提交", world.taps)


def test_captcha_stops():
    task = build_task()
    scenes = [make_scene({}, extra_texts=["请完成验证"])]
    world, outcome = run_grabber(scenes, task)
    assert outcome == GrabOutcome.NEEDS_HUMAN, outcome
    assert world.taps == [], world.taps
    print("✓ 验证码路径: 立即停止且不点击")


def test_sold_out_tier_stops():
    task = build_task()
    scenes = [
        # 选票页：目标票档标缺货
        make_scene(
            {"¥688": ((80, 900, 300, 960), True, True, "¥688"),
             "缺货登记": ((320, 900, 390, 960), False, True, "缺货登记"),
             "票档": ((80, 850, 200, 890), False, True, "票档")},
        ),
    ]
    world, outcome = run_grabber(scenes, task)
    assert world.taps == [] and world.backs == 0
    assert outcome == GrabOutcome.SOLD_OUT
    print("✓ 预约票档缺货时停止")


def test_order_retry_and_payment():
    task = build_task()
    submit = {"立即提交": ((600, 2200, 1000, 2280), True, True, "立即提交")}
    retry = {
        "继续尝试": ((400, 1300, 850, 1380), True, True, "继续尝试"),
        "返回重新选购": ((500, 1450, 850, 1500), True, True, "返回重新选购"),
    }
    world, outcome = run_grabber([
        make_scene(submit), make_scene({**submit, **retry}), make_scene(submit),
        make_scene({"立即支付": ((600, 2200, 1000, 2280), True, True, "立即支付")}),
    ], task)
    assert outcome == GrabOutcome.SUCCESS
    assert world.taps == [(800, 2240), (625, 1340), (800, 2240)]
    assert world.backs == 0
    print("✓ 提交 → 继续尝试 → 再提交 → 待支付即停")


def test_pending_submit_does_not_double_tap():
    task = build_task()
    task.post_submit_watch_seconds = 1
    submit = {"立即提交": ((600, 2200, 1000, 2280), True, True, "立即提交")}
    world, outcome = run_grabber([make_scene(submit), make_scene(submit)], task)
    assert outcome == GrabOutcome.NEEDS_HUMAN
    assert world.taps == [(800, 2240)] and world.backs == 0
    print("✓ 提交结果未明确时只点击一次")


def test_known_retry_popup_keeps_retrying_until_success():
    task = build_task()
    submit = {"立即提交": ((600, 2200, 1000, 2280), True, True, "立即提交")}
    retry = {"继续尝试": ((400, 1300, 850, 1380), True, True, "继续尝试")}
    world, outcome = run_grabber([make_scene(submit)]
                                + [make_scene({**submit, **retry}) for _ in range(6)]
                                + [make_scene({}, extra_texts=["订单提交成功"])], task)
    assert outcome == GrabOutcome.SUCCESS
    assert world.taps == [(800, 2240)] + [(625, 1340)] * 6
    assert world.backs == 0
    print("✓ 已识别的继续尝试弹窗持续重试，成功后停止")


def test_button_inactive_waits():
    task = build_task()
    # 详情页但按钮未激活（视觉无橙色块）：应只等待（不点击、不 back）
    scenes = [make_scene({"即将开抢": ((100, 1800, 400, 1900), False, True, "即将开抢",
                                       "cn.damai:id/info_v2_title")})]
    world, outcome = run_grabber(scenes, task)
    assert world.taps == [] and world.backs == 0
    print("✓ 按钮未激活: 原地等待不动作")



if __name__ == "__main__":
    test_happy_path()
    test_captcha_stops()
    test_sold_out_tier_stops()
    test_order_retry_and_payment()
    test_pending_submit_does_not_double_tap()
    test_known_retry_popup_keeps_retrying_until_success()
    test_button_inactive_waits()
    print("\n全部冒烟测试通过")
