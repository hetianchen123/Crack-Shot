"""从已确认的演出详情页核对日期和票价。"""

import re
from datetime import date

_DATE = re.compile(r"(?<!\d)(?:(\d{4})[-./年])?(\d{1,2})[-./月](\d{1,2})(?!\d)")
_CLOCK = re.compile(r"\d{1,2}:\d{2}")


def session_matches(wanted: str, label: str) -> bool:
    target = _DATE.search(wanted)
    actual = _DATE.search(label)
    if not target or not actual:
        return wanted in label
    ty, tm, td = target.groups()
    ay, am, ad = actual.groups()
    if (ty and ay and ty != ay) or (int(tm), int(td)) != (int(am), int(ad)):
        return False
    clock = _CLOCK.search(wanted)
    actual_clock = _CLOCK.search(label)
    return not clock or bool(actual_clock and clock.group() == actual_clock.group())


def _date_bounds(text: str):
    matches = list(_DATE.finditer(text))
    if not matches or not matches[0][1]:
        return None
    first, last = matches[0], matches[-1]
    try:
        start = date(int(first[1]), int(first[2]), int(first[3]))
        month, day = int(last[2]), int(last[3])
        year = int(last[1]) if last[1] else start.year + int((month, day) < (start.month, start.day))
        end = date(year, month, day)
        return (start, end) if start <= end else None
    except ValueError:
        return None


def inspect_show_details(device, task) -> dict:
    def text(hint):
        node = device.find_by_id(hint)
        return (node.text or node.description).strip() if node else ""

    time_text = text("info_v2_time_and_duration_tv")
    bounds = _date_bounds(time_text)
    target = _DATE.search(task.session)
    date_check = "需人工核对演出日期"
    page_date = time_text
    target_date = ""
    date_mismatch = False
    if bounds:
        start, end = bounds
        page_date = str(start) if start == end else f"{start} 至 {end}"
        if target:
            year, month, day = target.groups()
            dates = set()
            for candidate_year in (int(year),) if year else (start.year, end.year):
                try:
                    day_on_calendar = date(candidate_year, int(month), int(day))
                    if start <= day_on_calendar <= end:
                        dates.add(day_on_calendar)
                except ValueError:
                    continue
            date_mismatch = not dates
            if not year and len(dates) == 1:
                target_date = str(next(iter(dates)))
                clock = _CLOCK.search(task.session)
                if clock:
                    target_date += f" {clock.group()}"
            date_check = ("预填日期与页面不符" if date_mismatch else
                          "日期与页面一致" if start == end else "日期落在页面显示范围内")

    prices = []
    for hint in ("info_v2_price_left", "info_v2_price_right"):
        value = text(hint).replace(",", "").strip("¥￥元 ")
        if re.fullmatch(r"[0-9]+", value):
            prices.append(int(value))
    page_price = ""
    price_check = "需人工核对票档"
    outside = []
    if len(prices) == 2:
        low, high = sorted(prices)
        page_price = f"¥{low}" if low == high else f"¥{low}–¥{high}"
        outside = [p for p in task.price_tiers if p.isascii() and p.isdecimal()
                   and not low <= int(p) <= high]
        price_check = (f"超出页面范围：{'、'.join('¥' + p for p in outside)}" if outside else
                       "预填票档在价格范围内，具体票档待核对")
    elif prices:
        page_price = f"¥{prices[0]}"
        price_check = "页面仅读到一个价格，具体票档待核对"

    return {"target_date": target_date, "page_date": page_date, "page_price": page_price, "date_check": date_check,
            "price_check": price_check, "mismatch": date_mismatch or bool(outside)}
