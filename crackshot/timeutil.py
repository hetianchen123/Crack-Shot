"""时间与随机抖动工具。"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def now_ms() -> int:
    return int(time.time() * 1000)


def jitter_ms(pair) -> int:
    """在 [min, max] 毫秒区间内取随机值；越界值自动夹紧。"""
    lo, hi = int(pair[0]), int(pair[1])
    if lo > hi:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def sleep_jitter(pair) -> float:
    """按抖动区间睡眠，返回实际睡眠秒数。"""
    seconds = jitter_ms(pair) / 1000.0
    time.sleep(seconds)
    return seconds


def parse_local(text: str, tz: ZoneInfo = LOCAL_TZ) -> datetime:
    """解析 'YYYY-MM-DD HH:MM[:SS]' 本地时间。"""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"时间格式应为 YYYY-MM-DD HH:MM[:SS]，收到: {text!r}")


def epoch_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return int(dt.timestamp() * 1000)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_remaining(ms_left: int) -> str:
    seconds = max(0, ms_left) // 1000
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}小时{m:02d}分{s:02d}秒"
    if m:
        return f"{m}分{s:02d}秒"
    return f"{s}秒"
