"""App 内导航：启动大麦 → 搜索 → 进入目标演出详情页。

放弃深链方案（分享链接只落 H5）；全部通过 UI 操作完成，各步骤已在
真机验证：首页「搜索」按钮 → 输入框中文写入（uiautomator setText）→
联想词点击 → 搜索结果城市卡片点击 → 详情页。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .adb import Adb, AdbError, DAMAI_LAUNCH_ACTIVITY
from .config import TaskConfig
from .device import Device, Element
from .notify import Notifier
from .pages import PageSnapshot, PageState, recognize, ticket_notice_button
from .timeutil import sleep_jitter

log = logging.getLogger("crackshot.navigator")

# 每步之间的基础等待（页面过渡），再叠加随机抖动
_STEP_SLEEP = (0.8, 1.5)


def _snapshot_retry(device: Device, tries: int = 3) -> None:
    """uiautomator dump 在页面过渡时会偶发空树，强制刷新几次。"""
    for _ in range(tries):
        device.refresh()
        try:
            if device.dump_clickables(limit=5):
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)


def _bring_foreground(adb: Adb, timeout: float = 12.0) -> bool:
    """确保大麦在前台（可能恢复到上次页面，不保证首页）。"""
    try:
        adb.shell("am", "start", "-a", "android.intent.action.MAIN",
                  "-c", "android.intent.category.LAUNCHER",
                  "-n", DAMAI_LAUNCH_ACTIVITY,
                  "-f", "0x10000000")  # NEW_TASK：恢复已有任务栈到前台
    except AdbError as exc:
        log.error("拉起大麦失败: %s", exc)
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if adb.is_damai_foreground():
            return True
        time.sleep(0.8)
    return False


def _at_home(device: Device) -> bool:
    """首页特征：分类球（bricks_ball_name）或底部「首页」tab。"""
    if device.find_by_id("bricks_ball_name"):
        return True
    return device.find(["首页"]) is not None


def _ensure_home(adb: Adb, device: Device) -> bool:
    """确保在大麦首页：不在首页就 back 逐层返回，出现首页特征即停。"""
    for _ in range(5):
        _snapshot_retry(device)
        if _at_home(device):
            return True
        if not adb.is_damai_foreground():
            # back 退出过头了，重新拉起
            if not _bring_foreground(adb):
                return False
            continue
        adb.back()
        sleep_jitter((0.8, 1.4))
    _snapshot_retry(device)
    return _at_home(device)


def open_search(device: Device, adb: Adb) -> bool:
    """从大麦首页打开搜索页。

    首页顶栏节点在 uiautomator dump 中偶发缺失（自绘 header），找不到
    '搜索' 节点时按相对坐标点搜索胶囊（顶栏 y≈6.6% 屏高，胶囊 x≈47%
    宽、按钮 x≈88% 宽，真机 1176x2400 验证），以 EditText 出现为成功判据。
    """
    from PIL import Image
    import io as _io

    def _tap_search_bar() -> None:
        try:
            png = adb.screencap_bytes()
            w, h = Image.open(_io.BytesIO(png)).size
        except Exception:  # noqa: BLE001 —— 截图失败用常见分辨率兜底
            w, h = 1176, 2400
        adb.tap(int(w * 0.47), int(h * 0.066))

    for attempt in range(3):
        _snapshot_retry(device)
        entry = None
        for node in device.dump_clickables(limit=60):
            if node.text == "搜索" and node.clickable:
                entry = node
                break
        if entry:
            adb.tap(*entry.center)
        else:
            log.info("dump 未见搜索节点（第 %s 次），按相对坐标点搜索胶囊", attempt + 1)
            _tap_search_bar()
        # 搜索页加载有延迟，多次重试确认 EditText 再下结论（过早 back 会把
        # 刚打开的搜索页关掉，之前三次循环全部因此作废）
        for _ in range(4):
            sleep_jitter(_STEP_SLEEP)
            device.refresh()
            if device.find_by_class("EditText"):
                return True
        if not _ensure_home(adb, device):
            return False
    return False


def submit_search(device: Device, adb: Adb, task: TaskConfig) -> bool:
    """写入关键词并触发搜索：优先点联想词（官方完整演出名），否则回车。"""
    if not device.ime_text(task.show_name):
        log.warning("未找到搜索输入框")
        return False
    sleep_jitter((0.4, 0.9))
    adb.back()  # 收起输入法，联想词仍保留（真机验证）
    sleep_jitter((0.5, 1.0))
    _snapshot_retry(device)
    # 联想词选择：跳过与关键词相同的词（点了等于没搜）；
    # 优先 含演出名+城市 > 含演出名的长词（官方完整演出名）
    suggestion = None
    fallback = None
    for node in device.dump_clickables(limit=40):
        text = node.text
        if not text or text.strip() == task.show_name:
            continue
        if task.show_name in text:
            if task.city and task.city in text:
                suggestion = node
                break
            fallback = fallback or node
    suggestion = suggestion or fallback
    if suggestion:
        log.info("点击联想词: %s", suggestion.text)
        adb.tap(*suggestion.center)
        sleep_jitter((1.2, 2.0))
        return True
    # 无联想词：直接回车搜索
    device._d.press("enter")  # noqa: SLF001 —— u2 按键无公开封装
    sleep_jitter((1.2, 2.0))
    return True


def _label(node: Element) -> str:
    return node.text or node.description


def _inside(child: Element, ancestor: Element) -> bool:
    parent = child.parent
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.parent
    return False


def _search_result(nodes, task: TaskConfig) -> Optional[Element]:
    """只从当前可见的一屏选结果，不把输入框或其他演出的城市当作结果。"""
    if not nodes:
        return None
    height = max(n.bounds[3] for n in nodes)
    header = max((n.bounds[3] for n in nodes if n.clazz.endswith("EditText")
                  or "header_search_v2" in n.resource_id), default=0)
    visible = [n for n in nodes if n.enabled and _label(n)
               and n.center[1] > max(header, height * 0.10)
               and not n.clazz.endswith("EditText")]
    titles = [n for n in visible if task.show_name in _label(n)
              and (_label(n).strip() != task.show_name
                   or n.clickable or "title" in n.resource_id)]
    if not task.city:
        return titles[0] if titles else None

    cities = [n for n in visible if task.city in _label(n)]
    grid = [n for n in cities if "tv_city" in n.resource_id]
    # 城市和标题可能各占一行，必须属于同一张卡片。
    scoped = []
    for city_node in cities:
        parent = city_node.parent
        while parent is not None:
            if parent.bounds[3] - parent.bounds[1] > height * 0.45:
                break
            if any(t is city_node or _inside(t, parent) for t in titles):
                scoped.append(city_node)
                break
            parent = parent.parent

    for matches in ([n for n in grid if any(n is x for x in scoped)],
                    [n for n in titles if task.city in _label(n)], scoped):
        if matches:
            return matches[0]
    if len(titles) == 1:
        # 唯一同名聚合项可以进入；最终仍必须确认目标城市的单站详情。
        return grid[0] if len(grid) == 1 else titles[0]
    return None


def tap_result(device: Device, adb: Adb, task: TaskConfig, stop_check=None) -> bool:
    """支持多城方格和竖排列表，只等待首屏加载，不滑动。"""
    for _ in range(4):
        if stop_check and stop_check():
            return False
        _snapshot_retry(device)
        if is_target_detail(device, task) or _is_tour(device):
            return True
        if recognize(device, task).state == PageState.CAPTCHA:
            return False
        pick = _search_result(device.elements(), task)
        if pick:
            if stop_check and stop_check():
                return False
            log.info("点击首屏%s: %s @%s", "城市方格" if "tv_city" in pick.resource_id
                     else "搜索结果", _label(pick)[:46], pick.center)
            adb.tap(*pick.center)
            sleep_jitter((1.0, 1.8))
            return True
        time.sleep(0.6)
    log.warning("首屏没有匹配的演出和城市（%s / %s）", task.show_name, task.city)
    return False


def _is_tour(device: Device) -> bool:
    # 单站页也有其它城市的巡演横栏；底部购买栏才是单站特征。
    if device.find_by_id("purchase_status_bar") is not None:
        return False
    return (device.find_by_id("header_search_v2") is None
            and (device.find_by_id("city_card") is not None
                 or device.exists_contains_any(["城市巡演中"])))


def is_target_detail(device: Device, task: TaskConfig, snap: Optional[PageSnapshot] = None) -> bool:
    if (snap or recognize(device, task)).state != PageState.DETAIL or _is_tour(device):
        return False
    if device.find_by_id("header_search_v2") is not None:
        return False
    title = next((n for n in device.find_all_by_id("info_v2_title") if _label(n)), None)
    if title:
        if task.show_name not in _label(title):
            return False
    elif not device.exists_contains_any([task.show_name]):
        return False
    if not task.city:
        return True
    return (bool(title and task.city in _label(title))
            or any(task.city in _label(n) for n in device.elements()
                   if any(hint in n.resource_id for hint in ("venue_name", "venue_address",
                                                              "count_sell_time"))))


def dismiss_ticket_notice(device: Device, adb: Adb) -> bool:
    """只关闭已识别的提示，保留用户预选的观演人。"""
    btn = ticket_notice_button(device)
    if not btn or not btn.enabled:
        return False
    log.info("关闭详情页提示：%s @%s", _label(btn), btn.center)
    adb.tap(*btn.center)
    time.sleep(.20)
    device.refresh(fast=True)
    return True


def _enter_city_card(device: Device, adb: Adb, task: TaskConfig) -> bool:
    """若当前是巡演聚合页，点目标城市卡片进入单站详情页。"""
    for _ in range(2):
        _snapshot_retry(device)
        if device.find_by_id("header_search_v2") is not None:
            return False  # 还在搜索页（结果卡的城市行不可点，别误触）
        if not _is_tour(device):
            return False
        if not task.city:
            log.warning("巡演聚合页但任务未配置 city，请配置后重试")
            return False
        card = next((n for n in device.elements()
                     if n.enabled and task.city in _label(n)
                     and any(hint in n.resource_id for hint in ("tour_city_name", "city_card",
                                                                  "count_sell_time"))), None)
        card = card or device.find_contains(f"{task.city}站")
        if card:
            log.info("聚合页点击城市卡片: %s @%s", card.text[:20], card.center)
            adb.tap(*card.center)
            sleep_jitter((1.5, 2.5))
            return True
        time.sleep(1.0)
    return False


def navigate_to_detail(adb: Adb, device: Device, task: TaskConfig,
                       notify: Optional[Notifier] = None, stop_check=None) -> bool:
    """完整导航：确保大麦前台 → 搜索 → 点结果 → 校验目标单站。"""
    stopped = stop_check or (lambda: False)
    if stopped():
        return False
    if not adb.is_damai_foreground():
        log.info("大麦不在前台，自动拉起")
        if not _bring_foreground(adb):
            return False

    _snapshot_retry(device)
    if recognize(device, task).state == PageState.CAPTCHA:
        return False
    dismiss_ticket_notice(device, adb)
    if is_target_detail(device, task):
        return True

    if stopped():
        return False
    if _is_tour(device) and device.exists_contains_any([task.show_name]):
        _enter_city_card(device, adb, task)
        dismiss_ticket_notice(device, adb)
        if is_target_detail(device, task):
            return True

    if stopped() or not _ensure_home(adb, device):
        log.warning("未能回到大麦首页")
        return False
    if stopped() or not open_search(device, adb):
        log.warning("未找到搜索入口")
        return False
    if stopped() or not submit_search(device, adb, task):
        return False
    if not tap_result(device, adb, task, stop_check):
        return False

    for _ in range(4):
        if stopped():
            return False
        _snapshot_retry(device)
        if recognize(device, task).state == PageState.CAPTCHA:
            return False
        dismiss_ticket_notice(device, adb)
        if is_target_detail(device, task):
            log.info("已到达目标演出详情页: %s %s", task.show_name, task.city)
            return True
        if _is_tour(device):
            _enter_city_card(device, adb, task)
        time.sleep(0.5)
    log.warning("导航后未确认目标详情页（%s / %s）", task.show_name, task.city)
    return False
