"""页面状态识别：把当前屏幕归类为抢票流程中的一个状态。

识别顺序：验证码 → 失败弹窗 → 成功 → 排队 → 提交订单 → 选票页 → 详情页 → 未知。
全部基于关键词命中，关键词可在任务配置里覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from .config import TaskConfig
from .device import Device


class PageState(Enum):
    DETAIL = "detail"                # 演出详情页（含底部按钮的四种状态）
    RESERVE = "reserve"              # 预约登记页（未开票演出的"预约想看"，非购票流程）
    STOCK_REGISTRATION = "stock_registration"  # 缺货登记页，与正常选票页共用票档布局
    TIER_SHEET = "tier_sheet"        # 选票浮层（场次/票档/确定）
    ORDER_CONFIRM = "order_confirm"  # 确认订单页（观演人/提交订单）
    QUEUE = "queue"                  # 排队中
    SUCCESS = "success"              # 下单成功/待支付
    ERROR_DIALOG = "error_dialog"    # 失败/售罄等提示弹窗
    CAPTCHA = "captcha"              # 验证码/风控——必须交人工
    UNKNOWN = "unknown"


@dataclass
class PageSnapshot:
    state: PageState
    matched: str = ""       # 命中的关键词，写日志用

    @property
    def is_detail_like(self) -> bool:
        return self.state in (PageState.DETAIL, PageState.TIER_SHEET)


# 选票页（2026-11 版为独立页面）：'票档'标签在控件树可读（tv_price_name）
TIER_SHEET_MARKERS = ["票档", "每笔订单限购", "先到先得"]
# 订单确认页提交按钮：实测文案为'立即提交'（旧版本为'提交订单'）
SUBMIT_TEXTS = ["立即提交", "提交订单"]
QUEUE_MARKERS = ["排队中", "正在排队", "前方排队"]
SUCCESS_MARKERS = ["订单提交成功", "立即支付", "支付剩余时间", "去支付"]
# 详情页判定必须用详情页独有控件 id——搜索结果页的卡片同样带有
# 演出名/「缺货登记」/「开抢」文字，靠文字会误判（真机踩坑）。
# 单站详情页：info_v2_title（标题）/ purchase_status_bar（底部购买栏）；
# 巡演聚合页：count_sell_time（各城开抢时间）/ tour_city_name（城市卡）
DETAIL_ID_HINTS = ("purchase_status_bar", "info_v2_title",
                   "count_sell_time", "tour_city_name")


def _is_detail_page(device: Device) -> Optional[str]:
    for hint in DETAIL_ID_HINTS:
        el = device.find_by_id(hint)
        if el:
            return hint
    return None


def recognize(device: Device, task: TaskConfig) -> PageSnapshot:
    if device.exists_contains_any(task.captcha_texts):
        return PageSnapshot(PageState.CAPTCHA, "验证码关键词")
    # 失败提示多为长文案（contains），但必须同时存在弹窗按钮才按弹窗处理，
    # 否则“已售罄”这类详情页按钮文字会被误判成失败弹窗
    err_hit = _hit_contains(device, task.error_texts)
    if err_hit and device.find(task.error_button_texts):
        return PageSnapshot(PageState.ERROR_DIALOG, err_hit)
    if device.exists_contains_any(SUCCESS_MARKERS):
        return PageSnapshot(PageState.SUCCESS, _hit_contains(device, SUCCESS_MARKERS))
    if device.exists_contains_any(QUEUE_MARKERS):
        return PageSnapshot(PageState.QUEUE, _hit_contains(device, QUEUE_MARKERS))
    # 订单确认页必须有提交按钮——详情页也会出现“观演人”文案，不能单靠它判定
    if device.find(SUBMIT_TEXTS):
        return PageSnapshot(PageState.ORDER_CONFIRM, "提交按钮")
    # 预约登记页（未开票）：同样是 tv_price_name 布局但语义完全不同，
    # 必须先于选票页判定（“预约想看票档”包含“票档”但不是购票）
    if device.find_contains("预约想看") is not None:
        return PageSnapshot(PageState.RESERVE, "预约想想看标记")
    if (device.find(["提交缺货登记"])
            or device.exists_contains_any(["暂时无票，若到货", "若始终缺货，将不再另外通知"])):
        return PageSnapshot(PageState.STOCK_REGISTRATION, "缺货登记页")
    if device.find(TIER_SHEET_MARKERS) or device.find_by_id("tv_price_name"):
        return PageSnapshot(PageState.TIER_SHEET, "票档标签")
    # 详情页：仅认详情页/聚合页独有控件 id（文字判定已被搜索结果页
    # 卡片证伪——它们同样含演出名、缺货登记、开抢等字样）
    hint = _is_detail_page(device)
    if hint:
        return PageSnapshot(PageState.DETAIL, f"详情页特征:{hint}")
    return PageSnapshot(PageState.UNKNOWN, "")


def _hit(device: Device, texts: List[str]) -> Optional[str]:
    for text in texts:
        if device.exists_any([text]):
            return text
    return None


def _hit_contains(device: Device, texts: List[str]) -> Optional[str]:
    for text in texts:
        if device.exists_contains_any([text]):
            return text
    return None


def detail_button_state(device: Device, task: TaskConfig) -> PageSnapshot:
    """详情页定位状态（probe/watch 展示用；点击路径走 vision.py 像素检测）。"""
    hint = _is_detail_page(device)
    if hint:
        return PageSnapshot(PageState.DETAIL, f"详情页特征:{hint}")
    buy = device.find(task.buy_texts)
    if buy and buy.enabled:
        return PageSnapshot(PageState.DETAIL, f"可购买按钮:{buy.text}")
    wait = device.find(task.wait_texts)
    if wait:
        return PageSnapshot(PageState.DETAIL, f"等待中:{wait.text}")
    return PageSnapshot(PageState.UNKNOWN, "未找到底部按钮")
