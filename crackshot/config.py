"""任务配置：YAML 加载、默认值与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from .timeutil import epoch_ms, parse_local

# 详情页底部按钮：可点击则进入选票页
DEFAULT_BUY_TEXTS = ["立即预订", "立即购买"]
# 这些文字出现说明还没开票/不可购买，继续等
DEFAULT_WAIT_TEXTS = ["即将开抢", "缺货登记", "即将开售", "暂无余票", "已结束", "已售罄"]
# 出现即判定为验证码/风控，必须停下交人工
DEFAULT_CAPTCHA_TEXTS = ["请完成验证", "滑动验证", "滑块验证", "拖动滑块", "验证码", "安全验证"]
# 常见失败弹窗文案
DEFAULT_ERROR_TEXTS = [
    "太热情", "排队人数较多", "网络异常", "下单失败", "提交失败",
    "已售罄", "库存不足", "余票不足", "很抱歉", "哎呀",
]
DEFAULT_ERROR_BUTTON_TEXTS = ["确定", "知道", "知道了", "好的", "重新加载"]


@dataclass
class TaskConfig:
    name: str
    show_name: str                      # 详情页可见的演出关键词，用于核对停对了页面
    on_sale_at: str                     # 开票时间 YYYY-MM-DD HH:MM[:SS]（北京时间）
    price_tiers: List[str]              # 票档优先级，如 ["688", "488"]，匹配票档文本
    share_url: str = ""                 # 已废弃：分享链接只落 H5，改用 App 内搜索导航
    city: str = ""                      # 地点（城市/站点），搜索结果多城巡演时用于点对卡片
    session: str = ""                   # 场次（如 11-28 或 周六 19:00），选票页优先点匹配场次
    prewarm_minutes: int = 5            # 提前几分钟校验页面（人工应已停在详情页）
    arm_lead_seconds: int = 2           # 开票前几秒进入紧轮询
    max_retry_seconds: int = 90         # 开票后抢票窗口长度
    max_attempts: int = 40              # 进入订单流程前的尝试上限；订单重试由时间窗口控制
    retry_backoff_ms: Tuple[int, int] = (400, 1200)
    poll_interval_ms: Tuple[int, int] = (120, 280)   # 按钮状态轮询抖动区间
    confirm_wait_ms: Tuple[int, int] = (80, 160)     # 连续点击之间的抖动
    queue_wait_seconds: int = 60        # 提交后排队页最长等待
    post_submit_watch_seconds: int = 10
    buy_texts: List[str] = field(default_factory=lambda: list(DEFAULT_BUY_TEXTS))
    wait_texts: List[str] = field(default_factory=lambda: list(DEFAULT_WAIT_TEXTS))
    captcha_texts: List[str] = field(default_factory=lambda: list(DEFAULT_CAPTCHA_TEXTS))
    error_texts: List[str] = field(default_factory=lambda: list(DEFAULT_ERROR_TEXTS))
    error_button_texts: List[str] = field(default_factory=lambda: list(DEFAULT_ERROR_BUTTON_TEXTS))
    # 开票前锁定目标页底部坐标，到点只直点一次；后续重新识别按钮。
    blind_tap: bool = True
    blind_tap_texts: List[str] = field(default_factory=lambda: ["即将开抢", "即将开售"])

    def on_sale_ms(self) -> int:
        return epoch_ms(parse_local(self.on_sale_at))

    def validate(self) -> List[str]:
        errors = []
        if not self.name.strip():
            errors.append("task.name 不能为空")
        if not self.show_name.strip():
            errors.append(f"[{self.name}] show_name 不能为空")
        if not self.price_tiers:
            errors.append(f"[{self.name}] price_tiers 至少配置一个票档关键词")
        if self.share_url and not self.share_url.startswith(
                ("http://", "https://", "damai://", "cn.damai")):
            errors.append(f"[{self.name}] share_url 应为大麦分享链接（http/https/damai://）")
        if not self.city.strip() and self.session.strip():
            errors.append(f"[{self.name}] 配置了 session 建议同时配置 city，避免点错巡演城市")
        try:
            self.on_sale_ms()
        except ValueError as exc:
            errors.append(f"[{self.name}] on_sale_at 无效: {exc}")
        if not (1 <= self.prewarm_minutes <= 60):
            errors.append(f"[{self.name}] prewarm_minutes 建议在 1~60 分钟")
        if not (0 <= self.arm_lead_seconds <= 30):
            errors.append(f"[{self.name}] arm_lead_seconds 建议在 0~30 秒")
        if self.max_retry_seconds <= 0 or self.max_attempts <= 0:
            errors.append(f"[{self.name}] max_retry_seconds / max_attempts 必须为正")
        return errors


@dataclass
class GlobalConfig:
    device_serial: Optional[str] = None   # 多台设备时必填；单台可自动
    adb_path: Optional[str] = None        # 默认自动探测
    clock_offset_ms: int = 0              # 手机比电脑快/慢的修正，正数=电脑时钟需加
    min_tap_interval_ms: int = 80         # 两次点击之间最小间隔（安全限速）
    tasks: List[TaskConfig] = field(default_factory=list)

    def validate(self) -> List[str]:
        errors: List[str] = []
        names = set()
        for task in self.tasks:
            errors.extend(task.validate())
            if task.name in names:
                errors.append(f"任务名重复: {task.name}")
            names.add(task.name)
        return errors


def load_config(path: os.PathLike) -> GlobalConfig:
    """从 YAML 读取配置；未知字段忽略，列表字段支持部分覆盖。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = GlobalConfig(
        device_serial=raw.get("device_serial"),
        adb_path=raw.get("adb_path"),
        clock_offset_ms=int(raw.get("clock_offset_ms", 0)),
        min_tap_interval_ms=int(raw.get("min_tap_interval_ms", 80)),
    )
    for item in raw.get("tasks", []) or []:
        fields = {
            "name": item.get("name", ""),
            "show_name": item.get("show_name", ""),
            "on_sale_at": str(item.get("on_sale_at", "")),
            "price_tiers": [str(p) for p in item.get("price_tiers", [])],
            "share_url": str(item.get("share_url", "") or ""),
            "city": str(item.get("city", "") or ""),
            "session": str(item.get("session", "") or ""),
        }
        int_keys = (
            "prewarm_minutes", "arm_lead_seconds", "max_retry_seconds",
            "max_attempts", "queue_wait_seconds", "post_submit_watch_seconds",
        )
        for key in int_keys:
            if key in item:
                fields[key] = int(item[key])
        pair_keys = ("retry_backoff_ms", "poll_interval_ms", "confirm_wait_ms")
        for key in pair_keys:
            if key in item:
                pair = list(item[key])
                if len(pair) != 2:
                    raise ValueError(f"[{fields['name']}] {key} 应为 [min, max] 两个数")
                fields[key] = (int(pair[0]), int(pair[1]))
        list_keys = (
            "buy_texts", "wait_texts", "captcha_texts",
            "error_texts", "error_button_texts",
        )
        for key in list_keys:
            if key in item:
                fields[key] = [str(v) for v in item[key]]
        cfg.tasks.append(TaskConfig(**fields))
    errors = cfg.validate()
    if errors:
        raise ValueError("配置有误:\n  - " + "\n  - ".join(errors))
    return cfg


def example_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tasks.example.yaml"
