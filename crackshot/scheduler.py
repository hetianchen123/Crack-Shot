"""任务调度：等到预热时间 → 校验页面 → 开票前进入紧轮询 → 执行抢票。"""

from __future__ import annotations

import logging
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .adb import Adb
from .config import GlobalConfig, TaskConfig

from .device import Device
from .grabber import GrabOutcome, Grabber
from .notify import Notifier
from .pages import PageState, recognize
from .recording import record_screen
from .timeutil import LOCAL_TZ, epoch_ms, format_remaining, now_ms

log = logging.getLogger("crackshot.scheduler")


def sleep_until(target_ms: int, label: str = "", stop_check=None) -> bool:
    """分片睡眠到目标时刻，过程打印倒计时；被要求停止时返回 True。"""
    announced = -1
    while True:
        if stop_check and stop_check():
            return True
        left = target_ms - now_ms()
        if left <= 0:
            return False
        seconds_left = left // 1000
        # 距离越长播报越稀疏：>10 分钟每 5 分钟一次，>1 分钟每分钟，最后 1 分钟每 10 秒
        if seconds_left > 600:
            interval, step = 60.0, 300
        elif seconds_left > 60:
            interval, step = 30.0, 60
        else:
            interval, step = 10.0, 10
        if label and seconds_left // step != announced:
            announced = seconds_left // step
            log.info("距%s还有 %s", label, format_remaining(left))
        time.sleep(min(interval, left / 1000.0, 5.0))


class TaskRunner:
    def __init__(
        self,
        cfg: GlobalConfig,
        task: TaskConfig,
        adb: Adb,
        device: Device,
        notifier: Notifier,
        evidence_root: Path,
        dry_run: bool = False,
        stop_check=None,
        phase_cb=None,
        ollama_url: str = "http://127.0.0.1:11434",
        vlm_model: str = "qwen3-vl:4b-instruct",
        guard_only: bool = False,
        recording_path: Optional[Path] = None,
    ):
        self.cfg = cfg
        self.task = task
        self.adb = adb
        self.device = device
        self.notifier = notifier
        self.dry_run = dry_run
        self.stop_check = stop_check
        self.phase_cb = phase_cb
        self.ollama_url = ollama_url
        self.vlm_model = vlm_model
        self.guard_only = guard_only
        self.recording_path = recording_path
        self.grabber = Grabber(adb, device, task, notifier, evidence_root,
                               dry_run, stop_check=stop_check,
                               ollama_url=ollama_url, vlm_model=vlm_model)

    def _phase(self, name: str) -> None:
        if self.phase_cb:
            try:
                self.phase_cb(name)
            except Exception:  # noqa: BLE001 —— 状态回调失败不影响抢票
                log.debug("phase_cb 异常", exc_info=True)

    def run(self, ignore_schedule: bool = False) -> str:
        on_sale_ms = self.task.on_sale_ms() + self.cfg.clock_offset_ms
        if not ignore_schedule:
            prewarm_ms = on_sale_ms - self.task.prewarm_minutes * 60_000
            if sleep_until(prewarm_ms, f"[{self.task.name}] 预热", self.stop_check):
                return GrabOutcome.CANCELLED
            if now_ms() >= on_sale_ms + self.task.max_retry_seconds * 1000:
                return GrabOutcome.TIMEOUT
        if self.stop_check and self.stop_check():
            return GrabOutcome.CANCELLED

        until_sale = 0 if ignore_schedule else max(0, on_sale_ms - now_ms()) // 1000
        limit_seconds = until_sale + self.task.max_retry_seconds + self.task.queue_wait_seconds + 180
        recording = (record_screen(self.adb, self.recording_path, limit_seconds)
                     if self.recording_path and not self.dry_run else nullcontext())
        with recording:
            return self._run_active(on_sale_ms, ignore_schedule)

    def _run_active(self, on_sale_ms: int, ignore_schedule: bool) -> str:
        if ignore_schedule:
            log.info("手动模式：跳过定时")
            self.adb.wake()
            on_page = self._open_damai()
            if self.stop_check and self.stop_check():
                return GrabOutcome.CANCELLED
            if not on_page:
                return GrabOutcome.NEEDS_HUMAN
            self._phase("grabbing")
            return self.grabber.run(now_ms(), verify_detail=True)

        stayon_old = None
        try:
            try:
                stayon_old = self.adb.shell(
                    "settings", "get", "global", "stay_on_while_plugged_in").strip()
                self.adb.shell("svc", "power", "stayon", "usb")
                self.adb.wake()
            except Exception as exc:  # noqa: BLE001
                log.warning("点亮屏幕或设置常亮失败: %s", exc)
            return self._timed_run(on_sale_ms)
        finally:
            if stayon_old is not None:
                try:
                    if stayon_old in ("null", ""):
                        self.adb.shell("settings", "delete", "global",
                                       "stay_on_while_plugged_in")
                    else:
                        self.adb.shell("settings", "put", "global",
                                       "stay_on_while_plugged_in", stayon_old)
                except Exception as exc:  # noqa: BLE001
                    log.warning("恢复设备常亮设置失败: %s", exc)

    def _timed_run(self, on_sale_ms: int) -> str:
        arm_ms = on_sale_ms - self.task.arm_lead_seconds * 1000
        # 至少尝试一次导航：服务晚启动或设备迟上线时仍可在窗口内接手。
        on_page = False
        first_check = True
        navigation_failures = 0
        log.info("进入目标详情页守护")
        while first_check or now_ms() < arm_ms:
            if self.stop_check and self.stop_check():
                return GrabOutcome.CANCELLED
            if now_ms() >= on_sale_ms + self.task.max_retry_seconds * 1000:
                return GrabOutcome.TIMEOUT
            first_check = False
            was_on_page = on_page
            on_page = self._page_guard() or self._open_damai()
            if self.stop_check and self.stop_check():
                return GrabOutcome.CANCELLED
            navigation_failures = 0 if on_page else navigation_failures + 1
            if navigation_failures >= 2:
                self.notifier.critical("连续两次未能确认目标详情页，已停止自动导航，请人工核对")
                return GrabOutcome.NEEDS_HUMAN
            if on_page and not was_on_page:
                log.info("已确认目标演出详情页，守护中")
            if sleep_until(min(now_ms() + (2500 if on_page else 8000), arm_ms),
                           "", self.stop_check):
                return GrabOutcome.CANCELLED

        if not on_page:
            self.notifier.critical("到达开票准备时刻，仍未确认目标详情页")
            return GrabOutcome.NEEDS_HUMAN
        if self.guard_only:
            if sleep_until(on_sale_ms, "演练触发", self.stop_check):
                return GrabOutcome.CANCELLED
            if not self._page_guard():
                return GrabOutcome.NEEDS_HUMAN
            log.info("定时演练通过：到达开抢时刻，已确认目标详情页（无购买点击）")
            return "guard_verified"
        log.info("到达紧轮询时点，交给抢票状态机；开票前禁止点击")
        self._phase("grabbing")
        return self.grabber.run(on_sale_ms, verify_detail=True)

    def _page_guard(self) -> bool:
        """当前是否在目标单站详情页。"""
        try:
            from .navigator import is_target_detail
            self.device.refresh()
            return self.adb.is_damai_foreground() and is_target_detail(self.device, self.task)
        except Exception:  # noqa: BLE001
            return False

    def _open_damai(self) -> bool:
        from .navigator import navigate_to_detail
        return navigate_to_detail(self.adb, self.device, self.task,
                                  self.notifier, stop_check=self.stop_check)


def _make_guard_task() -> "TaskConfig":
    return TaskConfig(name="guard", show_name="Guard", on_sale_at="2030-01-01 00:00",
                      price_tiers=["0"])


_GUARD_TASK = _make_guard_task()


def read_page_sell_time(device, city: Optional[str] = None) -> Optional[int]:
    """读详情页开抢倒计时，返回开售时刻（本地毫秒时间戳）。

    支持格式：「X月X日 XX:XX开抢」「今天/明天 XX:XX开抢」（真机实测
    多城巡演城市卡片显示"宁波站 今天12:00开抢"）。多城巡演时优先取
    包含目标城市名的节点；读不到返回 None。
    """
    import re
    from .navigator import _snapshot_retry

    now = datetime.fromtimestamp(now_ms() / 1000, LOCAL_TZ)
    for _ in range(2):
        _snapshot_retry(device)
        if recognize(device, _GUARD_TASK).state != PageState.DETAIL:
            return None
        nodes = device.elements()
        labels = [n for n in nodes if "开抢" in (n.text or n.description)
                  or "开售" in (n.text or n.description)
                  or "count_sell_time" in n.resource_id]
        labels.sort(key=lambda n: (not city or city not in (n.text or n.description),
                                   "count_sell_time" not in n.resource_id))
        for node in labels:
            text = node.text or node.description
            hh_mm = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
            if not hh_mm:
                continue
            hh, mi, ss = (int(part or 0) for part in hh_mm.groups())
            md = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text)
            try:
                if md:
                    year, mon, day = md.groups()
                    candidate = datetime(int(year or now.year), int(mon), int(day),
                                         hh, mi, ss, tzinfo=LOCAL_TZ)
                    if not year and candidate < now - timedelta(days=90):
                        candidate = candidate.replace(year=now.year + 1)
                elif any(word in text for word in ("今天", "今日", "明天", "明日")):
                    base = now + timedelta(days=int("明天" in text or "明日" in text))
                    candidate = base.replace(hour=hh, minute=mi, second=ss, microsecond=0)
                else:
                    continue
                return epoch_ms(candidate)
            except ValueError:
                continue
        time.sleep(0.5)
    return None


def pick_task(cfg: GlobalConfig, name: Optional[str]) -> TaskConfig:
    if name:
        for task in cfg.tasks:
            if task.name == name:
                return task
        raise SystemExit(f"找不到任务: {name}，可用: {[t.name for t in cfg.tasks]}")
    if len(cfg.tasks) == 1:
        return cfg.tasks[0]
    # 未指定且多个任务：选最近开票的
    return min(cfg.tasks, key=lambda t: t.on_sale_ms())
