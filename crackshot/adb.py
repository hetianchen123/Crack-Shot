"""ADB 封装：路径探测、设备列表、shell、注入点击（带限速）。"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from .timeutil import now_ms

DAMAI_PACKAGE = "cn.damai"
# 大麦正常入口（MAIN/LAUNCHER）；深链组件见 navigator（2026-11 版）
DAMAI_LAUNCH_ACTIVITY = "cn.damai/.launcher.splash.SplashMainActivity"

_ADB_CANDIDATES = (
    "~/.local/bin/adb",
    "~/Library/Android/sdk/platform-tools/adb",
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    "/usr/bin/adb",
)


def find_adb(configured: Optional[str] = None) -> Optional[str]:
    """按 优先级探测可用的 adb：配置 > 环境变量 > 常见安装位置 > PATH。"""
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    env_path = os.environ.get("CRACKSHOT_ADB") or os.environ.get("ANDROID_HOME")
    if env_path:
        candidates.append(str(Path(env_path).expanduser() / "platform-tools" / "adb"))
    candidates.extend(_ADB_CANDIDATES)
    which = shutil.which("adb")
    if which:
        candidates.append(which)
    for candidate in candidates:
        path = str(Path(candidate).expanduser())
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, adb_path: str, serial: str, min_tap_interval_ms: int = 80):
        self.adb_path = adb_path
        self.serial = serial
        self._min_tap_interval_ms = max(0, min_tap_interval_ms)
        self._last_tap_ms = 0

    @staticmethod
    def list_devices(adb_path: str) -> List[dict]:
        out = _run(adb_path, ["devices", "-l"])
        devices = []
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("device", "offline", "unauthorized"):
                model = next((p[6:] for p in parts[2:] if p.startswith("model:")), None)
                devices.append({"id": parts[0], "state": parts[1], "model": model})
        return devices

    def shell(self, *args: str, timeout: float = 10.0) -> str:
        return _run(self.adb_path, ["-s", self.serial, "shell", *args], timeout=timeout)

    def tap(self, x: int, y: int) -> None:
        """注入点击；全局限速，避免脚本以超出人手的频率连点。"""
        gap = now_ms() - self._last_tap_ms
        if gap < self._min_tap_interval_ms:
            time.sleep((self._min_tap_interval_ms - gap) / 1000.0)
        self.shell("input", "tap", str(int(x)), str(int(y)))
        self._last_tap_ms = now_ms()

    def back(self) -> None:
        self.shell("input", "keyevent", "4")

    def wake(self) -> None:
        self.shell("input", "keyevent", "224")  # KEYCODE_WAKEUP

    def screencap(self, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(self.screencap_bytes())
        return out_path

    def screencap_bytes(self) -> bytes:
        result = subprocess.run(
            [self.adb_path, "-s", self.serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=15,
        )
        png_magic = bytes([137, 80, 78, 71, 13, 10, 26, 10])
        if not result.stdout.startswith(png_magic):
            raise AdbError("截图失败（可能被 FLAG_SECURE 页面拦截）")
        return result.stdout

    def screencap_raw(self) -> tuple:
        """获取未压缩的 RGBA 数据，避免手机编码 PNG。"""
        try:
            data = subprocess.check_output(
                [self.adb_path, "-s", self.serial, "exec-out", "screencap"],
                stderr=subprocess.PIPE, timeout=2.0)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdbError("原始截图读取失败") from exc
        if len(data) >= 16:
            width, height, pixel_format = struct.unpack_from("<III", data)
            offset = len(data) - width * height * 4
            if pixel_format in (1, 2) and width > 0 and height > 0 and offset in (12, 16):
                return width, height, memoryview(data)[offset:]
        raise AdbError("手机返回了不支持的原始截图格式")

    def foreground_package(self) -> Optional[str]:
        out = self.shell("dumpsys", "activity", "activities", timeout=8.0)
        for marker in ("topResumedActivity", "mResumedActivity"):
            idx = out.find(marker)
            if idx != -1:
                segment = out[idx: idx + 300].split()
                for token in segment:
                    if "/" in token and token.split("/")[0].count(".") >= 1:
                        package = token.split("/")[0]
                        if package not in ("u0", "0"):
                            return package
        return None

    def is_damai_foreground(self) -> bool:
        return self.foreground_package() == DAMAI_PACKAGE

    def has_package(self, package: str = DAMAI_PACKAGE) -> bool:
        return bool(self.shell("pm", "path", package).strip())


def _run(adb_path: str, args: List[str], timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            [adb_path, *args], capture_output=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise AdbError(f"找不到 adb: {adb_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb 超时: {' '.join(args)}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise AdbError(f"adb 失败({result.returncode}): {' '.join(args)} {stderr}")
    return result.stdout.decode("utf-8", "replace")
