"""抢票状态机：等按钮 → 进选票页 → 选票档 → 确定 → 提交订单；失败退出重试。

边界（不可越）：
- 遇到验证码/风控弹窗立即停止并通知人工，绝不尝试处理；
- 不点击任何支付/密码类元素（成功页交给人工支付）；
- 全部点击走 Adb.tap 全局限速；时序全部带随机抖动。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from .adb import Adb, AdbError
from .config import TaskConfig
from .device import Device
from .notify import Notifier
from .pages import ORDER_RETRY_TEXTS, SOLD_OUT_TEXTS, SUBMIT_TEXTS, PageState, recognize
from .show_details import session_matches
from .timeutil import format_remaining, jitter_ms, now_ms, sleep_jitter
from . import vision

log = logging.getLogger("crackshot.grabber")

_BUY_PROBE_LEAD_MS = 750
_BUY_TRANSITION_MS = 2000
_UNKNOWN_TRANSITION_EXTRA_MS = 2000
_CONTINUE_COOLDOWN_MS = 650


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
        ollama_url: str = "http://127.0.0.1:11434",
        vlm_model: str = "qwen3-vl:4b-instruct",
        tier_parser=None,
    ):
        self.adb = adb
        self.device = device
        self.task = task
        self.notify = notifier
        self.evidence_dir = evidence_dir
        self.dry_run = dry_run
        self.stop_check = stop_check
        self._find_button = button_finder
        self.ollama_url = ollama_url
        self.vlm_model = vlm_model
        self._parse_tiers = tier_parser
        self.deadline_ms = 0
        self.attempts = 0
        self._sale_at_ms = 0
        self._entered = True
        self._buy_transition_until_ms = 0
        self._unknown_after_buy = False
        self._submit_until_ms = 0
        self._queue_until_ms = 0
        self._last_continue_ms = None
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
        self.adb.tap(x, y)
        self.device.refresh()

    def _back(self) -> None:
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

        probe_at_ms = on_sale_ms - _BUY_PROBE_LEAD_MS
        while now_ms() < max(self.deadline_ms, self._submit_until_ms, self._queue_until_ms):
            if self._stop_requested():
                log.info("收到取消请求，停止抢票")
                return GrabOutcome.CANCELLED
            if self.attempts >= self.task.max_attempts and not (self._submit_until_ms or self._queue_until_ms):
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

        if snap.state == PageState.CAPTCHA:
            self._evidence("captcha")
            self.notify.critical("出现验证码/风控验证，请人工处理（脚本已停止）")
            return GrabOutcome.NEEDS_HUMAN

        if now_ms() < self._sale_at_ms and snap.state != PageState.DETAIL:
            return self._wait_for_button()

        if snap.state == PageState.STOCK_REGISTRATION:
            return self._sold_out("当前为缺货登记页")

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
                    self.notify.critical("购买点击后仍无法识别页面，请在手机上核对")
                    return GrabOutcome.NEEDS_HUMAN
                log.info("购买点击后仍在详情页，重新核对购买按钮")
            self._buy_transition_until_ms = 0

        if snap.state == PageState.SUCCESS:
            self.notify.success(f"[{self.task.name}] 下单成功，请尽快人工支付")
            self._evidence("success")
            return GrabOutcome.SUCCESS

        if snap.state == PageState.RESERVE:
            # 未开票演出的预约登记页：说明开票时间未到或配置有误
            self.notify.critical(
                f"[{self.task.name}] 该演出未开票（当前为预约登记页），"
                "请确认任务开票时间；正式开票后按钮会变为可购买状态")
            return GrabOutcome.NEEDS_HUMAN

        if self.dry_run and snap.state != PageState.DETAIL:
            self.notify.info("演练已停止：页面已离开详情页")
            return GrabOutcome.OFF_PAGE

        if snap.state == PageState.ORDER_RETRY:
            return self._continue_order()
        if self._await_continue_page and snap.state == PageState.UNKNOWN:
            return self._wait_for_order(self._last_continue_ms + _BUY_TRANSITION_MS)
        self._await_continue_page = False
        if snap.state == PageState.QUEUE:
            if not self._queue_until_ms:
                self._queue_until_ms = now_ms() + self.task.queue_wait_seconds * 1000
                log.info("订单已进入排队或提交处理中")
            return self._wait_for_order(self._queue_until_ms)
        if snap.state == PageState.ERROR_DIALOG and any(
                word in snap.matched for word in SOLD_OUT_TEXTS + ["库存不足", "余票不足"]):
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
            return self._from_tier_sheet()
        if snap.state == PageState.ORDER_CONFIRM:
            return self._from_order_confirm()

        # UNKNOWN：可能是票务须知弹窗挡住（首进城市站必弹），先尝试关闭
        from .navigator import dismiss_ticket_notice
        if dismiss_ticket_notice(self.device, self.adb):
            return None
        # 其余：失败弹窗先关掉，再尝试返回详情页
        log.info("页面状态 %s（命中: %s），尝试退出重试", snap.state.value, snap.matched)
        self._dismiss_error_and_back()
        return None

    # ---- 各页面处理 ----------------------------------------------------

    def _sold_out(self, reason: str) -> str:
        log.info("停止抢票：%s", reason)
        self._evidence("sold_out")
        return GrabOutcome.SOLD_OUT

    def _detail_sold_out(self) -> bool:
        if now_ms() < self._sale_at_ms or self._sale_countdown_present():
            return False
        button = self.device.find(SOLD_OUT_TEXTS)
        bar = self.device.find_by_id("purchase_status_bar")
        if not button or not bar:
            return False
        x, y = button.center
        left, top, right, bottom = bar.bounds
        return left <= x <= right and top <= y <= bottom

    def _wait_for_button(self) -> None:
        self._needs_backoff = False
        self._waiting_for_button = True
        until_sale_ms = self._sale_at_ms - now_ms()
        if until_sale_ms > 0:
            time.sleep(min(0.15, until_sale_ms / 1000))
        else:
            sleep_jitter(self.task.poll_interval_ms)

    def _from_detail(self, probe_started_ms: int) -> Optional[str]:
        """详情页：优先识别可购文字，自绘按钮在底部购买栏内用视觉定位。"""
        self._waiting_for_button = True
        wait = (self._sale_countdown_present()
                or self.device.find(self.task.wait_texts + ["立即预约", "已预约"]) is not None)
        if wait and probe_started_ms < self._sale_at_ms <= now_ms():
            # 跨过开票点的读数可能仍是旧倒计时，马上取下一帧。
            return None
        if not wait:
            buy = self.device.find(self.task.buy_texts)
            bbox = buy.bounds if buy and buy.enabled else None
            ui_read_ms = now_ms()
            source = "文字"
            if buy is None:
                if ui_read_ms < self._sale_at_ms:
                    return self._wait_for_button()
                bar = self.device.find_by_id("purchase_status_bar")
                ui_read_ms = now_ms()
                source = "截图"
                try:
                    bbox = self._find_button(self.adb.screencap_bytes())
                except (AdbError, OSError, ValueError) as exc:
                    log.debug("视觉检测异常: %s", exc)
                if bbox and bar:
                    l, t, r, b = bar.bounds
                    x1, y1, x2, y2 = bbox
                    bbox = (max(l, x1), max(t, y1), min(r, x2), min(b, y2))
                    if bbox[2] - bbox[0] < 40 or bbox[3] - bbox[1] < 30:
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
                tap_start_ms = now_ms()
                x, y = vision.tap_button_jitter(self.adb, bbox)
                tap_end_ms = now_ms()
                self.device.refresh()
                self._buy_transition_until_ms = tap_end_ms + _BUY_TRANSITION_MS
                self._unknown_after_buy = False
                log.info("点击已激活购买按钮 (%s,%s)，距开票 %+dms 发出，"
                         "控件核验 %dms，%s定位 %dms，ADB 点击 %dms",
                         x, y, tap_start_ms - self._sale_at_ms, ui_read_ms - probe_started_ms,
                         source, tap_start_ms - ui_read_ms, tap_end_ms - tap_start_ms)
                return None
        return self._wait_for_button()

    def _from_tier_sheet(self) -> Optional[str]:
        """选票页（独立页面，票档价格/确定按钮均为自绘）。

        视觉模型解析票档布局（价格+库存+坐标），控件树「缺货登记」标签
        交叉校正库存；场次未选中时先点匹配场次；票档随机点击后用像素
        检测橙色「确定」按钮随机点击。
        """
        from . import tiers
        parser = self._parse_tiers or tiers.parse_tier_sheet
        try:
            png = self.adb.screencap_bytes()
            layout = parser(png, self.ollama_url, self.vlm_model)
        except Exception as exc:  # noqa: BLE001 —— 解析失败退回重试而非崩溃
            log.warning("选票页视觉解析失败: %s", exc)
            sleep_jitter(self.task.retry_backoff_ms)
            return None
        # 只使用目标日期的票档。切换场次后，旧截图中的票档坐标和库存都可能变化。
        if self.task.session:
            matches = [s for s in layout["sessions"]
                       if session_matches(self.task.session, str(s.get("text", "")))]
            if not any(s.get("selected") for s in matches):
                if len(matches) != 1:
                    self.notify.critical(f"未能唯一确认目标演出日期 {self.task.session}，请人工核对")
                    return GrabOutcome.NEEDS_HUMAN
                target = matches[0]
                x = target["x"] + _rand(30)
                y = target["y"] + _rand(12)
                log.info("选择场次 %s → (%s,%s)", target.get("text"), x, y)
                self._tap(x, y)
                sleep_jitter(self.task.confirm_wait_ms)
                self._needs_backoff = False
                return None

        layout = tiers.reconcile_with_tags(layout, self.device)
        target_price = self.task.price_tiers[0]
        pick = tiers.pick_tier(layout["tiers"], [target_price])
        if not pick:
            visible = [{**tier, "in_stock": True} for tier in layout["tiers"]]
            if tiers.pick_tier(visible, [target_price]):
                return self._sold_out(f"预约票档 ¥{target_price} 缺货")
            self.notify.critical(f"选票页未能识别预约票档 ¥{target_price}，请人工核对")
            return GrabOutcome.NEEDS_HUMAN

        log.info("选中预约票档 %s", pick["price"])

        # 在匹配的票档内点击，再确认。
        x = pick["x"] + _rand(30)
        y = pick["y"] + _rand(12)
        self._tap(x, y)
        sleep_jitter(self.task.confirm_wait_ms)
        # 像素检测橙色「确定」按钮（未选好票档时按钮不亮）
        for _ in range(3):
            if self._stop_requested():
                return GrabOutcome.CANCELLED
            if now_ms() >= self.deadline_ms:
                return GrabOutcome.TIMEOUT
            state = recognize(self.device, self.task).state
            if state != PageState.TIER_SHEET:
                self._needs_backoff = False
                return None
            confirm = self.device.find(["确定"])
            if confirm and confirm.enabled:
                self._tap(*confirm.center)
                self._buy_transition_until_ms = now_ms() + _BUY_TRANSITION_MS
                self._needs_backoff = False
                return None
            try:
                hit = tiers.tap_confirm(self.adb, self.adb.screencap_bytes()) if confirm is None else None
            except Exception as exc:  # noqa: BLE001
                log.debug("确定按钮检测异常: %s", exc)
                hit = None
            if hit:
                log.info("点击确定按钮 @%s", hit)
                self.device.refresh()
                self._buy_transition_until_ms = now_ms() + _BUY_TRANSITION_MS
                self._needs_backoff = False
                return None  # 下一轮识别订单页
            sleep_jitter((150, 350))
        log.info("确定按钮未亮（票档可能未选中），重试本页")
        return None

    def _from_order_confirm(self) -> Optional[str]:
        """确认购买页：核对观演人已选中 → 点「立即提交」→ 观察结果。"""
        # 观演人：要求至少一个 checkbox 选中（预填后默认第一位已选中）；
        # 未选中则点第一位观演人
        try:
            checked = self.device.has_checked_checkbox()
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

        if self._last_continue_ms is not None and now_ms() - self._last_continue_ms < 180:
            return self._wait_for_order(self.deadline_ms)
        if self._stop_requested():
            return GrabOutcome.CANCELLED
        if now_ms() >= self.deadline_ms:
            return GrabOutcome.TIMEOUT

        # 点击返回结果不明时仍视为已提交，直到页面明确给出重试结果。
        self._submit_until_ms = now_ms() + self.task.post_submit_watch_seconds * 1000
        self._tap(*submit.center)
        log.info("点击提交订单 (%s,%s)，等待明确结果", *submit.center)
        self._needs_backoff = False
        return None

    def _continue_order(self) -> Optional[str]:
        if self._last_continue_ms is not None and now_ms() - self._last_continue_ms < _CONTINUE_COOLDOWN_MS:
            return self._wait_for_order(self.deadline_ms)
        button = self.device.find(ORDER_RETRY_TEXTS)
        if not button or not button.enabled:
            return self._wait_for_order(self.deadline_ms)
        if self._stop_requested():
            return GrabOutcome.CANCELLED
        if now_ms() >= self.deadline_ms or self.attempts >= self.task.max_attempts:
            return GrabOutcome.TIMEOUT
        self._last_continue_ms = now_ms()
        self._tap(*button.center)
        self._await_continue_page = True
        self._submit_until_ms = self._queue_until_ms = 0
        self._needs_backoff = False
        log.info("已点继续尝试，立即核对下一页")
        return None

    def _wait_for_order(self, until_ms: int) -> Optional[str]:
        if now_ms() >= until_ms:
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
