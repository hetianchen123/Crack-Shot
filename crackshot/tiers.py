"""本地选票页识别，支持定位确认按钮及读取票档。"""

from dataclasses import dataclass
import re

from .digitmatch import orange, read_digits
from .show_details import session_matches

_SESSION_LIST = "project_detail_perform_flowlayout"
_PRICE_LIST = "project_detail_perform_price_flowlayout"
_CARD = "ll_perform_item"
_BOTTOM = "id_sku_bottom_bar_container"
_DATE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})")
_CLOCK = re.compile(r"([0-9]{2}:[0-9]{2})$")
_PRICE = re.compile(r"[¥￥]\s*([0-9,]+)|([0-9,]+)\s*元")
_UNAVAILABLE = ("缺货登记", "已售罄", "暂无余票", "不可购买", "可预约")


@dataclass(frozen=True)
class Selection:
    action: str
    point: tuple = ()
    reason: str = ""
    quantity: int = 0


def _id(node):
    return node.resource_id.rsplit("/", 1)[-1]


def _label(node):
    return (node.text or node.description).strip()


def _under(node, parent):
    while node.parent is not None:
        node = node.parent
        if node is parent:
            return True
    return False


def _crop(pixels, bounds):
    left, top, right, bottom = bounds
    if not (0 <= left < right <= pixels.shape[1] and 0 <= top < bottom <= pixels.shape[0]):
        return pixels[0:0, 0:0]
    return pixels[top:bottom, left:right, :3]


class _Card:
    def __init__(self, node, nodes):
        self.node = node
        self.children = [n for n in nodes if _under(n, node)]

    def native_selected(self):
        return any(n.selected or n.checked for n in (self.node, *self.children))

    def selected_border(self, screen):
        card = _crop(screen(), self.node.bounds)
        height, width = card.shape[:2]
        if height < 16 or width < 32:
            return False
        middle = card[:, width // 4:width * 3 // 4]
        edge = max(2, height // 10)
        return any(orange(band).any(axis=0).mean() >= .55
                   for band in (middle[:edge], middle[-edge:]))

    def unavailable(self):
        return any(_label(n) in _UNAVAILABLE for n in self.children)

    def _text_crop(self, screen):
        left, top, right, bottom = self.node.bounds
        height = bottom - top
        badge_start = min((n.bounds[0] for n in self.children
                           if _id(n) == "tv_tag" and n.bounds[0] > left), default=right)
        return _crop(screen(), (left + height // 6, top + height // 5,
                                min(right - height // 8, badge_start - 3),
                                bottom - height // 5))

    def session(self, screen):
        for node in (self.node, *self.children):
            label = _label(node)
            if _DATE.match(label):
                return label
        glyphs = read_digits(self._text_crop(screen)).replace(" ", "")
        date = _DATE.match(glyphs)
        if not date:
            return ""
        clock = _CLOCK.search(glyphs[date.end():])
        return f"{date[1]} {clock[1]}" if clock else date[1]

    def price(self, screen):
        for node in (self.node, *self.children):
            label = _label(node)
            match = _PRICE.search(label)
            if match:
                return int((match[1] or match[2]).replace(",", ""))
            if label.isascii() and label.isdecimal() and "price" in _id(node):
                return int(label)
        groups = re.findall(r"[0-9]+", read_digits(self._text_crop(screen)).replace(" ", ""))
        return int(groups[0]) if len(groups) == 1 and len(groups[0]) >= 2 else None


def _target(cards, screen, read_label, matches):
    selected = [card for card in cards if card.native_selected()]
    if not selected:
        selected = [card for card in cards if card.selected_border(screen)]
    if len(selected) > 1:
        return None, False
    if selected and matches(read_label(selected[0], screen)):
        return selected[0], True
    candidates = [card for card in cards if matches(read_label(card, screen))]
    return (candidates[0], False) if len(candidates) == 1 else (None, False)


def _read_confirm(nodes, bottom, capture) -> Selection:
    if bottom is None or not bottom.enabled:
        return Selection("wait", reason="选票页底栏未就绪")
    controls = [n for n in nodes if _under(n, bottom)]
    # 大麦的自绘按钮始终 enabled=true；未选好票时以遮罩禁用，不能只看 enabled。
    if any(_id(n) == "view_translucent" for n in controls):
        return Selection("wait", reason="确定按钮仍被禁用遮罩覆盖")
    confirm = next((n for n in controls if _label(n) == "确定"), None)
    if confirm is None:
        button = next((n for n in controls if _id(n) == "btn_buy_view" and n.enabled), None)
        total = next((_label(n).replace(",", "") for n in controls if _id(n) == "tv_price"), "")
        # 合计和禁用遮罩均在当前控件树中，不再截图检查按钮颜色。
        if button and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", total) and float(total) > 0:
            confirm = button
    if confirm and confirm.enabled:
        return Selection("confirm", confirm.center)
    return Selection("wait", reason="确定按钮暂不可用")


def read_confirm(nodes, capture, _task) -> Selection:
    """仅定位选票页可用的确认按钮，使用页面已有的预约选中项。"""
    return _read_confirm(nodes, next((n for n in nodes if _id(n) == _BOTTOM), None), capture)


def read_selection(nodes, capture, task) -> Selection:
    """每次读取基于同一组控件与最多一帧画面。"""
    frame = None

    def screen():
        nonlocal frame
        if frame is None:
            frame = capture()
        return frame

    by_id = {}
    for node in nodes:
        by_id.setdefault(_id(node), []).append(node)

    def first(rid):
        return next(iter(by_id.get(rid, ())), None)

    title = first("title_tv")
    if title and _label(title) and (task.show_name not in _label(title)
                                     or (task.city and task.city not in _label(title))):
        return Selection("blocked", reason="选票页演出或城市不符")

    def cards(rid):
        container = first(rid)
        return [_Card(n, nodes) for n in by_id.get(_CARD, ()) if container and _under(n, container)]

    if task.session:
        session, selected = _target(cards(_SESSION_LIST), screen, _Card.session,
                                    lambda label: session_matches(task.session, label))
        if not session:
            return Selection("wait", reason=f"无法核对场次 {task.session}")
        if session.unavailable():
            return Selection("sold_out", reason=f"目标场次 {task.session} 不可购买")
        if not selected:
            return Selection("session", session.node.center) if session.node.enabled else Selection("wait")

    price_text = task.price_tiers[0]
    if not re.fullmatch(r"[0-9]+", price_text) or int(price_text) <= 0:
        return Selection("blocked", reason="任务的目标票档金额无效")
    wanted = int(price_text)
    price, selected = _target(cards(_PRICE_LIST), screen, _Card.price, lambda amount: amount == wanted)
    if not price:
        return Selection("wait", reason=f"无法核对目标票档 ¥{wanted}")
    if price.unavailable():
        return Selection("sold_out", reason=f"目标票档 ¥{wanted} 显示缺货")
    if not selected:
        return Selection("price", price.node.center) if price.node.enabled else Selection("wait")

    bottom = first(_BOTTOM)
    if not bottom:
        return Selection("wait", reason="选票页底栏未出现")
    total_node = next((n for n in by_id.get("tv_price", ()) if _under(n, bottom)), None)
    amount = _label(total_node).replace(",", "") if total_node else ""
    if not re.fullmatch(r"[0-9]+(?:\.0+)?", amount):
        return Selection("wait", reason="无法核对订单合计")
    total = int(amount.split(".")[0])
    if total <= 0 or total % wanted:
        return Selection("wait", reason=f"订单合计 ¥{total} 与票档 ¥{wanted} 无法核对")
    confirm = _read_confirm(nodes, bottom, screen)
    if confirm.action == "confirm":
        return Selection("confirm", confirm.point, quantity=total // wanted)
    return confirm
