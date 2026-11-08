"""正式运行的后台录屏。"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .adb import Adb

log = logging.getLogger("crackshot.recording")


def _find_scrcpy():
    bundled = Path(__file__).resolve().parent.parent / "var/tools/scrcpy/scrcpy"
    for candidate in (os.environ.get("CRACKSHOT_SCRCPY"), str(bundled), "scrcpy"):
        if candidate:
            path = shutil.which(os.path.expanduser(candidate))
            if path:
                return path
    return None


@contextmanager
def record_screen(adb: Adb, output: Path, limit_seconds: int):
    """启动独立录屏进程，退出时发送中断让 MP4 正常封尾。"""
    process = diagnostics = None
    partial = output.with_suffix(".partial.mp4")
    try:
        scrcpy = _find_scrcpy()
        if not scrcpy:
            raise OSError("未找到 scrcpy")
        output.parent.mkdir(parents=True, exist_ok=True)
        diagnostics = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [scrcpy, "--serial", adb.serial, "--no-window", "--no-playback",
             "--no-control", "--no-audio", "--max-size=1280", "--max-fps=15",
             "--video-bit-rate=2M", f"--time-limit={limit_seconds}",
             f"--record={partial}"],
            env={**os.environ, "ADB": adb.adb_path}, stdin=subprocess.DEVNULL,
            stdout=diagnostics, stderr=subprocess.STDOUT, start_new_session=True, umask=0o077,
        )
        log.info("录屏进程已启动")
    except OSError as exc:
        log.warning("录屏启动失败: %s", exc)

    try:
        yield
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                try:
                    code = process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise OSError("录屏进程停止超时") from None
                if code != 0 or not partial.is_file() or not partial.stat().st_size:
                    end = diagnostics.seek(0, os.SEEK_END)
                    diagnostics.seek(max(0, end - 1500))
                    message = diagnostics.read().decode("utf-8", "replace").strip()
                    raise OSError(f"没有可回放的 MP4（退出码 {code}）{message}")
                partial.replace(output)
                log.info("录屏已保存: %s", output)
        except OSError as exc:
            log.warning("录屏保存失败: %s", exc)
        finally:
            if diagnostics is not None:
                diagnostics.close()
            try:
                partial.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("无法清理录屏临时文件: %s", exc)
