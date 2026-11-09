"""只读检查大麦预约页上带实心爱心的场次和票档。"""

import base64
import io
import re
import time

from PIL import Image

from .navigator import is_target_detail
from .pages import PageState, recognize
from .timeutil import now_ms, parse_local
from .web import extractor


class ReservationUnavailable(RuntimeError):
    pass


RESERVATION_PROMPT = """这是大麦 App 截图。只输出 JSON。
page 必须是 reservation 或 other：只有同时看见「预约想看场次」「预约想看票档」和
底部「已预约」按钮才可填 reservation，否则填 other。
sessions 和 prices 只记录右侧有实心金色/黄色爱心的选项。空心爱心、仅写
「可预约」的票档、只有边框的选项都不要记录。保留每个有实心爱心的选项，
没有则返回空数组，不要推测。sessions 格式 YYYY-MM-DD HH:MM，
价格只返回金额数字字符串。例如：
{"page":"reservation","sessions":["2026-11-28 19:30"],"prices":["588"]}。"""

ENTRY_PROMPT = """这是大麦演出详情页底部橙色按钮的局部截图。
只输出 JSON，字段 text 原样抄写按钮主标题。
如果看不清或没有文字，填空字符串。不要根据其他文字推断。"""


def _inspect_image(image, base, model, prompt, schema):
    payload = {
        "model": model, "stream": False, "think": False, "format": schema,
        "options": {"temperature": 0, "num_predict": 220},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(image).decode("ascii")]}],
    }
    try:
        result = extractor._request(f"{base}/api/chat", payload, timeout=45)
        content = (result.get("message") or {}).get("content", "")
        parsed = extractor._parse_json(content)
        if isinstance(parsed, dict):
            return parsed
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReservationUnavailable("本机视觉模型未能读取预约截图") from exc
    raise ReservationUnavailable("本机视觉模型未返回有效的预约信息")


def _already_reserved(adb, bar, base, model):
    with Image.open(io.BytesIO(adb.screencap_bytes())) as image:
        crop = image.crop(bar.bounds)
        output = io.BytesIO()
        crop.save(output, format="PNG")
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    result = _inspect_image(output.getvalue(), base, model, ENTRY_PROMPT, schema)
    return "".join(str(result.get("text", "")).split()) == "已预约"


def _selection(image, base, model):
    strings = {"type": "array", "items": {"type": "string"}}
    schema = {"type": "object", "properties": {
        "page": {"type": "string", "enum": ["reservation", "other"]},
        "sessions": strings, "prices": strings,
    }, "required": ["page", "sessions", "prices"]}
    return _inspect_image(image, base, model, RESERVATION_PROMPT, schema)


def _unique_selection(result):
    if result.get("page") != "reservation":
        raise ReservationUnavailable("未能确认已进入预约页")
    sessions, prices = result.get("sessions"), result.get("prices")
    if not isinstance(sessions, list) or not isinstance(prices, list) or len(sessions) != 1 or len(prices) != 1:
        raise ReservationUnavailable("预约场次或票档未能唯一确认，请在手机上核对勾选项")
    session, price = str(sessions[0]).strip(), str(prices[0]).strip()
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}", session):
            raise ValueError()
        parsed = parse_local(session)
        if not re.fullmatch(r"[0-9]+", price) or int(price) <= 0:
            raise ValueError()
    except ValueError as exc:
        raise ReservationUnavailable("预约场次或票档格式不明确，请在手机上核对") from exc
    return parsed.strftime("%Y-%m-%d %H:%M"), str(int(price))


def read_reservation(adb, device, task, settings, sale_at_ms):
    """只点详情页的已预约入口，读完用返回键退出。"""
    base = settings["ollama_url"].rstrip("/")
    local = extractor.status(base, settings["vlm_model"])
    if not local["reachable"] or not local["model"]:
        raise ReservationUnavailable("本机视觉模型不可用，暂时无法读取预约勾选项")
    model = local["model"]
    if not is_target_detail(device, task):
        raise ReservationUnavailable("不在目标演出详情页")
    bar = device.find_by_id("purchase_status_bar")
    if not bar or not bar.enabled:
        raise ReservationUnavailable("未找到详情页购买栏")
    left, top, right, bottom = bar.bounds
    button = device.find(["已预约"])
    if button and button.enabled:
        point = button.center
    elif _already_reserved(adb, bar, base, model):
        point = bar.center
    else:
        raise ReservationUnavailable("未找到明确的「已预约」入口")

    x, y = point
    device.refresh(fast=True)
    current_bar = device.find_by_id("purchase_status_bar")
    if (not left <= x <= right or not top <= y <= bottom or not current_bar
            or current_bar.bounds != bar.bounds or now_ms() >= sale_at_ms
            or not is_target_detail(device, task)):
        raise ReservationUnavailable("已预约入口所在页面发生变化")
    adb.tap(x, y)

    on_reservation = False
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until:
            device.refresh(fast=True)
            if recognize(device, task).state == PageState.RESERVE:
                on_reservation = True
                break
            time.sleep(.15)
        result = _selection(adb.screencap_bytes(), base, model)
        on_reservation = on_reservation or result.get("page") == "reservation"
        return _unique_selection(result)
    finally:
        if on_reservation and adb.is_damai_foreground():
            device.refresh(fast=True)
            if recognize(device, task).state in (PageState.RESERVE, PageState.UNKNOWN):
                adb.back()
                device.refresh()
