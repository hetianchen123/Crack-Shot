"""选票页视觉解析：qwen3-vl 读取票档价格/库存/位置，返回可点击布局。

选票页的票档价格与“确定”按钮均为自绘控件（控件树无文字节点），
只有“缺货登记”标签可读。开票前用本地视觉模型解析一次布局，
热路径按解析坐标随机点击；确定按钮用 vision.find_buy_button 像素检测。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import vision
from .adb import Adb

log = logging.getLogger("crackshot.tiers")

# 归一化坐标（0-1000）让模型输出稳定，再按截图实际尺寸换算
PROMPT = """这是大麦App选票页面的截图。请仔细识别并返回严格 JSON：
{
 "sessions": [{"text": "场次文字如 2026-11-28 周六 19:00", "selected": true/false, "cx": 0-1000, "cy": 0-1000}],
 "tiers": [{"price": "纯数字价格如 688", "in_stock": true/false, "cx": 0-1000, "cy": 0-1000}],
 "notes": "其他异常说明，没有则空字符串"
}
要求：
- tiers 按页面从上到下、从左到右顺序列出所有票档；
- in_stock：该票档行带“缺货登记”字样时为 false；
- cx/cy 是该票档价格文字中心在截图中的相对位置（横向0=最左1000=最右，纵向0=最顶1000=最底）；
- 只报告真实可见内容，不推测。"""


def parse_tier_sheet(png_bytes: bytes, ollama_url: str, model: str,
                     timeout: float = 120.0) -> Dict[str, Any]:
    """截图 → 选票页布局（sessions/tiers 带绝对像素坐标）。"""
    from PIL import Image
    import io
    w, h = Image.open(io.BytesIO(png_bytes)).size

    payload = {
        "model": model,
        "stream": False,
        "format": {"type": "object", "properties": {
            "sessions": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "selected": {"type": "boolean"},
                "cx": {"type": "integer"}, "cy": {"type": "integer"}}}},
            "tiers": {"type": "array", "items": {"type": "object", "properties": {
                "price": {"type": "string"}, "in_stock": {"type": "boolean"},
                "cx": {"type": "integer"}, "cy": {"type": "integer"}},
                "required": ["price", "in_stock", "cx", "cy"]}},
            "notes": {"type": "string"}},
            "required": ["tiers"]},
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 900},
        "messages": [{"role": "user", "content": PROMPT,
                      "images": [base64.b64encode(png_bytes).decode("ascii")]}],
    }
    req = Request(f"{ollama_url.rstrip('/')}/api/chat",
                  data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        content = json.loads(resp.read().decode("utf-8"))["message"]["content"]
    parsed = _load_json(content)
    if not parsed or not parsed.get("tiers"):
        raise RuntimeError(f"视觉解析选票页失败: {str(content)[:200]}")

    def _abs(node: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **node,
            "x": int(node.get("cx", 500) / 1000 * w),
            "y": int(node.get("cy", 500) / 1000 * h),
        }

    return {
        "size": (w, h),
        "sessions": [_abs(s) for s in parsed.get("sessions", [])],
        "tiers": [_abs(t) for t in parsed["tiers"]],
        "notes": parsed.get("notes", ""),
    }


def reconcile_with_tags(layout: Dict[str, Any], device) -> Dict[str, Any]:
    """用控件树「缺货登记」标签坐标交叉校正票档库存。

    模型偶发把有票误判为缺货（实测 qwen3-vl:4b 有 1/7 错误率），而缺货
    标签在控件树是真实可读节点——以控件树为准：票档 y 与任一缺货 tag y
    对齐（±90px）即缺货，否则视为有票。
    """
    tags = device.find_all(["缺货登记"])
    for tag in tags:
        tx, ty = tag.center
        candidates = [tier for tier in layout["tiers"] if 0 <= tx - tier["x"] < 350
                      and abs(tier["y"] - ty) < 90]
        if candidates:
            nearest = min(candidates, key=lambda tier: (tx - tier["x"]) ** 2 + (ty - tier["y"]) ** 2)
            nearest["in_stock"] = False
    return layout


def pick_tier(tiers: List[Dict[str, Any]], wanted: List[str]) -> Optional[Dict[str, Any]]:
    """按用户票档优先级选第一个有货的票档；都没有货返回 None。"""
    for price in wanted:
        digits = re.sub(r"\D", "", str(price))
        if not digits:
            continue
        for tier in tiers:
            if re.sub(r"\D", "", str(tier.get("price", ""))) == digits and tier.get("in_stock"):
                return tier
    return None


def tap_confirm(adb: Adb, png_bytes: bytes) -> Optional[tuple]:
    """像素检测橙色“确定”按钮并随机点击，返回点击坐标。"""
    bbox = vision.find_buy_button(png_bytes)
    if not bbox:
        return None
    return vision.tap_button_jitter(adb, bbox)


def _load_json(content: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(content)
    except ValueError:
        match = re.search(r"\{.*\}", content, re.S)
        if match:
            try:
                return json.loads(match.group(0))
            except ValueError:
                return None
        return None
