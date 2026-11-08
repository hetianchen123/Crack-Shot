"""通知：仅文字（终端输出 + macOS 文字横幅），不调用任何系统声音。"""

from __future__ import annotations

import shutil
import subprocess


class Notifier:
    """用户要求（2026-11-08）：不要系统声音/语音播报，只保留文字提示。"""

    def __init__(self) -> None:
        self._osascript = shutil.which("osascript")

    def info(self, message: str) -> None:
        self._notify("Crack-shot", message)

    def success(self, message: str) -> None:
        self._notify("抢票成功", message, critical=True)

    def critical(self, message: str) -> None:
        """需要人工处理：文字横幅（无声音），页面上以醒目状态呈现。"""
        self._notify("需要人工处理", message, critical=True)

    def _notify(self, title: str, message: str, critical: bool = False) -> None:
        print(f"[{'!!' if critical else '通知'}] {title}: {message}")
        if self._osascript:
            safe_msg = message.replace('"', "'")
            safe_title = title.replace('"', "'")
            subprocess.run(
                [
                    self._osascript, "-e",
                    f'display notification "{safe_msg}" with title "{safe_title}"',
                ],
                capture_output=True,
                timeout=5,
            )


class SilentNotifier(Notifier):
    """dry-run / 测试用：只打印。"""

    def _notify(self, title: str, message: str, critical: bool = False) -> None:
        print(f"[dry-run{'·需人工' if critical else ''}] {title}: {message}")
