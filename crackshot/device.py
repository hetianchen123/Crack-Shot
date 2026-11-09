"""uiautomator2 封装：连接、按文字找控件、读取可点元素。

性能设计（抢票热路径关键）：
- 一次 dump_hierarchy 拿到整棵树，所有关键词匹配在本地完成，
  避免每个 exists() 都跑一趟 USB 往返；
- 250ms 内的重复查询共享同一次 dump（一个识别周期只付一次成本）；
- 关键点击全部走 Adb.tap（裸 input tap），本层只做“找到该点什么”。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from xml.etree import ElementTree


@dataclass
class Element:
    text: str
    bounds: Tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    resource_id: str = ""
    description: str = ""
    clazz: str = ""
    parent: Optional["Element"] = field(default=None, repr=False, compare=False)
    checked: bool = False

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2


class U2Unavailable(RuntimeError):
    pass


def import_u2():
    try:
        import uiautomator2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise U2Unavailable(
            "未安装 uiautomator2，请先: pip install -r requirements.txt"
        ) from exc
    return uiautomator2


def _parse_nodes(xml: str) -> List[Element]:
    """解析可见控件，并保留容器关系以识别同一搜索结果里的标题和城市。"""
    nodes: List[Element] = []

    def visit(xml_node, parent=None):
        attrs = xml_node.attrib
        l, t, r, b = _parse_bounds(attrs.get("bounds", ""))
        if attrs.get("visible-to-user") == "false":
            return
        if xml_node.tag == "node" and l < r and t < b:
            parent = Element(attrs.get("text", ""), (l, t, r, b),
                             attrs.get("clickable") == "true",
                             attrs.get("enabled") == "true",
                             attrs.get("resource-id", ""), attrs.get("content-desc", ""),
                             attrs.get("class", ""), parent,
                             attrs.get("checked") == "true" and
                             (attrs.get("checkable") == "true"
                              or "checkbox" in attrs.get("class", "").lower()))
            nodes.append(parent)
        for child in xml_node:
            visit(child, parent)

    if xml.strip():
        visit(ElementTree.fromstring(xml))
    return nodes


def _parse_bounds(raw: str) -> Tuple[int, int, int, int]:
    try:
        nums = raw.replace("][", ",").strip("[]").split(",")
        l, t, r, b = (int(v) for v in nums)
        return l, t, r, b
    except (ValueError, TypeError):
        return (0, 0, 0, 0)


class Device:
    """连接一台设备；serial 为空时由 u2 自动选择唯一设备。"""

    _DUMP_TTL_S = 0.25  # 同一识别周期内共享一次 dump

    def __init__(self, serial: Optional[str] = None):
        u2 = import_u2()
        if serial:
            self._d = u2.connect(serial)
        else:
            self._d = u2.connect()
        self.serial = serial or str(getattr(self._d, "serial", "") or "")
        self._d.settings["operation_delay"] = (0, 0)
        self._d.settings["wait_timeout"] = 3
        self._dumped_at = float("-inf")
        self._nodes: List[Element] = []
        self._fast_dump = False

    # ---- 快照 ---------------------------------------------------------

    def _snapshot(self) -> List[Element]:
        if time.monotonic() - self._dumped_at > self._DUMP_TTL_S:
            if self._fast_dump:
                # 开票热路径不等待 dump_hierarchy 遇到空树时的秒级重试。
                xml = self._d.jsonrpc.dumpWindowHierarchy(False, self._d.settings["max_depth"])
            else:
                xml = self._d.dump_hierarchy()
            self._nodes = _parse_nodes(xml or "")
            self._dumped_at = time.monotonic()
        return self._nodes

    def refresh(self, *, fast: bool = False) -> None:
        """强制丢弃缓存，下一次查询重新 dump（点击后调用）。"""
        self._fast_dump = fast
        self._dumped_at = float("-inf")

    def elements(self) -> List[Element]:
        return self._snapshot()

    # ---- 查询（全部本地匹配） ------------------------------------------

    def find(self, texts: List[str]) -> Optional[Element]:
        """按精确文字匹配第一个存在的控件；找不到返回 None。"""
        wanted = set(texts)
        for node in self._snapshot():
            if node.text in wanted or node.description in wanted:
                return node
        return None

    def has_checked_checkbox(self) -> bool:
        """是否存在勾选中的复选框（观演人预选检测；属性顺序不定双向匹配）。"""
        return any(node.checked for node in self._snapshot())

    def find_all(self, texts: List[str]) -> List[Element]:
        """按精确文字匹配全部控件（如所有「缺货登记」标签）。"""
        wanted = set(texts)
        return [n for n in self._snapshot()
                if n.text in wanted or n.description in wanted]

    def find_contains(self, text: str) -> Optional[Element]:
        """按包含文字匹配第一个控件（用于票档价格行、长错误文案）。"""
        for node in self._snapshot():
            if text in node.text or text in node.description:
                return node
        return None

    def exists_any(self, texts: List[str]) -> bool:
        return self.find(texts) is not None

    def exists_contains_any(self, texts: List[str]) -> bool:
        return any(self.find_contains(t) is not None for t in texts)

    def find_by_class(self, substr: str) -> Optional[Element]:
        """按类名包含匹配第一个节点（如 EditText）。"""
        for node in self._snapshot():
            if substr in node.clazz:
                return node
        return None

    def find_all_by_id(self, substr: str) -> List[Element]:
        """按 resource-id 子串匹配全部节点。"""
        return [n for n in self._snapshot() if substr in n.resource_id]

    def find_by_id(self, substr: str) -> Optional[Element]:
        """按 resource-id 子串匹配第一个节点（自绘控件无文字时靠 id 定位）。"""
        for node in self._snapshot():
            if substr in node.resource_id:
                return node
        return None

    def ime_text(self, text: str) -> bool:
        """向第一个输入框写入文字（uiautomator setText，支持中文，不走输入法）。"""
        sel = self._d(className="android.widget.EditText")
        if not sel.exists:
            return False
        sel.set_text(text)
        self.refresh()
        return True

    # ---- 探测 ---------------------------------------------------------

    def dump_clickables(self, limit: int = 40) -> List[Element]:
        """列出当前页面带文字的元素（probe/校准用）。"""
        self.refresh()
        return [n for n in self._snapshot() if n.text or n.description][:limit]
