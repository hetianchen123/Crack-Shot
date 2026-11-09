"""FastAPI 应用：账号/设备/任务/运行管理 + 截图提取。

默认只监听 127.0.0.1，本机使用，不做跨域和鉴权。
"""

import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 注意：本文件不能用 `from __future__ import annotations`——
# Pydantic 模型定义在 create_app 内部，字符串注解无法解析到局部类，
# FastAPI 会把 body 参数误当成 query 参数。

from ..config import TaskConfig
from ..timeutil import now_ms, parse_local
from . import db, extractor
from .runner import DBLogHandler, RunManager, SCHEDULED_STATUSES, format_time, schedule_times

log = logging.getLogger("crackshot.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_path: Path) -> FastAPI:
    db.init(db_path)
    manager = RunManager(db_path)

    app = FastAPI(title="Crack-shot 后台", docs_url="/api/docs")
    app.state.run_manager = manager

    @app.on_event("startup")
    def _startup() -> None:
        root = logging.getLogger("crackshot")
        if not any(isinstance(h, DBLogHandler) for h in root.handlers):
            root.addHandler(DBLogHandler())
        manager.start()
        log.info("Web 后台已启动")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        manager.shutdown()

    # ---- 概览 ----------------------------------------------------------

    @app.get("/api/overview")
    def overview() -> Dict[str, Any]:
        tasks = db.list_tasks()
        online = manager.scan_devices()
        busy = {item["serial"]: item["run_id"] for item in manager.running_view()}
        devices = []
        for d in db.list_devices():
            run_id = busy.get(d["serial"])
            busy_task_id = manager.run_task.get(run_id) if run_id else None
            devices.append({
                **d,
                "online": d["serial"] in online,
                "busy_run_id": run_id,
                "busy_task": next((t["name"] for t in tasks if t["id"] == busy_task_id), None),
            })
        unregistered = [
            {"serial": s, "model": m, "registered": False}
            for s, m in online.items() if s not in {d["serial"] for d in devices}
        ]
        settings = db.get_settings()
        now_next = sorted(
            (t for t in tasks if t["enabled"] and t["status"] in SCHEDULED_STATUSES),
            key=lambda t: t["on_sale_at"])
        return {
            "tasks": tasks,
            "devices": devices,
            "unregistered_devices": unregistered,
            "runs": db.list_runs(limit=15),
            "next_up": now_next[:5],
            "running": [
                {"run_id": rid, "serial": s} for s, rid in busy.items()],
            "settings": settings,
            "extractor": extractor.status(settings["ollama_url"], settings["vlm_model"]),
        }

    # ---- 账号 ----------------------------------------------------------

    class AccountIn(BaseModel):
        label: str
        note: str = ""

    @app.get("/api/accounts")
    def accounts() -> List[Dict[str, Any]]:
        return db.list_accounts()

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountIn) -> Dict[str, Any]:
        if not body.label.strip():
            raise HTTPException(400, "账号名称不能为空")
        try:
            account_id = db.create_account(body.label, body.note)
        except Exception as exc:  # noqa: BLE001 —— UNIQUE 冲突等
            raise HTTPException(400, f"创建失败: {exc}") from exc
        return {"id": account_id}

    @app.patch("/api/accounts/{account_id}")
    def update_account(account_id: int, body: AccountIn) -> Dict[str, str]:
        db.update_account(account_id, label=body.label, note=body.note)
        return {"ok": "1"}

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: int) -> Dict[str, str]:
        db.delete_account(account_id)
        return {"ok": "1"}

    # ---- 设备 ----------------------------------------------------------

    class DeviceIn(BaseModel):
        serial: str
        model: str = ""
        account_id: Optional[int] = None
        note: str = ""

    class DevicePatch(BaseModel):
        account_id: Optional[int] = None
        enabled: Optional[bool] = None
        note: Optional[str] = None

    @app.get("/api/devices")
    def devices() -> Dict[str, Any]:
        online = manager.scan_devices()
        registered = db.list_devices()
        return {
            "devices": [{**d, "online": d["serial"] in online} for d in registered],
            "online": [{"serial": s, "model": m} for s, m in online.items()],
        }

    @app.post("/api/devices", status_code=201)
    def register_device(body: DeviceIn) -> Dict[str, Any]:
        if not body.serial.strip():
            raise HTTPException(400, "serial 不能为空")
        device_id = db.upsert_device(body.serial.strip(), body.model, body.account_id, body.note)
        return {"id": device_id}

    @app.patch("/api/devices/{device_id}")
    def patch_device(device_id: int, body: DevicePatch) -> Dict[str, str]:
        db.update_device(device_id, account_id=body.account_id,
                         enabled=body.enabled, note=body.note)
        return {"ok": "1"}

    @app.delete("/api/devices/{device_id}")
    def delete_device(device_id: int) -> Dict[str, str]:
        db.delete_device(device_id)
        return {"ok": "1"}

    # ---- 任务 ----------------------------------------------------------

    class TaskIn(BaseModel):
        name: str
        show_name: str
        on_sale_at: str = ""
        price_tiers: List[str]
        device_id: Optional[int] = None
        options: Dict[str, Any] = {}
        note: str = ""
        enabled: Optional[bool] = None
        share_url: str = ""
        city: str = ""
        session: str = ""

    @app.get("/api/tasks")
    def tasks() -> List[Dict[str, Any]]:
        settings = db.get_settings()
        return [_with_schedule(task, settings) for task in db.list_tasks()]

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskIn) -> Dict[str, Any]:
        _validate_task(body)
        task_id = db.create_task(body.name, body.show_name, body.on_sale_at,
                                 body.price_tiers, body.device_id, body.options,
                                 body.note, body.share_url, body.city, body.session,
                                 enabled=body.enabled is not False)
        db.add_event(task_id, None, "info", f"任务创建: {body.name}")
        return {"id": task_id}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> Dict[str, Any]:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return _with_schedule(task, db.get_settings())

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: int, body: TaskIn) -> Dict[str, Any]:
        fields = {
            "name": body.name.strip(), "show_name": body.show_name.strip(),
            "on_sale_at": body.on_sale_at.strip(),
            "price_tiers": body.price_tiers, "options": body.options,
            "device_id": body.device_id, "note": body.note,
            "share_url": body.share_url, "city": body.city.strip(), "session": body.session.strip(),
        }
        try:
            with manager.edit_task(task_id) as existing:
                if not existing:
                    raise HTTPException(404, "任务不存在")
                _validate_task(body, existing)
                if any(fields[k] != existing[k] for k in
                       ("show_name", "city", "session", "device_id", "price_tiers")):
                    fields.update(status="draft", calibrated_at="", scheduled_dry_run=0, calibration={})
                elif any(fields[k] != existing[k] for k in ("on_sale_at", "options")):
                    fields.update(status="ready" if existing["calibrated_at"] else "draft",
                                  scheduled_dry_run=0)
                if body.enabled is not None:
                    fields["enabled"] = body.enabled
                db.update_task(task_id, fields)
                db.add_event(task_id, None, "info", "任务资料已更新")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": "1"}

    class ScheduleIn(BaseModel):
        on_sale_at: str
        dry_run: bool = False

    @app.post("/api/tasks/{task_id}/schedule")
    def schedule_task(task_id: int, body: ScheduleIn) -> Dict[str, Any]:
        try:
            with manager.edit_task(task_id) as task:
                if not task:
                    raise HTTPException(404, "任务不存在")
                device = db.get_device(task["device_id"])
                if not task["enabled"] or not device or not device["enabled"]:
                    raise ValueError("请先启用任务并绑定可用设备")
                if not body.dry_run and not task["calibrated_at"]:
                    raise ValueError("请先校准页面开售时间，再确认正式排程")
                if not body.dry_run and not task["session"]:
                    raise ValueError("请先填写目标演出日期")
                if not body.dry_run and len(task["price_tiers"]) != 1:
                    raise ValueError("请校准读取预约页，或手动确认唯一预约票档")
                if not body.dry_run and task["calibration"].get("mismatch"):
                    raise ValueError("演出日期或票档与详情页不符，请核对任务资料后重新校准")
                if body.dry_run and task["status"] in SCHEDULED_STATUSES and not task["scheduled_dry_run"]:
                    raise ValueError("该任务已有正式排程，请另建草稿进行定时演练")
                fields = {"on_sale_at": body.on_sale_at.strip(), "status": "scheduled",
                          "scheduled_dry_run": int(body.dry_run)}
                parse_local(fields["on_sale_at"])
                settings = db.get_settings()
                sale_ms, due_ms, _ = schedule_times({**task, **fields}, settings)
                if sale_ms <= now_ms():
                    raise ValueError("开售或演练时刻必须在未来")
                if body.dry_run:
                    # 演练时间独立于页面校准值；结束后可用原校准值确认正式排程。
                    label = "定时演练"
                else:
                    label = "正式排程"
                db.update_task(task_id, fields)
                db.add_event(task_id, None, "info",
                             f"{label}已建立：{fields['on_sale_at']}，预热 {format_time(due_ms)}")
                return _with_schedule({**task, **fields}, settings)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int) -> Dict[str, str]:
        try:
            with manager.edit_task(task_id):
                db.delete_task(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": "1"}

    class RunIn(BaseModel):
        dry_run: bool = True

    @app.post("/api/tasks/{task_id}/run")
    def run_task(task_id: int, body: RunIn) -> Dict[str, Any]:
        try:
            run_id = manager.spawn(task_id, manual=True, dry_run=body.dry_run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id}

    @app.post("/api/tasks/{task_id}/calibrate")
    def calibrate_task(task_id: int) -> Dict[str, Any]:
        try:
            run_id = manager.calibrate_task(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "message": "校准已开始（导航到详情页读取开售时间）"}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: int) -> Dict[str, Any]:
        for run_id, tid in list(manager.run_task.items()):
            if tid == task_id:
                if not manager.cancel(run_id):
                    raise HTTPException(409, "该运行无法停止或已结束")
                return {"ok": "1", "run_id": run_id}
        raise HTTPException(404, "任务当前没有运行中的实例")

    # ---- 运行记录 -------------------------------------------------------

    @app.get("/api/runs")
    def runs(task_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        return db.list_runs(limit=min(limit, 500), task_id=task_id)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int) -> Dict[str, Any]:
        run = rowdict_run(run_id)
        if not run:
            raise HTTPException(404, "运行不存在")
        recording_url = None
        if run["finished_at"] and not run["dry_run"] and manager.recording_path(run_id).is_file():
            recording_url = f"/api/runs/{run_id}/recording"
        return {"run": run, "events": db.list_events(run_id=run_id, limit=300),
                "recording_url": recording_url}

    @app.get("/api/runs/{run_id}/recording", response_class=FileResponse)
    def run_recording(run_id: int) -> FileResponse:
        run = rowdict_run(run_id)
        path = manager.recording_path(run_id)
        if not run or run["dry_run"] or not run["finished_at"] or not path.is_file():
            raise HTTPException(404, "该运行没有可回放的录屏")
        return FileResponse(path, media_type="video/mp4", filename=path.name,
                            content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: int) -> List[Dict[str, Any]]:
        return db.list_events(task_id=task_id, limit=300)

    # ---- 截图提取 -------------------------------------------------------

    @app.post("/api/extract")
    async def extract(file: UploadFile = File(...)) -> Dict[str, Any]:
        settings = db.get_settings()
        data = await file.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > 12 * 1024 * 1024:
            raise HTTPException(400, "图片超过 12MB")
        try:
            draft = extractor.extract(data, settings["ollama_url"], settings["vlm_model"])
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return draft

    # ---- 设置 -----------------------------------------------------------

    class SettingsIn(BaseModel):
        clock_offset_ms: Optional[int] = None
        ollama_url: Optional[str] = None
        vlm_model: Optional[str] = None
        default_prewarm_minutes: Optional[int] = None

    @app.put("/api/settings")
    def put_settings(body: SettingsIn) -> Dict[str, Any]:
        if body.default_prewarm_minutes is not None and not 1 <= body.default_prewarm_minutes <= 60:
            raise HTTPException(400, "预热时间必须为 1 至 60 分钟")
        mapping = {
            "clock_offset_ms": body.clock_offset_ms,
            "ollama_url": body.ollama_url,
            "vlm_model": body.vlm_model,
            "default_prewarm_minutes": body.default_prewarm_minutes,
        }
        for key, value in mapping.items():
            if value is not None:
                db.set_setting(key, str(value))
        return db.get_settings()

    class NoCacheStaticFiles(StaticFiles):
        """本地工具：静态资源一律 no-cache，改动刷新即生效，避免 webview 强缓存旧 JS。"""

        def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    if STATIC_DIR.exists():
        app.mount("/", NoCacheStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


def rowdict_run(run_id: int) -> Optional[Dict[str, Any]]:
    row = db.query_one(
        "SELECT r.*, t.name AS task_name FROM runs r JOIN tasks t ON t.id=r.task_id"
        " WHERE r.id=?", (run_id,))
    return db.rowdict(row)


def _validate_task(body: "TaskIn", existing=None) -> None:
    if not (body.name.strip() and body.show_name.strip() and body.price_tiers):
        raise HTTPException(400, "名称、演出关键词、票档优先级都不能为空")
    try:
        session = body.session.strip()
        if not existing or session != existing["session"]:
            day, _, clock = session.partition(" ")
            try:
                if (date.fromisoformat(day).isoformat() != day
                        or (clock and parse_local(session).strftime("%H:%M") != clock)):
                    raise ValueError()
            except ValueError as exc:
                raise ValueError("请填写目标演出日期，格式为 YYYY-MM-DD，可加 HH:MM") from exc
        if not existing or body.price_tiers != existing["price_tiers"]:
            if len(body.price_tiers) != 1:
                raise ValueError("只填写一个预约票档")
            if any(not p.isascii() or not p.isdecimal() or int(p) <= 0 for p in body.price_tiers):
                raise ValueError("票档价格请填写大于零的整数金额")
        task = TaskConfig(name=body.name, show_name=body.show_name,
                          on_sale_at=body.on_sale_at.strip() or "2000-01-01 00:00",
                          price_tiers=body.price_tiers, city=body.city, session=body.session,
                          **_options_to_fields(body.options))
        errors = task.validate()
        if errors:
            raise ValueError("; ".join(errors))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"参数无效: {exc}") from exc


def _with_schedule(task: dict, settings: dict) -> dict:
    task = {**task, "prewarm_minutes": int((task["options"] or {}).get(
        "prewarm_minutes", settings.get("default_prewarm_minutes", "5")))}
    if task["status"] in SCHEDULED_STATUSES:
        try:
            _, due, _ = schedule_times(task, settings)
            return {**task, "prewarm_at": format_time(due)}
        except ValueError:
            pass
    return {**task, "prewarm_at": None}


def _options_to_fields(options: Dict[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key in ("prewarm_minutes", "arm_lead_seconds", "max_retry_seconds",
                "max_attempts", "queue_wait_seconds", "post_submit_watch_seconds"):
        if key in options:
            fields[key] = int(options[key])
    for key in ("poll_interval_ms", "retry_backoff_ms", "confirm_wait_ms"):
        if key in options:
            pair = list(options[key])
            if len(pair) != 2:
                raise ValueError(f"{key} 应为 [min,max]")
            fields[key] = (int(pair[0]), int(pair[1]))
    return fields
