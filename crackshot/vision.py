"""视觉层：截图 + 像素检测定位购买按钮，点击加随机偏移。

为什么不用控件树找按钮：2026-11 版大麦详情页购买按钮为自绘控件，
无文字节点且 resource-id 可能被混淆；颜色/位置是 UI 渲染层面的特征，
抗混淆、自适应布局变化。

热路径（抢票瞬间）只用像素检测（毫秒级，不开模型）；
低频路径（预热校准）可调用本地 Ollama 视觉模型交叉验证区域。
"""

from __future__ import annotations

import io
import logging
import random
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

log = logging.getLogger("crackshot.vision")

# 大麦购买按钮橙（真机采样：RGB 254-255, 83-106, 4-19）
# 宽容差：R 高、G 中、B 低；白/灰(R≈G≈B)、纯红(G 极低)均不命中
_ORANGE_R, _ORANGE_G, _ORANGE_B = (245, 255), (60, 160), (0, 70)
_MIN_AREA = 600          # 最少橙色像素数（半分辨率下），过滤零星杂色
_SEARCH_TOP_RATIO = 0.85  # 只在屏幕下 15% 区域找（按钮永远在底栏）

BBox = Tuple[int, int, int, int]  # (x1, y1, x2, y2) 原图坐标


def find_buy_button(png_bytes: bytes) -> Optional[BBox]:
    """在截图底部区域找橙色购买按钮，返回其包围盒；找不到返回 None。

    半分辨率处理兼顾速度与精度；单次分析 <10ms（不含截图本身）。
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    scale = 2
    small = img.resize((w // scale, h // scale))
    arr = np.asarray(small, dtype=np.int16)
    return _find_buy_button(arr, scale)


def _find_buy_button(arr, scale):
    top = int(arr.shape[0] * _SEARCH_TOP_RATIO)
    region = arr[top:, :, :]
    r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    mask = (
        (r >= _ORANGE_R[0]) & (r <= _ORANGE_R[1])
        & (g >= _ORANGE_G[0]) & (g <= _ORANGE_G[1])
        & (b >= _ORANGE_B[0]) & (b <= _ORANGE_B[1])
    )
    count = int(mask.sum())
    if count < _MIN_AREA // scale:
        return None
    ys, xs = np.nonzero(mask)
    # 投影法取包围盒（底栏内只有一个大橙色块；零星噪点用 2%/98% 分位剔除）
    x1 = int(np.percentile(xs, 2)) * scale
    x2 = int(np.percentile(xs, 98)) * scale
    y1 = int(np.percentile(ys, 2)) * scale + top * scale
    y2 = int(np.percentile(ys, 98)) * scale + top * scale
    if x2 - x1 < 40 or y2 - y1 < 30:  # 太小不是按钮
        return None
    return (x1, y1, x2, y2)


def opening_frame(raw) -> tuple:
    """定位按钮并提取文字区域的白色像素，用于剔除未变化的预约画面。"""
    width, height, data = raw
    pixels = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)
    bbox = _find_buy_button(pixels[::2, ::2, :3], 2)
    if not bbox:
        return None, None
    left, top, right, bottom = bbox
    dx, dy = (right - left) // 5, (bottom - top) // 10
    crop = pixels[top + dy:bottom - dy, left + dx:right - dx, :3]
    white = np.all(crop >= 230, axis=-1)
    glyphs = np.asarray(Image.fromarray(white).resize((96, 32), Image.Resampling.NEAREST))
    return bbox, glyphs


def button_text_changed(before, after) -> bool:
    if before is None or after is None:
        return False
    union = np.count_nonzero(before | after)
    return (np.count_nonzero(before) >= 24 and np.count_nonzero(after) >= 24
            and np.count_nonzero(before ^ after) >= .35 * union)


def random_point(bbox: BBox, margin: float = 0.18) -> Tuple[int, int]:
    """按钮包围盒内随机取点（避开边缘 margin 比例），模拟人手落点不精准。"""
    x1, y1, x2, y2 = bbox
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    return (
        int(random.uniform(x1 + mx, x2 - mx)),
        int(random.uniform(y1 + my, y2 - my)),
    )


def tap_button_jitter(adb, bbox: BBox, margin: float = 0.18) -> Tuple[int, int]:
    """按视觉包围盒随机点击；返回实际点击坐标。"""
    x, y = random_point(bbox, margin)
    adb.tap(x, y)
    return (x, y)


# 详情页底部购买按钮的相对区域（不依赖颜色/id，抗自绘抗混淆）：
# x 30%~95%、y 93.5%~97.8%（真机 1176x2400 验证与实际按钮容器吻合）
_BUY_REGION_X = (0.30, 0.95)
_BUY_REGION_Y = (0.935, 0.978)


def tap_bottom_region(adb, size) -> Tuple[int, int]:
    """盲点：按屏幕尺寸在底部购买按钮区域内随机点击（armed 后兜底）。"""
    w, h = size
    x = int(w * random.uniform(*_BUY_REGION_X))
    y = int(h * random.uniform(*_BUY_REGION_Y))
    adb.tap(x, y)
    return (x, y)
