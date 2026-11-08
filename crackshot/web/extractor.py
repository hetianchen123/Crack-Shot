"""截图 → 任务草稿：调用本地 Ollama 视觉模型提取大麦页面信息。

不联网、不调云端 API；Ollama 未运行或无视觉模型时返回明确错误，
前端回退到手动录入。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

PROMPT = """你是一个信息提取助手。这张截图来自大麦App（演出购票）。
请仔细阅读截图中的文字，提取以下信息并以严格 JSON 返回（不要输出其他内容）：
{
  "show_name": "演出名称关键词（用于在详情页核对的短词，如歌手名+城市，10字以内）",
  "full_title": "截图中完整的演出标题",
  "on_sale_text": "开售/预售/开抢时间相关文字，找不到则空字符串",
  "sessions": ["场次列表，如 2026-11-28 周六 19:00，没有则空数组"],
  "price_tiers": ["票档价格数字，如 688、488，按页面出现顺序，只保留数字"],
  "venue": "场馆名称，找不到则空字符串",
  "status_text": "底部按钮或状态文字，如 立即预订/即将开抢/缺货登记"
}
注意：只提取截图里真实可见的文字，不要编造；价格去掉货币符号只留数字。"""

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
        "format": "json",
        "options": {"temperature": 0.1},
    }
    try:
        resp = _request(f"{base}/api/chat", payload, timeout=timeout)
    except (URLError, OSError) as exc:
        raise RuntimeError(f"调用 Ollama 失败: {exc}") from exc
    content = (resp.get("message") or {}).get("content", "")
    parsed = _parse_json(content)
    if not parsed:
        raise RuntimeError("模型未返回有效 JSON，请重试或手动录入")
    tiers = _clean_tiers(parsed.get("price_tiers"))
    sessions = [str(s).strip() for s in (parsed.get("sessions") or []) if str(s).strip()]
    on_sale_text = str(parsed.get("on_sale_text") or "").strip()
    return {
        "show_name": str(parsed.get("show_name") or "").strip()[:30],
        "full_title": str(parsed.get("full_title") or "").strip()[:120],
        "on_sale_text": on_sale_text,
        "on_sale_guess": _guess_datetime(on_sale_text, sessions),
        "sessions": sessions,
        "price_tiers": tiers,
        "venue": str(parsed.get("venue") or "").strip()[:60],
        "status_text": str(parsed.get("status_text") or "").strip()[:30],
        "model": st["model"],
    }


def _parse_json(content: str) -> Optional[Dict[str, Any]]:
    content = content.strip()
    try:
        return json.loads(content)
    except ValueError:
        pass
    # 模型偶尔包 markdown 代码块
    match = re.search(r"\{.*\}", content, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None


def _clean_tiers(raw: Any) -> List[str]:
    tiers: List[str] = []
    for item in raw or []:
        digits = re.sub(r"[^\d]", "", str(item))
        if digits and digits not in tiers:
            tiers.append(digits)
    return tiers[:12]


def _guess_datetime(on_sale_text: str, sessions: List[str]) -> str:
    """尽力从开售文字/场次里猜 YYYY-MM-DD HH:MM；猜不到返回空串让人工填。"""
    text = f"{on_sale_text} {' '.join(sessions)}"
    md = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    hm = re.search(r"(\d{1,2}):(\d{2})", text)
    if not md:
        return ""
    date = f"{int(md.group(1)):04d}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
    if hm:
        return f"{date} {int(hm.group(1)):02d}:{hm.group(2)}"
    return date
