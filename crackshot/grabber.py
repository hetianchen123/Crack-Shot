"""抢票状态机：等按钮 → 进选票页 → 确定 → 提交订单；失败退出重试。

边界（不可越）：
- 遇到验证码/风控弹窗立即停止并通知人工，绝不尝试处理；
- 不点击任何支付/密码类元素（成功页交给人工支付）；
- 全部点击走 Adb.tap 全局限速；定时首击不加随机等待。
"""

from __future__ import annotations

import io
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from .adb import Adb, AdbError
from .config import TaskConfig
from .device import Device
from .notify import Notifier
from .opening import OpeningProbe
from .pages import ORDER_STOCK_MARKERS, SOLD_OUT_TEXTS, SUBMIT_TEXTS, PageState, recognize
from .tiers import read_confirm
from .timeutil import now_ms, sleep_jitter
from . import vision

log = logging.getLogger("crackshot.grabber")

_BUY_PROBE_LEAD_MS = 750
_FIXED_BUY_PREP_LEAD_MS = 1750
_BUY_TRANSITION_MS = 2000
_FIXED_BUY_TRANSITION_MS = 150
_FIXED_BUY_MAX_LATE_MS = 100
_UNKNOWN_TRANSITION_EXTRA_MS = 2000
_ORDER_ACTION_INTERVAL_MS = 800
_VISUAL_RESULT_MAX_AGE_MS = 1000


def _rand(span: int) -> int:
    """点击偏移抖动：[-span, span] 随机。"""
    import random as _random
    return _random.randint(-span, span)


class GrabOutcome:
    SUCCESS = "success"
    NEEDS_HUMAN = "needs_human"   # 验证码/页面不符等，已停下
    SOLD_OUT = "sold_out"
    TIMEOUT = "timeout"
    OFF_PAGE = "off_page"         # 长时间不在预期页面
    CANCELLED = "cancelled"       # 外部要求停止（web 后台取消等）


class Grabber:
    def __init__(
        self,
        adb: Adb,
        device: Device,
        task: TaskConfig,
        notifier: Notifier,
        evidence_dir: Path,
        dry_run: bool = False,
        stop_check: Optional[Callable[[], bool]] = None,
        button_finder=vision.find_buy_button,
        tier_reader=read_confirm,
    ):
        self.adb = adb
        self.device = device
        self.task = task
        self.notify = notifier
        self.evidence_dir = evidence_dir
        self.dry_run = dry_run
        self.stop_check = stop_check
        self._find_button = button_finder
        self._read_tier = tier_reader
        self._tier_wait_since = None
        self._tier_confirm_until_ms = 0
        self._capture_raw = (getattr(adb, "screencap_raw", None)
                             if button_finder is vision.find_buy_button else None)
        self._opening_probe = None
        self._purchase_pool = None
        self._purchase_scan = None
        self._recover_early_reservation = False
        self.deadline_ms = 0
        self.attempts = 0
        self._sale_at_ms = 0
        self._entered = True
        self._buy_transition_until_ms = 0
        self._unknown_after_buy = False
        self._submit_until_ms = 0
        self._queue_until_ms = 0
        self._last_order_action_ms = 0
        self._order_submissions = 0
        self._order_active = False
        self._await_continue_page = False
        self._verify_detail = False
        self._waiting_for_button = False
        self._needs_backoff = True  # 仅等待按钮翻转时为 False，避免轮询被退避拖慢

    # ------------------------------------------------------------------

    def _stop_requested(self) -> bool:
        return bool(self.stop_check and self.stop_check())

    def _sale_countdown_present(self) -> bool:
        """详情页是否仍显示开抢倒计时（= 未开票）。

        开票前该节点存在（“X月X日 XX:XX开抢”）；开票后消失、按钮转可购。
        这是页面自述状态，比本地时钟更可靠。
        """
        try:
            node = self.device.find_by_id("count_sell_time")
            if node is None:
                import re
                for marker in ("开抢", "开售"):
                    candidate = self.device.find_contains(marker)
                    if candidate and re.search(r"\d{1,2}:\d{2}|即将开[抢售]",
                                               candidate.text or candidate.description):
                        return True
            return node is not None
        except Exception:  # noqa: BLE001 —— 判定失败视为未开票（保守不点击）
            return True

    def _tap(self, x: int, y: int) -> None:
        """统一点击入口：注入点击后立刻失效控件快照，下次查询重新 dump。"""
        self._discard_purchase_vision()
        self.adb.tap(x, y)
        self.device.refresh()

    def _back(self) -> None:
        self._discard_purchase_vision()
        self.adb.back()
        self.device.refresh()

    def run(self, on_sale_ms: int, verify_detail: bool = False) -> str:
        """可提前进入循环，但开票时刻之前禁止购买点击。"""
        self.deadline_ms = on_sale_ms + self.task.max_retry_seconds * 1000
        self._sale_at_ms = on_sale_ms
        self._verify_detail = verify_detail
        self._entered = False
        if not self.dry_run and len(self.task.price_tiers) != 1:
            self.notify.critical("请先确认唯一预约票档，任务已停止")
            return GrabOutcome.NEEDS_HUMAN
        log.info("进入抢票循环，窗口 %s 秒，目标票档 %s",
                 self.task.max_retry_seconds, self.task.price_tiers[:1])

        # 坐标在开票前读取，到点路径只等待时钟并注入点击。
        self._purchase_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="purchase-vision")
        try:
            if verify_detail and self.task.blind_tap and now_ms() < on_sale_ms:
                outcome = self._timed_first_click(on_sale_ms)
                if outcome:
                    return outcome
            elif verify_detail and self._capture_raw and on_sale_ms - now_ms() >= 1000:
                self._opening_probe = OpeningProbe(self._capture_raw, on_sale_ms)
            return self._run_until_end(on_sale_ms)
        finally:
            self._discard_purchase_vision()
            self._purchase_pool.shutdown(wait=False, cancel_futures=True)
            self._purchase_pool = None

    def _timed_first_click(self, on_sale_ms: int) -> Optional[str]:
        """预热确认目标页面和坐标；开票瞬间不读控件、不截图。"""
        from .navigator import is_target_detail

        while True:
            if self._stop_requested():
                return GrabOutcome.CANCELLED
            left_ms = on_sale_ms - _FIXED_BUY_PREP_LEAD_MS - now_ms()
            if left_ms <= 0:
                break
            time.sleep(min(0.05, left_ms / 1000))
        check_started = now_ms()
        if not self.adb.is_damai_foreground():
            self.notify.critical("定时点击准备失败：大麦不在前台")
            return GrabOutcome.NEEDS_HUMAN
        self.device.refresh(fast=True)
        snap = recognize(self.device, self.task)
        if not is_target_detail(self.device, self.task, snap):
            self.notify.critical("定时点击准备失败：当前不在目标演出详情页")
            return GrabOutcome.NEEDS_HUMAN
        bar = self.device.find_by_id("purchase_status_bar")
        if not bar or not bar.enabled or now_ms() >= on_sale_ms:
            log.info("未能在开票前锁定坐标，改为等待可购按钮")
            return None
        if self._known_unavailable_button(bar):
            log.info("购买栏显示不可购买，等待页面状态更新")
            return None
        log.info("定时坐标已就绪 %s，开票前核验耗时 %dms；到点直接点击",
                 bar.center, now_ms() - check_started)
        while True:
            if self._stop_requested():
                return GrabOutcome.CANCELLED
            left_ms = on_sale_ms - now_ms()
            if left_ms <= 0:
                break
            time.sleep(min(0.05, left_ms / 1000))
        started = now_ms()
        if not 0 <= started - on_sale_ms <= _FIXED_BUY_MAX_LATE_MS:
            # 时钟调整或线程唤醒过晚时，不再使用之前锁定的坐标。
            return None
        if started >= self.deadline_ms:
            return GrabOutcome.TIMEOUT
        if self.dry_run:
            log.info("[dry-run] 定时首击 %s，距触发时刻 %+dms，未点击",
                     bar.center, started - on_sale_ms)
            return "guard_verified"
        self._entered = True
        try:
            self._tap(*bar.center)
        except AdbError as exc:
            log.warning("定时点击结果未知，先核对页面：%s", exc)
        ended = now_ms()
        self._waiting_for_button = False
        self._needs_backoff = False
        self._buy_transition_until_ms = ended + _FIXED_BUY_TRANSITION_MS
        self._unknown_after_buy = False
        self._recover_early_reservation = True
        log.info("定时坐标点击 %s，距开票 %+dms 发出，ADB 点击 %dms（无截图、无元素等待）",
                 bar.center, started - on_sale_ms, ended - started)
        return None

    def _run_until_end(self, on_sale_ms: int) -> str:
        probe_at_ms = on_sale_ms - _BUY_PROBE_LEAD_MS
        while now_ms() < max(self.deadline_ms, self._submit_until_ms, self._queue_until_ms):
            if self._stop_requested():
                log.info("收到取消请求，停止抢票")
                return GrabOutcome.CANCELLED
            if self.attempts >= self.task.max_attempts and not self._order_active:
                log.info("达到尝试次数上限 %s，停止", self.task.max_attempts)
                return GrabOutcome.TIMEOUT
            until_probe_ms = probe_at_ms - now_ms()
            if until_probe_ms > 0:
                time.sleep(min(0.2, until_probe_ms / 1000))
                continue
            self.device.refresh(fast=True)
            self._waiting_for_button = False
            try:
                outcome = self._attempt()
            except AdbError as exc:
                self.attempts += 1
                log.warning("adb 异常: %s", exc)
                sleep_jitter(self.task.retry_backoff_ms)
                continue
            if not self._waiting_for_button:
                self.attempts += 1
            if outcome:
                return outcome
            if self._needs_backoff:
                sleep_jitter(self.task.retry_backoff_ms)
        return GrabOutcome.TIMEOUT

    # ------------------------------------------------------------------

    def _attempt(self) -> Optional[str]:
        """单次完整尝试：详情页按钮 → 选票 → 提交。返回非 None 表示整轮结束。"""
        self._needs_backoff = True
        probe_started_ms = now_ms()
        snap = recognize(self.device, self.task)
        if snap.state != PageState.DETAIL:
            self._discard_purchase_vision()
        if snap.state in (PageState.ORDER_CONFIRM, PageState.ORDER_RETRY, PageState.QUEUE):
            self._order_active = True
        elif snap.state in (PageState.DETAIL, PageState.TIER_SHEET):
            self._order_active = False

        if snap.state == PageState.CAPTCHA:
            self._evidence("captcha")
            self.notify.critical("出现验证码/风控验证，请人工处理（脚本已停止）")
            return GrabOutcome.NEEDS_HUMAN

        if now_ms() < self._sale_at_ms and snap.state != PageState.DETAIL:
            return self._wait_for_button()

        if snap.state == PageState.STOCK_REGISTRATION:
            return self._sold_out("当前为缺货登记页")
        if snap.state == PageState.ORDER_SOLD_OUT:
            return self._sold_out(snap.matched)
        if snap.state == PageState.ORDER_REVIEW:
            self._evidence("order_review")
            self.notify.critical(f"确认购买页提示「{snap.matched}」，已停止，请在手机上核对")
            return GrabOutcome.NEEDS_HUMAN

        if not self._entered:
            if snap.state == PageState.UNKNOWN and now_ms() < self._sale_at_ms + _BUY_TRANSITION_MS:
                return self._wait_for_button()
            if (snap.state != PageState.DETAIL
                    and (self._verify_detail or snap.state != PageState.TIER_SHEET)):
                self.notify.critical(f"无法确认抢票起始页面（{snap.state.value}），请人工核对")
                return GrabOutcome.NEEDS_HUMAN
            self._entered = True

        if self._buy_transition_until_ms:
            if snap.state in (PageState.UNKNOWN, PageState.DETAIL):
                first_unknown = snap.state == PageState.UNKNOWN and not self._unknown_after_buy
                if first_unknown:
                    self._unknown_after_buy = True
                    log.info("购买点击后页面尚未识别，等待切换")
                transition_until = self._buy_transition_until_ms
                if snap.state == PageState.UNKNOWN:
                    transition_until += _UNKNOWN_TRANSITION_EXTRA_MS
                if first_unknown or now_ms() < transition_until:
                    return self._wait_for_button()
                if snap.state == PageState.UNKNOWN:
                    self._evidence("unknown_page")
                    self.notify.critical("购买点击后仍无法识别页面，请在手机上核对")
                    return GrabOutcome.NEEDS_HUMAN
                log.info("购买点击后仍在详情页，重新核对购买按钮")
            self._buy_transition_until_ms = 0

        if snap.state == PageState.SUCCESS:
            self.notify.success(f"[{self.task.name}] 下单成功，请尽快人工支付")
            self._evidence("success")
            return GrabOutcome.SUCCESS

        if snap.state == PageState.RESERVE:
            if self._recover_early_reservation:
                if self._stop_requested():
                    return GrabOutcome.CANCELLED
                log.info("定时首击进入预约页，返回详情等待正式开售")
                self._recover_early_reservation = False
                self._back()
                self._buy_transition_until_ms = now_ms() + _FIXED_BUY_TRANSITION_MS
                self._needs_backoff = False
                return None
            # 未开票演出的预约登记页：说明开票时间未到或配置有误
            self.notify.critical(
                f"[{self.task.name}] 该演出未开票（当前为预约登记页），"
                "请确认任务开票时间；正式开票后按钮会变为可购买状态")
            return GrabOutcome.NEEDS_HUMAN

        if self.dry_run and snap.state != PageState.DETAIL:
            self.notify.info("演练已停止：页面已离开详情页")
            return GrabOutcome.OFF_PAGE

        if self._await_continue_page and snap.state == PageState.UNKNOWN:
            return self._wait_for_order(self._last_order_action_ms + _BUY_TRANSITION_MS)
        self._await_continue_page = False
        if snap.state == PageState.ORDER_RETRY:
            return self._continue_order(snap.matched)
        if snap.state == PageState.QUEUE:
            if not self._queue_until_ms:
                self._queue_until_ms = now_ms() + self.task.queue_wait_seconds * 1000
                log.info("订单已进入排队或提交处理中")
            return self._wait_for_order(self._queue_until_ms)
        if snap.state == PageState.ERROR_DIALOG and any(
                word in snap.matched for word in SOLD_OUT_TEXTS + ORDER_STOCK_MARKERS):
            return self._sold_out(snap.matched)
        if snap.state == PageState.DETAIL and self._detail_sold_out():
            return self._sold_out("详情页显示缺货")
        pending = self._queue_until_ms or self._submit_until_ms
        if pending:
            if snap.state != PageState.ERROR_DIALOG:
                return self._wait_for_order(pending)
            self._queue_until_ms = self._submit_until_ms = 0

        if snap.state == PageState.DETAIL:
            if self._verify_detail:
                from .navigator import is_target_detail
                if not is_target_detail(self.device, self.task, snap):
                    self.notify.critical("当前详情页与任务演出或城市不符，已停止")
                    return GrabOutcome.NEEDS_HUMAN
            self._needs_backoff = False
            return self._from_detail(probe_started_ms)
        if snap.state == PageState.TIER_SHEET:
            self._recover_early_reservation = False
            return self._from_tier_sheet()
        if snap.state == PageState.ORDER_CONFIRM:
            return self._from_order_confirm()

        from .navigator import dismiss_ticket_notice
        if dismiss_ticket_notice(self.device, self.adb):
            return None
        if snap.state == PageState.ERROR_DIALOG:
            log.info("已识别失败弹窗（%s），关闭后重试", snap.matched)
            self._dismiss_error_and_back()
            return None
        self._evidence("unknown_page")
        self.notify.critical(f"出现未登记的页面（{snap.state.value}），已停止，请人工核对")
        return GrabOutcome.NEEDS_HUMAN

    # ---- 各页面处理 ----------------------------------------------------

    def _sold_out(self, reason: str) -> str:
        log.info("停止抢票：%s", reason)
        self._evidence("sold_out")
        return GrabOutcome.SOLD_OUT

    def _detail_sold_out(self) -> bool:
        if now_ms() < self._sale_at_ms or self._sale_countdown_present():
            return False
        return self._known_unavailable_button(self.device.find_by_id("purchase_status_bar"))

    def _known_unavailable_button(self, bar) -> bool:
        if not bar:
            return False
        left, top, right, bottom = bar.bounds
        return any(left <= x <= right and top <= y <= bottom
                   for x, y in (n.center for n in self.device.find_all(SOLD_OUT_TEXTS)))

    def _wait_for_button(self) -> None:
        self._needs_backoff = False
        self._waiting_for_button = True
        until_sale_ms = self._sale_at_ms - now_ms()
        if until_sale_ms > 0:
            time.sleep(min(0.15, until_sale_ms / 1000))
        elif self._buy_transition_until_ms or self._purchase_scan or self._opening_probe:
            time.sleep(0.01)
        else:
            sleep_jitter(self.task.poll_interval_ms)

    def _from_detail(self, probe_started_ms: int) -> Optional[str]:
        """详情页：优先识别可购文字，自绘按钮在底部购买栏内用视觉定位。"""
        self._waiting_for_button = True
        if probe_started_ms < self._sale_at_ms <= now_ms():
            return None
        buy = self.device.find(self.task.buy_texts)
        wait = buy is None and (
            self._sale_countdown_present()
            or self.device.find(self.task.wait_texts + ["立即预约", "已预约"]) is not None
        )
        if wait:
            self._discard_purchase_scan()
        if not wait:
            bbox = buy.bounds if buy and buy.enabled else None
            ui_read_ms = now_ms()
            source = "文字"
            capture_ms = 0
            if buy is None:
                if ui_read_ms < self._sale_at_ms:
                    return self._wait_for_button()
                bar = self.device.find_by_id("purchase_status_bar")
                ui_read_ms = now_ms()
                bbox, source, capture_ms = self._purchase_candidate(bar, probe_started_ms)
                if bbox and bar:
                    l, t, r, b = bar.bounds
                    x1, y1, x2, y2 = bbox
                    bbox = (max(l, x1), max(t, y1), min(r, x2), min(b, y2))
                    # 详情购买按钮占据大部分购买栏；选票页的确定按钮只在右侧。
                    if bbox[2] - bbox[0] < max(40, (r - l) * .60) or bbox[3] - bbox[1] < 30:
                        bbox = None
            if bbox:
                until_sale_ms = self._sale_at_ms - now_ms()
                if until_sale_ms > 0:
                    # 开票前读到可购信号时，到点后复核再点击。
                    time.sleep(min(0.15, until_sale_ms / 1000))
                    return None
                if now_ms() >= self.deadline_ms:
                    return GrabOutcome.TIMEOUT
                if self._stop_requested():
                    return GrabOutcome.CANCELLED
                if self.dry_run:
                    log.info("[dry-run] 已识别可购按钮 %s，未点击", bbox)
                    return "guard_verified"
                self._waiting_for_button = False
                self._recover_early_reservation = False
                tap_start_ms = now_ms()
                x, y = vision.random_point(bbox)
                self._tap(x, y)
                tap_end_ms = now_ms()
                self._buy_transition_until_ms = tap_end_ms + _BUY_TRANSITION_MS
                self._unknown_after_buy = False
                log.info("点击已激活购买按钮 (%s,%s)，距开票 %+dms 发出，"
                         "控件核验 %dms，%s采样 %dms，核验后至点击 %dms，ADB 点击 %dms",
                         x, y, tap_start_ms - self._sale_at_ms, ui_read_ms - probe_started_ms,
                         source, capture_ms, tap_start_ms - ui_read_ms, tap_end_ms - tap_start_ms)
                return None
        return self._wait_for_button()

    def _discard_purchase_scan(self):
        if self._purchase_scan:
            self._purchase_scan[1].cancel()
            self._purchase_scan = None

    def _discard_purchase_vision(self):
        self._discard_purchase_scan()
        if self._opening_probe:
            self._opening_probe.close()
            self._opening_probe = None

    def _purchase_candidate(self, bar, probe_started_ms):
        """只取已完成的视觉结果，并用画面采集后的控件树核对当前页面。"""
        if self._verify_detail and (not bar or not bar.enabled):
            self._discard_purchase_vision()
            return None, "", 0
        if bar and self._opening_probe:
            candidate = self._opening_probe.result(bar.bounds, observed_at_ms=probe_started_ms)
            if candidate:
                return candidate[0], "并行原始帧", candidate[1]
            if not self._opening_probe.ready:
                return None, "", 0
            self._discard_purchase_vision()

        bounds = bar.bounds if bar else None
        if self._purchase_scan:
            previous_bounds, future = self._purchase_scan
            if previous_bounds != bounds:
                self._discard_purchase_scan()
            elif not future.done():
                return None, "", 0
            else:
                bbox, source, started, finished = future.result()
                if probe_started_ms < finished:
                    return None, "", 0
                self._purchase_scan = None
                if now_ms() - finished <= _VISUAL_RESULT_MAX_AGE_MS:
                    return bbox, source, finished - started
        if self._purchase_pool:
            self._purchase_scan = (bounds, self._purchase_pool.submit(self._locate_purchase_button))
        return None, "", 0

    def _locate_purchase_button(self):
        """视觉线程通过独立的 ADB 截图读取画面，不读控件树或点击。"""
        start = now_ms()
        if self._capture_raw:
            try:
                bbox, _ = vision.opening_frame(self._capture_raw())
                return bbox, "异步原始帧", start, now_ms()
            except (AdbError, OSError, ValueError) as exc:
                log.debug("原始截图不可用，回退 PNG: %s", exc)
        try:
            bbox = self._find_button(self.adb.screencap_bytes())
        except (AdbError, OSError, ValueError, subprocess.SubprocessError) as exc:
            log.debug("视觉检测异常: %s", exc)
            bbox = None
        return bbox, "异步 PNG", start, now_ms()

    def _tier_pixels(self):
        try:
            return np.asarray(self.device.fast_screenshot())
        except Exception as exc:  # noqa: BLE001
            log.debug("选票快速截图不可用：%s", exc)
        if self._capture_raw:
            try:
                width, height, data = self._capture_raw()
                return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)
            except (AdbError, OSError, ValueError) as exc:
                log.debug("选票原始帧不可用，改用 PNG: %s", exc)
        with Image.open(io.BytesIO(self.adb.screencap_bytes())) as image:
            return np.asarray(image.convert("RGB"))

    def _from_tier_sheet(self) -> Optional[str]:
        """沿用预约选中项，点击可用的「确定」。"""
        if now_ms() < self._tier_confirm_until_ms:
            return self._wait_for_order(self._tier_confirm_until_ms)
        started = now_ms()
        decision = self._read_tier(self.device.elements(), self._tier_pixels, self.task)
        if decision.action == "sold_out":
            return self._sold_out(decision.reason)
        if decision.action == "blocked":
            self.notify.critical(decision.reason)
            return GrabOutcome.NEEDS_HUMAN
        if decision.action == "wait":
            if self._tier_wait_since is None:
                self._tier_wait_since = started
            if now_ms() - self._tier_wait_since >= 2500:
                self.notify.critical(f"选票页需要人工处理：{decision.reason or '控件未就绪'}")
                return GrabOutcome.NEEDS_HUMAN
            return self._wait_for_order(self.deadline_ms)

        self._tier_wait_since = None
        if self._stop_requested():
            return GrabOutcome.CANCELLED
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT
        self._tap(*decision.point)
        self._needs_backoff = False
        if decision.action == "confirm":
            self._tier_confirm_until_ms = now_ms() + _BUY_TRANSITION_MS
            self._buy_transition_until_ms = self._tier_confirm_until_ms
            self._unknown_after_buy = False
            log.info("点击选票页已启用的确定按钮；定位及点击 %sms", now_ms() - started)
        else:
            log.info("点击目标%s，下一帧重新核对", "场次" if decision.action == "session" else "票档")
        return None

    def _from_order_confirm(self) -> Optional[str]:
        """确认购买页：核对观演人已选中 → 点「立即提交」→ 观察结果。"""
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT
        # 观演人：要求至少一个 checkbox 选中（预填后默认第一位已选中）；
        # 未选中则点第一位观演人
        try:
            checked = (self.device.has_checked_checkbox()
                       or self.device.exists_any(["所需观演人已选齐"]))
        except Exception:  # noqa: BLE001
            checked = False
        if not checked:
            first = self.device.find_by_id("text_name")
            if first:
                log.info("观演人未选中，点选第一位「%s」", first.text)
                if not self.dry_run:
                    self._tap(first.center[0] + _rand(60), first.center[1] + _rand(10))
                    sleep_jitter(self.task.confirm_wait_ms)
                return None  # 下一轮复查选中态
            self.notify.critical(
                f"[{self.task.name}] 订单页未找到观演人列表，请检查账号观演人设置")
            return GrabOutcome.NEEDS_HUMAN

        submit = self.device.find(SUBMIT_TEXTS)
        if not submit or not submit.enabled:
            return self._wait_for_order(self.deadline_ms)
        if self.device.exists_any(["请选择观演人", "添加观演人"]):
            self.notify.critical(f"[{self.task.name}] 订单页观演人未预填，请人工处理")
            return GrabOutcome.NEEDS_HUMAN
        if self.dry_run:
            return "guard_verified"

        if self._order_submissions and now_ms() - self._last_order_action_ms < _ORDER_ACTION_INTERVAL_MS:
            return self._wait_for_order(self.deadline_ms)
        if self._stop_requested():
            return GrabOutcome.CANCELLED
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT

        # 点击返回结果不明时仍视为已提交，直到页面明确给出重试结果。
        self._last_order_action_ms = now_ms()
        self._order_submissions += 1
        self._submit_until_ms = self._last_order_action_ms + self.task.post_submit_watch_seconds * 1000
        self._tap(*submit.center)
        log.info("点击提交订单 (%s,%s)，等待明确结果", *submit.center)
        self._needs_backoff = False
        return None

    def _continue_order(self, action_text: str) -> Optional[str]:
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT
        if now_ms() - self._last_order_action_ms < _ORDER_ACTION_INTERVAL_MS:
            return self._wait_for_order(self.deadline_ms)
        button = self.device.find([action_text])
        if not button or not button.enabled:
            return self._wait_for_order(self.deadline_ms)
        if self._stop_requested():
            return GrabOutcome.CANCELLED
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT
        self._last_order_action_ms = now_ms()
        self._await_continue_page = True
        self._submit_until_ms = self._queue_until_ms = 0
        self._tap(*button.center)
        self._needs_backoff = False
        log.info("已点「%s」，等待页面就绪及订单操作冷却", action_text)
        return None

    def _wait_for_order(self, until_ms: int) -> Optional[str]:
        if now_ms() >= until_ms:
            self._evidence("order_status_timeout")
            self.notify.critical("订单状态仍未明确，请在手机上核对")
            return GrabOutcome.NEEDS_HUMAN
        self._waiting_for_button = True
        self._needs_backoff = False
        time.sleep(0.055)
        return None

    # ---- 工具 ----------------------------------------------------------

    def _dismiss_error_and_back(self) -> None:
        """关失败弹窗（点确定/知道），再 back 一次退回上一页。"""
        if not self.dry_run:
            btn = self.device.find(self.task.error_button_texts)
            if btn:
                x, y = btn.center
                log.info("关闭弹窗“%s” (%s,%s)", btn.text, x, y)
                self._tap(x, y)
                sleep_jitter(self.task.confirm_wait_ms)
                return  # 关掉弹窗通常就回到可重试页面，无需再 back
            self._back()
        sleep_jitter(self.task.retry_backoff_ms)

    def _evidence(self, tag: str) -> None:
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"{tag}-{now_ms()}.png"
            self.adb.screencap(path)
            log.info("证据截图: %s", path)
        except (AdbError, OSError) as exc:
            log.warning("截图失败: %s", exc)
