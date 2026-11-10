"""命令行入口：doctor / login / probe / watch / list / run。"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from uuid import uuid4
from pathlib import Path
from typing import Optional

from . import __version__
from .adb import Adb, AdbError, DAMAI_PACKAGE, find_adb
from .config import GlobalConfig, load_config
from .device import Device, U2Unavailable
from .notify import Notifier, SilentNotifier
from .pages import PageState, detail_button_state, recognize
from .scheduler import TaskRunner, pick_task
from .timeutil import format_remaining, now_ms

VAR_DIR = Path(__file__).resolve().parent.parent / "var"


def setup_logging(verbose: bool) -> Path:
    log_dir = VAR_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"crackshot-{time.strftime('%Y%m%d-%H%M%S')}.log"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")],
    )
    logging.getLogger(__name__).info("日志文件: %s", log_file)
    return log_file


def load_cfg(path: str) -> GlobalConfig:
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(f"配置文件不存在: {cfg_path}（可复制 tasks.example.yaml 为 tasks.yaml）")
    return load_config(cfg_path)


def connect_device(cfg: GlobalConfig) -> Adb:
    adb_path = find_adb(cfg.adb_path)
    if not adb_path:
        sys.exit("未找到 adb，请安装 platform-tools 或在配置里写 adb_path")
    devices = [d for d in Adb.list_devices(adb_path) if d["state"] == "device"]
    if not devices:
        sys.exit("没有已授权的设备：连接数据线、打开 USB 调试并在手机上授权")
    serial = cfg.device_serial
    if serial:
        if not any(d["id"] == serial for d in devices):
            sys.exit(f"配置的设备 {serial} 不在线，当前: {[d['id'] for d in devices]}")
    else:
        if len(devices) > 1:
            sys.exit(f"有多台设备 {[d['id'] for d in devices]}，请在配置里指定 device_serial")
        serial = devices[0]["id"]
    return Adb(adb_path, serial, cfg.min_tap_interval_ms)


# ---- 子命令 ----------------------------------------------------------


def cmd_doctor(args) -> int:
    cfg = load_cfg(args.config)
    print(f"Crack-shot v{__version__} 自检")
    adb_path = find_adb(cfg.adb_path)
    print(f"  adb: {adb_path or '未找到（安装 platform-tools 或配置 adb_path）'}")
    if not adb_path:
        return 1
    try:
        devices = Adb.list_devices(adb_path)
    except AdbError as exc:
        print(f"  adb 运行失败: {exc}")
        return 1
    for d in devices:
        mark = "✓" if d["state"] == "device" else "✗"
        print(f"  设备 {mark} {d['id']} [{d['state']}] {d['model'] or ''}")
    usable = [d for d in devices if d["state"] == "device"]
    if not usable:
        print("  没有可用设备")
        return 1
    serial = cfg.device_serial or (usable[0]["id"] if len(usable) == 1 else None)
    if not serial:
        print("  多台设备，请在 tasks.yaml 指定 device_serial")
        return 1
    adb = Adb(adb_path, serial)
    try:
        installed = adb.has_package()
        print(f"  大麦 App({DAMAI_PACKAGE}): {'已安装' if installed else '未安装 ✗'}")
        fg = adb.foreground_package()
        print(f"  前台应用: {fg or '未知'}")
    except AdbError as exc:
        print(f"  设备通信失败: {exc}")
        return 1
    try:
        Device(serial)
        print("  uiautomator2: 连接成功")
    except U2Unavailable as exc:
        print(f"  uiautomator2: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  uiautomator2: 连接失败（首次运行会在手机装 atx-agent）: {exc}")
        return 1
    print(f"  任务数: {len(cfg.tasks)}，时区: Asia/Shanghai，本机时间: {time.strftime('%F %T')}")
    print("自检通过 ✓")
    return 0


def cmd_login(args) -> int:
    from .navigator import LoginState, open_phone_login

    cfg = load_cfg(args.config) if Path(args.config).is_file() else GlobalConfig()
    try:
        adb = connect_device(cfg)
        adb.wake()
        state = open_phone_login(adb, Device(adb.serial))
    except (AdbError, U2Unavailable) as exc:
        print(f"检查大麦登录失败：{exc}")
        return 1
    if state == LoginState.SIGNED_IN:
        print("大麦已登录")
        return 0
    if state == LoginState.CONSENT_REQUIRED:
        print("已选择手机号登录，请在手机上确认协议后完成登录")
    else:
        print("已打开手机号登录页，请在手机上完成登录")
    return 2


def cmd_probe(args) -> int:
    cfg = load_cfg(args.config)
    adb = connect_device(cfg)
    try:
        device = Device(cfg.device_serial or adb.serial)
    except U2Unavailable as exc:
        sys.exit(str(exc))
    print("当前页面可交互元素（文字 → 坐标，用于确认关键词和位置）：")
    for el in device.dump_clickables():
        c = el.center
        state = "" if el.enabled else " [禁用]"
        print(f"  {el.text!r} @({c[0]},{c[1]}) clickable={el.clickable}{state}")
    task = pick_task(cfg, args.task)
    snap = recognize(device, task)
    print(f"\n页面识别: {snap.state.value}（命中: {snap.matched or '无'}）")
    btn = detail_button_state(device, task)
    print(f"详情页按钮: {btn.state.value}（{btn.matched or '未找到'}）")
    return 0


def cmd_watch(args) -> int:
    """演练模式：只观察按钮状态变化，不点击。开票前用它确认识别正常。"""
    cfg = load_cfg(args.config)
    adb = connect_device(cfg)
    try:
        device = Device(cfg.device_serial or adb.serial)
    except U2Unavailable as exc:
        sys.exit(str(exc))
    task = pick_task(cfg, args.task)
    on_sale_ms = task.on_sale_ms()
    print(f"监视「{task.name}」按钮状态（每秒一次，Ctrl+C 退出）")
    last = ""
    while True:
        try:
            snap = recognize(device, task)
            btn = detail_button_state(device, task)
            line = f"{time.strftime('%T')} 页面={snap.state.value}({snap.matched}) 按钮={btn.matched}"
            if line != last:
                print(line)
                last = line
            left = on_sale_ms - now_ms()
            if left > 0:
                print(f"    距开票 {format_remaining(left)}")
        except KeyboardInterrupt:
            print("\n结束监视")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"    读取异常: {exc}")
        time.sleep(1.0)


def cmd_list(args) -> int:
    cfg = load_cfg(args.config)
    print(f"共 {len(cfg.tasks)} 个任务：")
    for task in cfg.tasks:
        left = task.on_sale_ms() - now_ms()
        status = f"还有 {format_remaining(left)}" if left > 0 else f"已过 {-left // 60000} 分钟"
        print(f"  [{task.name}] {task.show_name}")
        print(f"    开票 {task.on_sale_at}（{status}）票档优先级 {task.price_tiers}")
    return 0


def cmd_run(args) -> int:
    cfg = load_cfg(args.config)
    task = pick_task(cfg, args.task)
    adb = connect_device(cfg)
    try:
        device = Device(cfg.device_serial or adb.serial)
    except U2Unavailable as exc:
        sys.exit(str(exc))
    notifier = SilentNotifier() if args.dry_run else Notifier()
    evidence_dir = VAR_DIR / "evidence" / task.name
    evidence_dir.mkdir(parents=True, exist_ok=True)
    recording = VAR_DIR / "recordings" / f"cli-{uuid4().hex}.mp4"
    runner = TaskRunner(cfg, task, adb, device, notifier, evidence_dir, dry_run=args.dry_run,
                        recording_path=recording)
    outcome = runner.run(ignore_schedule=args.now)
    print(f"任务结束: {outcome}")
    return 0 if outcome == "success" else 2


def cmd_serve(args) -> int:
    """启动 Web 后台（账号/设备/任务管理 + 多设备调度）。"""
    import uvicorn

    from .web.app import create_app

    db_path = VAR_DIR / (args.db or "crackshot.db")
    app = create_app(db_path)
    print(f"Web 后台: http://127.0.0.1:{args.port} （数据库: {db_path}）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crackshot", description="大麦个人抢票辅助（ADB + uiautomator2）"
    )
    parser.add_argument("--config", default="tasks.yaml", help="配置文件路径（默认 tasks.yaml）")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="自检：adb/设备/大麦/依赖")
    sub.add_parser("login", help="检查大麦登录状态，未登录时打开手机号登录")
    sub.add_parser("list", help="列出任务")

    p_probe = sub.add_parser("probe", help="查看当前页面元素与识别结果")
    p_probe.add_argument("--task", help="按哪个任务的关键词识别（默认自动选择）")

    p_watch = sub.add_parser("watch", help="监视按钮状态变化（不点击，演练用）")
    p_watch.add_argument("--task", help="任务名")

    p_run = sub.add_parser("run", help="执行抢票任务")
    p_run.add_argument("--task", help="任务名（默认最近开票的）")
    p_run.add_argument("--dry-run", action="store_true", help="只识别不点击")
    p_run.add_argument("--now", action="store_true", help="跳过定时，立即进入抢票循环（演练用）")

    p_serve = sub.add_parser("serve", help="启动 Web 后台")
    p_serve.add_argument("--port", type=int, default=8780)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--db", help="SQLite 文件名（默认 crackshot.db）")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    handlers = {
        "doctor": cmd_doctor,
        "login": cmd_login,
        "probe": cmd_probe,
        "watch": cmd_watch,
        "list": cmd_list,
        "run": cmd_run,
        "serve": cmd_serve,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
