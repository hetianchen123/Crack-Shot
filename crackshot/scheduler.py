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

_RECORDING_TAIL_SECONDS = 60


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
        self.recording_path = recording_path
        self.grabber = Grabber(adb, device, task, notifier, evidence_root,
                               dry_run, stop_check=stop_check)

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
        limit_seconds = (until_sale + self.task.max_retry_seconds + self.task.queue_wait_seconds
                         + self.task.post_submit_watch_seconds + 180 + _RECORDING_TAIL_SECONDS)
        recording = (record_screen(self.adb, self.recording_path, limit_seconds)
                     if self.recording_path and not self.dry_run else nullcontext())
        with recording as process:
            outcome = self._run_active(on_sale_ms, ignore_schedule)
            if outcome != GrabOutcome.CANCELLED and process is not None:
                def tail_should_stop():
                    return process.poll() is not None or (self.stop_check and self.stop_check())

                if not tail_should_stop():
                    log.info("自动操作已结束（%s），继续录屏 %d 秒", outcome, _RECORDING_TAIL_SECONDS)
                    sleep_until(now_ms() + _RECORDING_TAIL_SECONDS * 1000,
                                stop_check=tail_should_stop)
        return outcome

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
    属于目标城市的文案或单站主倒计时；歧义时返回 None。
    """
    import re
    from .navigator import _inside, _is_tour, _snapshot_retry

    for _ in range(2):
        _snapshot_retry(device)
        if recognize(device, _GUARD_TASK).state != PageState.DETAIL:
            return None
        now = datetime.fromtimestamp(now_ms() / 1000, LOCAL_TZ)
        nodes = device.elements()
        tour = _is_tour(device)
        labels = [n for n in nodes if "开抢" in (n.text or n.description)
                  or "开售" in (n.text or n.description)
                  or "count_sell_time" in n.resource_id]
        target_times, detail_times = set(), set()
        for node in labels:
            text = node.text or node.description
            # 城市卡片里的倒计时有时与城市名分别放在两个子节点中。
            parent = node
            while parent and "city_card" not in parent.resource_id:
                parent = parent.parent
            city_label = re.match(r"^\s*([^\s\d，。]+?)(?:站|市)(?=\s|\d|今天|今日|明天|明日)", text)
            scopes = {city_label[1]} if city_label else set()
            if parent:
                scopes.update((item.text or item.description).strip().rstrip("站市")
                              for item in nodes if "tour_city_name" in item.resource_id
                              and _inside(item, parent) and (item.text or item.description).strip())
            if len(scopes) > 1 or (city and scopes and city.rstrip("站市") not in scopes):
                continue
            if (tour or parent) and not scopes:
                continue
            clock_pattern = r"(\d{1,2}):(\d{2})(?::(\d{2}))?"
            hh_mm = re.search(clock_pattern + r"\s*开[抢售]", text)
            if not hh_mm:
                clocks = list(re.finditer(clock_pattern, text))
                hh_mm = clocks[0] if len(clocks) == 1 else None
            if not hh_mm:
                continue
            hh, mi, ss = (int(part or 0) for part in hh_mm.groups())
            prefix = text[:hh_mm.start()]
            dates = list(re.finditer(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|今天|今日|明天|明日", prefix))
            if not dates:
                continue
            date = dates[-1]
            try:
                if date[2]:
                    year, mon, day = date.groups()
                    candidate = datetime(int(year or now.year), int(mon), int(day),
                                         hh, mi, ss, tzinfo=LOCAL_TZ)
                    if not year and candidate < now - timedelta(days=90):
                        candidate = candidate.replace(year=now.year + 1)
                else:
                    base = now + timedelta(days=int(date[0] in ("明天", "明日")))
                    candidate = base.replace(hour=hh, minute=mi, second=ss, microsecond=0)
                (target_times if scopes and city else detail_times).add(epoch_ms(candidate))
            except ValueError:
                continue
        candidates = target_times or detail_times
        if len(candidates) == 1:
            return candidates.pop()
        if len(candidates) > 1:
            log.warning("目标开售时间存在多个候选，无法校准")
            return None
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
