"""无设备冒烟测试：用假设备驱动抢票状态机走通关键路径。

运行: .venv/bin/python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crackshot.adb import Adb
from crackshot.config import TaskConfig, load_config
from crackshot.device import Element
from crackshot.grabber import GrabOutcome, Grabber
from crackshot.notify import SilentNotifier
from crackshot.pages import recognize  # noqa: F401 —— 确认可导入


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


def fake_tier_parser(png, url, model):
    # 固定布局：688 有票（坐标对应场景元素的“¥668”位置附近）
    return {"sessions": [], "notes": "",
            "tiers": [{"price": "688", "in_stock": True, "cx": 160, "cy": 387,
                       "x": 190, "y": 930}]}

def run_grabber(scenes, task, button_bboxes=None):
    world = World(scenes)
    bboxes = list(button_bboxes or [])
    def fake_finder(png):
        return bboxes.pop(0) if bboxes else None
    grabber = Grabber(
        adb=FakeAdb(world),
        device=FakeDevice(world),
        task=task,
        notifier=SilentNotifier(),
        evidence_dir=Path("/tmp/crackshot-test-evidence"),
        dry_run=False,
        button_finder=fake_finder,
        tier_parser=fake_tier_parser,
    )
    grabber.deadline_ms = 10**13  # 测试中不过期
    outcome = None
    for _ in range(30):
        outcome = grabber._attempt()
        if outcome:
            return world, outcome
    return world, "no-outcome"


def test_happy_path():
    task = build_task()
    scenes = [
        # 详情页：立即预订可点
        make_scene({"演出标题": ((100, 1800, 400, 1900), True, True, "演出标题",
                                "cn.damai:id/info_v2_title")}),
        # 选票页：688 票档 + 确定（两幕：点票档后浮层仍在，再点确定）
        make_scene(
            {"¥688": ((80, 900, 300, 960), True, True, "¥688"),
             "确定": ((800, 2000, 1000, 2060), True, True, "确定"),
             "票档": ((80, 880, 200, 930), False, True, "票档")},
            extra_texts=["每笔订单限购"],
        ),
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
    button_bbox = (340, 2240, 1124, 2368)
    world, outcome = run_grabber(scenes, task, button_bboxes=[button_bbox])
    assert outcome == GrabOutcome.SUCCESS, outcome
    # 点击序列：视觉按钮随机点 → 票档 → 确定 → 提交订单
    assert len(world.taps) == 4, world.taps
    bx = world.taps[0]
    assert button_bbox[0] <= bx[0] <= button_bbox[2], world.taps  # 第一点落在按钮内
    print("✓ 成功路径: 视觉定位+随机点击完成下单", world.taps)


def test_captcha_stops():
    task = build_task()
    scenes = [make_scene({}, extra_texts=["请完成验证"])]
    world, outcome = run_grabber(scenes, task)
    assert outcome == GrabOutcome.NEEDS_HUMAN, outcome
    assert world.taps == [], world.taps
    print("✓ 验证码路径: 立即停止且不点击")


def test_sold_out_tier_retries():
    task = build_task()
    scenes = [
        # 选票页：目标票档标缺货
        make_scene(
            {"¥688": ((80, 900, 300, 960), True, True, "缺货登记 ¥688")},
            extra_texts=["票档", "确定"],
        ),
    ]
    world, outcome = run_grabber(scenes, task)
    # 不点缺货票档，直接 back 退出
    assert world.taps == [], world.taps
    assert world.backs >= 1
    assert outcome in (None, "no-outcome")
    print("✓ 缺货票档: 不点击，退出重试")


def test_button_inactive_waits():
    task = build_task()
    # 详情页但按钮未激活（视觉无橙色块）：应只等待（不点击、不 back）
    scenes = [make_scene({"即将开抢": ((100, 1800, 400, 1900), False, True, "即将开抢",
                                       "cn.damai:id/info_v2_title")})]
    world, outcome = run_grabber(scenes, task)  # 视觉注入：始终 None
    assert world.taps == [] and world.backs == 0
    print("✓ 按钮未激活: 原地等待不动作")



if __name__ == "__main__":
    test_happy_path()
    test_captcha_stops()
    test_sold_out_tier_retries()
    test_button_inactive_waits()
    print("\n全部冒烟测试通过")
