"""截图 → 任务草稿：调用本地 Ollama 视觉模型提取大麦页面信息。

不联网、不调云端 API；Ollama 未运行或无视觉模型时返回明确错误，
前端回退到手动录入。
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unicodedata import normalize
from urllib.error import URLError
from urllib.request import Request, urlopen

PROMPT = """读取大麦 App 的演出详情页或场次/票档预约页截图，只输出 JSON：
{
  "show_name": "标题里连续出现的简短演出关键词，例如艺人名",
  "full_title": "截图中完整的演出标题",
  "city": "标题或场馆地址中的演出城市，不含市、站等后缀",
  "venue": "场馆名称",
  "status_text": "底部主按钮文字，例如 已预约",
  "sessions": ["所有可见场次的演出日期和时间，保留年份"],
  "price_tiers": ["所有可见票档的单张票价数字"],
  "on_sale_text": "页面上未绑定到某个场次的开抢/开售时间原文",
  "reserved_sessions": [{"session": "已预约卡片第一行的演出日期和时间，保留年份",
                         "on_sale_text": "同一张卡片里写着开抢/开售的那一行原文"}],
  "reserved_price_tiers": ["已预约票档的单张票价数字"]
}
先找底部「已预约」。在场次区和票档区分别检查每张卡片右侧的爱心：
实心橙色/黄色爱心表示已预约，常伴橙色边框和橙色文字。空心爱心（包括加号）
或仅写「可预约」表示可以预约，不能放入 reserved 字段。保留所有已预约项，无法确认就留空。
场次卡片的第一行是演出时间；第二行「M月D日 HH:MM开抢」是开票时间，属于该场次，
不是第二个演出日期。照抄这行，即使没有年份也保留。只提取实际看见的内容，未知填空字符串或空数组。"""

_DATE = r"(?<![0-9])([0-9]{4})\s*[-/.年]\s*([0-9]{1,2})\s*[-/.月]\s*([0-9]{1,2})(?![0-9])日?"
_MONTH_DAY = r"(?<![0-9])([0-9]{1,2})\s*月\s*([0-9]{1,2})(?![0-9])\s*日"
_TIME = r"\s*(?:(?:周|星期)[一二三四五六日天]\s*)?([0-9]{1,2}):([0-9]{2})(?![0-9])"
_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_FIELDS = {
    **dict.fromkeys(("show_name", "full_title", "city", "venue", "status_text", "on_sale_text"), _STRING),
    **dict.fromkeys(("sessions", "price_tiers", "reserved_price_tiers"), _STRINGS),
    "reserved_sessions": {"type": "array", "items": {
        "type": "object", "required": ["session", "on_sale_text"],
        "properties": {"session": _STRING, "on_sale_text": _STRING},
    }},
}
_SCHEMA = {"type": "object", "properties": _FIELDS, "required": list(_FIELDS)}

# 常见视觉模型名关键词（Ollama /api/tags 里按此挑默认值）
_VISION_HINTS = ("vl", "llava", "minicpm-v", "gemma3", "moondream", "vision", "bakllava")


def _request(url: str, payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status(ollama_url: str, configured_model: str) -> Dict[str, Any]:
    """Ollama 可达性 + 模型列表 + 将使用的模型。"""
    base = ollama_url.rstrip("/")
    try:
        with urlopen(f"{base}/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, ValueError) as exc:
        return {"reachable": False, "error": str(exc), "models": [],
                "model": configured_model or ""}
    models = [m.get("name", "") for m in data.get("models", [])]
    model = configured_model
    if not model:
        model = next((m for m in models if any(h in m.lower() for h in _VISION_HINTS)), "")
    return {"reachable": True, "models": models, "model": model}


def extract(image_bytes: bytes, ollama_url: str, configured_model: str,
            timeout: float = 180.0) -> Dict[str, Any]:
    """截图 → 任务草稿字段。返回 dict（前端直接预填表单）。"""
    base = ollama_url.rstrip("/")
    st = status(ollama_url, configured_model)
    if not st["reachable"]:
        raise RuntimeError(f"本地 Ollama 不可达（{base}），请先启动 Ollama")
    if not st["model"]:
        raise RuntimeError(
            "Ollama 中没有可用的视觉模型，请先安装（如 `ollama pull qwen2.5vl:7b`）"
            "或在设置里指定模型")
    payload = {
        "model": st["model"],
        "messages": [{
            "role": "user",
            "content": PROMPT,
            "images": [base64.b64encode(image_bytes).decode("ascii")],
        }],
        "stream": False,
        "think": False,
        "format": _SCHEMA,
        "options": {"temperature": 0, "num_ctx": 6144, "num_predict": 700},
    }
    try:
        resp = _request(f"{base}/api/chat", payload, timeout=timeout)
    except (URLError, OSError) as exc:
        raise RuntimeError(f"调用 Ollama 失败: {exc}") from exc
    content = (resp.get("message") or {}).get("content", "")
    parsed = _parse_json(content)
    if not parsed:
        raise RuntimeError("模型未返回有效 JSON，请重试或手动录入")
    return {**_draft_fields(parsed), "model": st["model"]}


def _draft_fields(parsed: Dict[str, Any]) -> Dict[str, Any]:
    prices = map(_price, _items(parsed.get("price_tiers")))
    tiers = list(dict.fromkeys(price for price in prices if price))[:12]
    sessions = _items(parsed.get("sessions"))
    reserved = _items(parsed.get("reserved_sessions"))
    reserved_prices = _items(parsed.get("reserved_price_tiers"))
    status_text = _text(parsed.get("status_text"))
    reservation = bool(reserved or reserved_prices
                       or any(word in status_text for word in ("已预约", "可预约", "取消预约")))
    card = reserved[0] if len(reserved) == 1 and isinstance(reserved[0], dict) else {}
    page_sale_text = _text(parsed.get("on_sale_text"))
    if reservation:
        session = _session(_text(card.get("session")))
        price = _price(reserved_prices[0]) if len(reserved_prices) == 1 else ""
        on_sale_text = _text(card.get("on_sale_text")) if session else ""
    else:
        session = _session(_text(sessions[0])) if len(sessions) == 1 else ""
        price = tiers[0] if len(tiers) == 1 else ""
        on_sale_text = page_sale_text
    return {
        "show_name": _text(parsed.get("show_name"))[:30],
        "full_title": _text(parsed.get("full_title"))[:120],
        "city": re.sub(r"[市站]$", "", _text(parsed.get("city")))[:30],
        "on_sale_text": on_sale_text or page_sale_text,
        "on_sale_guess": _guess_datetime(on_sale_text, session),
        "session_guess": session,
        "price_guess": price,
        "sessions": list(filter(None, map(_text, sessions))),
        "price_tiers": tiers,
        "venue": _text(parsed.get("venue"))[:60],
        "status_text": status_text[:30],
    }


def _parse_json(content: str) -> Optional[Dict[str, Any]]:
    content = content.strip()
    try:
        value = json.loads(content)
    except ValueError:
        # 模型偶尔包 markdown 代码块
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _price(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    numbers = re.findall(r"[-+]?[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?",
                         normalize("NFKC", str(value)))
    if len(numbers) != 1:
        return ""
    price = Decimal(numbers[0].replace(",", ""))
    return str(int(price)) if price > 0 and price == price.to_integral_value() else ""


def _session(value: str) -> str:
    text = normalize("NFKC", value)
    day = re.search(_DATE, text)
    if not day:
        return ""
    clock = re.compile(_TIME).match(text, day.end())
    try:
        parts = [int(part) for part in day.groups()]
        if clock:
            parts.extend(map(int, clock.groups()))
            return datetime(*parts).strftime("%Y-%m-%d %H:%M")
        return date(*parts).isoformat()
    except ValueError:
        return ""


def _guess_datetime(on_sale_text: str, session: str = "") -> str:
    """无年份的开售时间用同一场次的演出日期推定，可跨年。"""
    text = normalize("NFKC", on_sale_text)
    full = re.search(_DATE + _TIME, text)
    partial = re.search(_MONTH_DAY + _TIME, text) if not full and session else None
    anchor = date.fromisoformat(session[:10]) if session else None
    if full:
        candidates = [tuple(map(int, full.groups()))]
    elif partial:
        fields = tuple(map(int, partial.groups()))
        candidates = [(anchor.year, *fields), (anchor.year - 1, *fields)]
    else:
        return ""
    for parts in candidates:
        try:
            value = datetime(*parts)
        except ValueError:
            continue
        if not anchor or value.date() <= anchor:
            return value.strftime("%Y-%m-%d %H:%M")
    return ""
