"""RunManager：后台扫描到期任务，按设备并发执行（每设备同时最多一个任务）。

- 设备是物理资源：serial 级占用表，冲突任务标记 conflict 并发事件提醒；
- 每次运行产生 runs 记录 + events 时间线（通过 logging 钩子收进 DB）；
- 支持外部取消（stop event），抢票循环/等待/排队各环节都会及时响应。
"""

from __future__ import annotations

import logging
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Set

from ..adb import Adb, find_adb
from ..config import GlobalConfig
from ..device import Device
from ..notify import Notifier
from ..scheduler import TaskRunner
from ..timeutil import LOCAL_TZ, now_ms
from . import db

log = logging.getLogger("crackshot.web.runner")
SCHEDULED_STATUSES = {"scheduled", "waiting_device", "conflict"}

# 当前线程正在执行的 run/task（DBLogHandler 用）
CURRENT_RUN: ContextVar[Optional[int]] = ContextVar("current_run", default=None)
CURRENT_TASK: ContextVar[Optional[int]] = ContextVar("current_task", default=None)


class DBLogHandler(logging.Handler):
    """把 crackshot 日志收进 events 表，作为任务时间线。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            run_id = CURRENT_RUN.get()
            task_id = CURRENT_TASK.get()
            if run_id is None and task_id is None:
                return
            db.add_event(task_id, run_id,
                         "warn" if record.levelno >= logging.WARNING else "info",
                         record.getMessage())
        except Exception:  # noqa: BLE001 —— 日志钩子绝不影响主流程
            pass


class WebNotifier(Notifier):
    """语音/弹窗之外，同步写事件时间线。"""

    def __init__(self, task_id: int, run_id: int):
        super().__init__()
        self.task_id = task_id
        self.run_id = run_id

    def info(self, message: str) -> None:
        db.add_event(self.task_id, self.run_id, "info", message)
        super().info(message)

    def success(self, message: str) -> None:
        db.add_event(self.task_id, self.run_id, "success", message)
        super().success(message)

    def critical(self, message: str) -> None:
        db.add_event(self.task_id, self.run_id, "warn", message)
        super().critical(message)


class RunManager:
    def __init__(self, db_path: Path):
        self.evidence_root = db_path.parent / "evidence"
        self.recording_root = db_path.parent / "recordings" / db_path.name
        self._lock = threading.RLock()
        self.device_busy: Dict[str, int] = {}     # serial → run_id
        self.run_task: Dict[int, int] = {}        # run_id → task_id
        self.task_running: Set[int] = set()
        self.cancel_events: Dict[int, threading.Event] = {}  # run_id → event
        self._online_cache: Dict[str, str] = {}   # serial → model
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._housekeeping, name="run-manager", daemon=True)
        self._thread.start()
        log.info("RunManager 已启动")

    def shutdown(self) -> None:
        self._stop.set()

    # ---- 对外查询 ------------------------------------------------------

    def scan_devices(self) -> Dict[str, str]:
        """在线设备 serial → model（失败时用上次缓存）。"""
        adb_path = find_adb(None)
        if not adb_path:
            return self._online_cache
        try:
            found = {}
            for d in Adb.list_devices(adb_path):
                if d["state"] == "device":
                    found[d["id"]] = d["model"] or ""
            self._online_cache = found
        except Exception as exc:  # noqa: BLE001
            log.warning("扫描设备失败: %s", exc)
        return self._online_cache

    def running_view(self) -> list:
        out = []
        with self._lock:
            for serial, run_id in self.device_busy.items():
                out.append({"serial": serial, "run_id": run_id})
        return out

    def is_task_running(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self.task_running

    def recording_path(self, run_id: int) -> Path:
        return self.recording_root / f"run-{run_id}.mp4"

    @contextmanager
    def edit_task(self, task_id: int):
        with self._lock:
            task = db.get_task(task_id)
            if task_id in self.task_running:
                raise ValueError("任务运行中，不能修改")
            yield task

    # ---- 调度 ----------------------------------------------------------

    def _housekeeping(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("调度循环异常: %s", exc)

    def _tick(self) -> None:
        online = self.scan_devices()
        settings = db.get_settings()
        for item in sorted(db.list_tasks(), key=lambda t: t["on_sale_at"]):
            with self._lock:
                task = db.get_task(item["id"])
                if (not task or not task["enabled"] or task["status"] not in SCHEDULED_STATUSES
                        or task["id"] in self.task_running):
                    continue
                try:
                    _, due_ms, expire_ms = schedule_times(task, settings)
                except ValueError as exc:
                    self._mark_once(task, "failed", f"排程无效: {exc}")
                    continue
                now = now_ms()
                if now >= expire_ms:
                    self._mark_once(task, "failed", "已错过开票窗口（任务过期，未执行）")
                    continue
                if now < due_ms:
                    continue
                device = db.get_device(task["device_id"])
                if not device or not device["enabled"]:
                    self._mark_once(task, "waiting_device", "任务未绑定可用设备")
                    continue
                if device["serial"] in self.device_busy:
                    self._mark_once(task, "conflict",
                                    "设备正被其他任务占用，将在开票窗口内重试")
                    continue
                if device["serial"] not in online:
                    self._mark_once(task, "waiting_device", "设备不在线，请在开票前连接")
                    continue
                self.spawn(task["id"], manual=False, dry_run=bool(task["scheduled_dry_run"]))

    def _mark_once(self, task: dict, status: str, message: str) -> None:
        """状态没变时只更新一次，避免每 2 秒刷一条。"""
        if task["status"] != status:
            db.set_task_status(task["id"], status)
            db.add_event(task["id"], None, "warn", message)

    # ---- 执行 ----------------------------------------------------------

    def spawn(self, task_id: int, manual: bool = True, dry_run: bool = False) -> int:
        """启动一个运行线程；manual=True 表示人工点“立即运行”（跳过定时直接执行）。"""
        with self._lock:
            task, device = self._check_available(task_id)
            if not task["enabled"]:
                raise ValueError("任务已停用")
            if manual and not dry_run and (task["status"] not in SCHEDULED_STATUSES
                                           or task["scheduled_dry_run"]):
                raise ValueError("请先确认正式排程")
            run_id = db.create_run(task_id, dry_run=dry_run)
            self.device_busy[device["serial"]] = run_id
            self.run_task[run_id] = task_id
            self.task_running.add(task["id"])
            cancel = threading.Event()
            self.cancel_events[run_id] = cancel
        thread = threading.Thread(
            target=self._worker, name=f"run-{run_id}",
            args=(run_id, task, device, dry_run, manual, cancel),
            daemon=True,
        )
        db.add_event(task_id, run_id, "info",
                     f"运行 #{run_id} 启动（{'手动' if manual else '调度'}，"
                     f"{'演练 dry-run' if dry_run else '正式'}）")
        thread.start()
        return run_id

    def _check_available(self, task_id: int):
        task = db.get_task(task_id)
        if not task:
            raise ValueError(f"任务不存在: {task_id}")
        if task_id in self.task_running:
            raise ValueError("任务已在运行")
        device = db.get_device(task["device_id"])
        if not device or not device["enabled"]:
            raise ValueError("请绑定已启用的设备")
        if device["serial"] in self.device_busy:
            raise ValueError(f"设备正在执行运行 #{self.device_busy[device['serial']]}")
        return task, device

    def calibrate_task(self, task_id: int) -> int:
        """读取详情页的开售时间，保存为待确认的校准结果。"""
        with self._lock:
            task, device = self._check_available(task_id)
            serial = device["serial"]
            run_id = db.create_run(task_id, dry_run=True)
            self.device_busy[serial] = run_id
            self.run_task[run_id] = task_id
            self.task_running.add(task_id)
        threading.Thread(target=self._calibrate_worker,
                         name=f"calib-{run_id}", daemon=True,
                         args=(run_id, task, serial)).start()
        return run_id

    def _calibrate_worker(self, run_id: int, task: dict, serial: str) -> None:
        from ..navigator import navigate_to_detail
        from ..scheduler import read_page_sell_time
        run_token = CURRENT_RUN.set(run_id)
        task_token = CURRENT_TASK.set(task["id"])
        db.add_event(task["id"], run_id, "info", "开始校准：导航到演出详情页")
        outcome = "error"
        try:
            adb_path = find_adb(None)
            if not adb_path:
                raise RuntimeError("未找到 adb")
            adb = Adb(adb_path, serial)
            adb.wake()
            dev = Device(serial)
            task_cfg = db.task_config_from_row(task)
            if not navigate_to_detail(adb, dev, task_cfg):
                raise RuntimeError("导航到详情页失败，请检查演出关键词/城市")
            page_time = read_page_sell_time(dev, task_cfg.city or None)
            if page_time is None:
                raise RuntimeError("详情页未读到开售时间（可能已开票或无倒计时）")
            calibrated_at = format_time(page_time)
            fields = {"calibrated_at": calibrated_at}
            if task["status"] not in SCHEDULED_STATUSES:
                fields["status"] = "ready"
            db.update_task(task["id"], fields)
            db.add_event(task["id"], run_id, "success",
                         f"校准完成，页面开售时间：{calibrated_at}（等待人工确认）")
            outcome = "calibrated"
        except Exception as exc:  # noqa: BLE001
            db.add_event(task["id"], run_id, "error", f"校准失败: {exc}")
        finally:
            db.finish_run(run_id, outcome, 0)
            CURRENT_RUN.reset(run_token)
            CURRENT_TASK.reset(task_token)
            with self._lock:
                self.device_busy.pop(serial, None)
                self.run_task.pop(run_id, None)
                self.task_running.discard(task["id"])

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            event = self.cancel_events.get(run_id)
        if not event:
            return False
        event.set()
        return True

    def _worker(self, run_id: int, task: dict, device: dict,
                dry_run: bool, manual: bool, cancel: threading.Event) -> None:
        run_token = CURRENT_RUN.set(run_id)
        task_token = CURRENT_TASK.set(task["id"])
        outcome = "error"
        attempts = 0
        try:
            settings = db.get_settings()
            adb_path = find_adb(None)
            if not adb_path:
                raise RuntimeError("电脑上未找到 adb")
            adb = Adb(adb_path, device["serial"])
            dev = Device(device["serial"])
            task_cfg = db.task_config_from_row(task, settings)
            global_cfg = GlobalConfig(clock_offset_ms=int(settings.get("clock_offset_ms", "0")))
            notifier = WebNotifier(task["id"], run_id)

            evidence_dir = self.evidence_root / f"{task['name']}-run{run_id}"
            db.set_task_status(task["id"], "prewarming")

            def phase(name: str) -> None:
                if name == "grabbing":
                    db.set_task_status(task["id"], "grabbing")
                    db.add_event(task["id"], run_id, "info", "进入抢票阶段")

            runner = TaskRunner(global_cfg, task_cfg, adb, dev, notifier,
                                evidence_dir, dry_run=dry_run,
                                stop_check=cancel.is_set, phase_cb=phase,
                                ollama_url=settings.get("ollama_url", "http://127.0.0.1:11434"),
                                vlm_model=settings.get("vlm_model") or "qwen3-vl:4b-instruct",
                                guard_only=dry_run and not manual,
                                recording_path=self.recording_path(run_id))
            outcome = runner.run(ignore_schedule=manual)
            attempts = runner.grabber.attempts
        except Exception as exc:  # noqa: BLE001
            log.error("运行 #%s 异常: %s", run_id, exc)
            db.add_event(task["id"], run_id, "error", f"异常: {exc}\n{traceback.format_exc()[-400:]}")
            outcome = "error"
        finally:
            db.finish_run(run_id, outcome, attempts)
            final_status = {"success": "success", "needs_human": "needs_human"}.get(
                outcome, outcome if outcome in ("cancelled",) else "failed")
            if dry_run:
                final_status = task["status"] if manual else (
                    "ready" if task["calibrated_at"] else "draft")
            db.set_task_status(task["id"], final_status)
            db.add_event(task["id"], run_id, "info", f"运行 #{run_id} 结束: {outcome}")
            CURRENT_RUN.reset(run_token)
            CURRENT_TASK.reset(task_token)
            with self._lock:
                self.device_busy.pop(device["serial"], None)
                self.run_task.pop(run_id, None)
                self.task_running.discard(task["id"])
                self.cancel_events.pop(run_id, None)


def schedule_times(task: dict, settings: Dict[str, str]):
    """开售、预热和窗口结束；与执行器使用相同的配置和时钟偏移。"""
    cfg = db.task_config_from_row(task, settings)
    sale = cfg.on_sale_ms() + int(settings.get("clock_offset_ms", "0"))
    return sale, sale - cfg.prewarm_minutes * 60_000, sale + cfg.max_retry_seconds * 1000


def format_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
