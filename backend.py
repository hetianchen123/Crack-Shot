#!/usr/bin/env python3
"""管理当前项目的本地 Web 后台。"""

import argparse
import fcntl
import os
import signal
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VAR = ROOT / "var"
PORT = "8780"
URL = f"http://127.0.0.1:{PORT}"
LOG = VAR / "backend.log"


def listening_pids():
    result = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
                            capture_output=True, text=True)
    if result.stderr or result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "无法检查后台端口")
    return {int(pid) for pid in result.stdout.split()}


def backend_pid():
    listeners = listening_pids()
    if not listeners:
        return None
    if len(listeners) == 1:
        pid = next(iter(listeners))
        command = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True).stdout.split()
        cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True).stdout.splitlines()
        valid_args = (["main.py", "serve"], ["main.py", "serve", "--port", PORT])
        if command[1:] in valid_args and f"n{ROOT}" in cwd:
            return pid
    raise RuntimeError(f"端口 {PORT} 已被其他进程占用，PID: {', '.join(map(str, sorted(listeners)))}")


def start():
    pid = backend_pid()
    if pid:
        print(f"后台已在运行（PID {pid}） {URL}")
        return
    python = ROOT / ".venv/bin/python"
    if not os.access(python, os.X_OK):
        raise RuntimeError("请先按照 README 安装 .venv 和项目依赖")
    with LOG.open("ab") as log:
        process = subprocess.Popen([str(python), "main.py", "serve", "--port", PORT],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"后台启动失败，查看 {LOG}")
        if listening_pids() == {process.pid}:
            print(f"后台已启动（PID {process.pid}） {URL}")
            print(f"启动日志 {LOG}")
            return
        time.sleep(0.2)
    if process.poll() is None:
        process.terminate()
    raise RuntimeError(f"后台启动超时，查看 {LOG}")


def stop():
    pid = backend_pid()
    if pid is None:
        print("后台已停止")
        return
    db_uri = (VAR / "crackshot.db").as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as db:
        active = db.execute("SELECT id FROM runs WHERE finished_at IS NULL LIMIT 1").fetchone()
    if active:
        raise RuntimeError(f"运行 #{active[0]} 尚未结束，请在后台停止任务或等待它完成")
    if backend_pid() == pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if pid not in listening_pids():
            print(f"后台已停止（PID {pid}）")
            return
        time.sleep(0.2)
    raise RuntimeError(f"后台在 10 秒内未退出（PID {pid}）")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    try:
        VAR.mkdir(exist_ok=True)
        with (VAR / "backend.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "start":
                start()
            elif args.action == "stop":
                stop()
            else:
                pid = backend_pid()
                print(f"后台运行中（PID {pid}） {URL}" if pid else "后台已停止")
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
