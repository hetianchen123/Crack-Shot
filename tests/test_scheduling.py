"""定时流程集成测试：真实 API / SQLite / 调度与执行线程；替换手机和时钟。"""
import asyncio
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crackshot import grabber, navigator, scheduler
from crackshot.device import Device, _parse_nodes
from crackshot.config import TaskConfig
from crackshot.timeutil import parse_local
from crackshot.web import db, runner
from crackshot.web.app import create_app
from crackshot.reservation import ReservationUnavailable
from crackshot.pages import PageState, recognize
from crackshot.tiers import read_selection

SHOW = "示例巡演"
SALE = "2026-11-09 15:00:00"
SALE_MS = int(parse_local(SALE).timestamp() * 1000)


def node(label="", rid="", bounds="[0,0][1080,2400]", children="", clazz="", selected=False,
         enabled=True, checked=False):
    return (f'<node text="{label}" resource-id="{rid}" bounds="{bounds}"'
            f' enabled="{str(enabled).lower()}" clickable="true" class="{clazz}"'
            f' checked="{str(checked).lower()}"'
            f' selected="{str(selected).lower()}">{children}</node>')


def device_with_xml(xml):
    dev = Device.__new__(Device)
    dev.refresh()
    def dump():
        return f"<hierarchy>{xml()}</hierarchy>"
    dev._d = SimpleNamespace(dump_hierarchy=dump, settings={"max_depth": 50},
                             jsonrpc=SimpleNamespace(dumpWindowHierarchy=lambda *_: dump()))
    return dev


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")

    def pick(self, body):
        header = node(f"{SHOW} 杭州", "header_search_v2", "[0,0][1080,250]",
                      clazz="android.widget.EditText")
        return navigator._search_result(
            _parse_nodes(node(children=header + body)), self.task)

    def test_city_grid(self):
        grid = node(bounds="[0,300][1080,900]", children=
                    node(f"{SHOW}·演唱会", "title", "[10,320][900,430]")
                    + node("长沙", "tv_city", "[0,440][350,560]")
                    + node("杭州", "tv_city", "[350,440][700,560]"))
        self.assertEqual(self.pick(grid).center, (525, 500))

    def test_vertical_rows_with_separate_city(self):
        row = lambda city, top: node(bounds=f"[0,{top}][1080,{top + 300}]", children=
                     node(f"{SHOW}·演唱会", "result_title", f"[0,{top}][750,{top + 150}]")
                     + node(city, "city_label", f"[20,{top + 170}][420,{top + 260}]"))
        self.assertEqual(self.pick(row("长沙", 300) + row("杭州", 650)).center, (220, 865))
        self.assertIsNone(self.pick(row("长沙", 300) + row("宁波", 650)))

    def test_missing_first_screen_does_not_swipe(self):
        dev = device_with_xml(lambda: node("其他演出", "title", "[0,320][900,430]"))
        class NoInput:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected device operation: {name}")
        with patch.object(navigator, "time", SimpleNamespace(sleep=lambda _: None)):
            self.assertFalse(navigator.tap_result(dev, NoInput(), self.task))

    def test_city_strip_does_not_turn_detail_into_tour(self):
        def detail(city):
            return device_with_xml(lambda: node(children=
                node(rid="info_v2_title_layout", children=
                     node(f"{city} · {SHOW}", "info_v2_title_tv1"))
                + node(rid="trade_project_detail_purchase_status_bar_container_fl")
                + node("杭州站", "tour_city_name")))
        self.assertTrue(navigator.is_target_detail(detail("杭州"), self.task))
        self.assertFalse(navigator.is_target_detail(detail("宁波"), self.task))

    def test_real_name_notice_closes_only_the_acknowledgement(self):
        dev = device_with_xml(lambda: node(children=
            node("实名制观演") + node("预选实名观演人", bounds="[100,1300][900,1400]")
            + node("知道了", bounds="[100,1420][900,1500]")))
        taps = []
        self.assertEqual(recognize(dev, self.task).state, PageState.TICKET_NOTICE)
        self.assertTrue(navigator.dismiss_ticket_notice(dev, SimpleNamespace(tap=lambda *xy: taps.append(xy))))
        self.assertEqual(taps, [(500, 1460)])

    def test_native_confirm_requires_exact_selected_tier_and_consistent_total(self):
        task = TaskConfig("test", SHOW, SALE, ["480"], city="杭州", session="2027-01-09 19:30")
        def checkout(amount):
            return device_with_xml(lambda: node(children=
                node(f"{SHOW} · 杭州站", "title_tv")
                + node(rid="project_detail_perform_flowlayout", children=
                    node("2027-01-09 周六 19:30", "ll_perform_item", selected=True))
                + node(rid="project_detail_perform_price_flowlayout", children=
                    node("看台480元", "ll_perform_item", selected=True)
                    + node("看台680元", "ll_perform_item"))
                + node(rid="id_sku_bottom_bar_container", children=node(amount, "tv_price")
                    + node("确定", bounds="[600,2100][1000,2240]"))))
        def no_capture():
            self.fail("控件已经给出确认信息，不应截图")
        result = read_selection(checkout("960").elements(), no_capture, task)
        self.assertEqual((result.action, result.point, result.quantity), ("confirm", (800, 2170), 2))
        self.assertEqual(read_selection(checkout("680").elements(), no_capture, task).action, "wait")


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(Path(self.tmp.name) / "test.db")
        self.manager = self.app.state.run_manager
        self.now = SALE_MS - 300001
        self.state = "home"
        self.nav_times = []
        self.taps = []
        self.wakes = []
        self.recording_starts = []
        self.recording_stops = []
        self.evidence_times = []
        self.patches = [
            patch("crackshot.web.app.now_ms", lambda: self.now),
            patch.object(runner, "now_ms", lambda: self.now),
            patch.object(scheduler, "now_ms", lambda: self.now),
            patch.object(grabber, "now_ms", lambda: self.now),
            patch.object(scheduler, "time", SimpleNamespace(sleep=self.advance)),
            patch.object(scheduler, "record_screen", self.fake_record_screen),
            patch.object(grabber, "time", SimpleNamespace(sleep=self.advance)),
            patch.object(grabber, "sleep_jitter", lambda _: self.advance(0.2)),
            patch.object(runner, "find_adb", return_value="fake-adb"),
            patch.object(runner, "Adb", side_effect=lambda *args: self.adb()),
            patch.object(runner, "Device", side_effect=lambda *args: device_with_xml(self.xml)),
            patch.object(self.manager, "scan_devices", return_value={"fake-phone": ""}),
            patch.object(navigator, "navigate_to_detail", side_effect=self.navigate),
            patch.object(runner, "read_reservation", side_effect=ReservationUnavailable("模拟详情页未提供入口")),
            patch.object(runner, "WebNotifier", side_effect=lambda *args:
                         SimpleNamespace(info=lambda _: None, critical=lambda _: None)),
        ]
        for mock in self.patches:
            mock.start()
            self.addCleanup(mock.stop)
        self.device_id = self.request("POST", "/api/devices", {"serial": "fake-phone"}, 201)["id"]

    @contextmanager
    def fake_record_screen(self, _adb, path, _limit_seconds):
        self.recording_starts.append(self.now)
        try:
            yield SimpleNamespace(poll=lambda: None)
        finally:
            self.recording_stops.append(self.now)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test recording")

    def advance(self, seconds):
        self.now += max(1, round(seconds * 1000))
        if (getattr(self, "interrupt_at", None) is not None
                and self.now >= self.interrupt_at and self.state == "detail"):
            self.state = "home"
            self.interrupt_at = None

    def xml(self):
        if self.state == "detail":
            countdown = (node("杭州站 明天15:00开抢", "count_sell_time", "[10,200][900,400]")
                         if self.now < SALE_MS else
                         node("立即预订", "purchase_status_bar", "[600,2000][1000,2250]"))
            return node(children=node(f"{SHOW} · 杭州站", "info_v2_title", "[10,50][900,200]")
                        + countdown + node("2026.11.28-11.29 约120分钟", "info_v2_time_and_duration_tv")
                        + node("488", "info_v2_price_left") + node("888", "info_v2_price_right"))
        return node("请完成验证" if self.state == "captcha" else "首页")

    def adb(self):
        def tap(*point):
            self.taps.append((self.now, point))
            self.state = "captcha"
        def screenshot(path):
            self.evidence_times.append(self.now)
            Path(path).write_bytes(b"simulated")
        return SimpleNamespace(wake=lambda: self.wakes.append(self.now),
                               is_damai_foreground=lambda: True,
                               shell=lambda *args: "0", tap=tap, screencap=screenshot)

    def navigate(self, _adb, _device, _task, *args, **kwargs):
        self.nav_times.append(self.now)
        self.state = "detail"
        return True

    def request(self, method, path, body=None, expected=200, *, headers=(), raw=False):
        async def call():
            messages = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body or {}).encode()}
            async def send(message):
                messages.append(message)
            await self.app({"type": "http", "asgi": {"version": "3.0"}, "method": method,
                            "path": path, "headers": [(b"content-type", b"application/json"), *headers],
                            "query_string": b"", "scheme": "http", "root_path": "",
                            "http_version": "1.1"}, receive, send)
            payload = b"".join(m.get("body", b"") for m in messages)
            if not raw:
                payload = json.loads(payload)
            self.assertEqual(messages[0]["status"], expected, payload)
            return payload
        return asyncio.run(call())

    def wait_run(self, tid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            records = db.list_runs(task_id=tid)
            if records and records[0]["finished_at"] and not self.manager.is_task_running(tid):
                return records[0]
            time.sleep(0.01)
        self.fail("调度线程没有结束")

    def create_draft(self, session="2026-11-28", price_tiers=None, expected=201):
        return self.request("POST", "/api/tasks", {
            "name": "测试", "show_name": SHOW, "city": "杭州", "session": session,
            "device_id": self.device_id, "price_tiers": price_tiers or ["688"],
        }, expected).get("id")

    def test_create_calibrate_confirm_and_click_at_sale(self):
        tid = self.create_draft()
        task = self.request("GET", f"/api/tasks/{tid}")
        self.assertEqual((task["status"], task["city"], task["session"]), ("draft", "杭州", "2026-11-28"))
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE}, 409)
        self.manager._tick()
        self.assertEqual(db.list_runs(task_id=tid), [])
        # 页面写的是「明天」：校准发生在前一日，触发在目标日。
        self.now -= 86_400_000
        self.request("POST", f"/api/tasks/{tid}/calibrate")
        self.assertEqual(self.wait_run(tid)["outcome"], "calibrated")
        task = self.request("GET", f"/api/tasks/{tid}")
        self.assertEqual((task["status"], task["calibrated_at"], task["on_sale_at"]), ("ready", SALE, ""))
        self.assertEqual(task["calibration"]["page_date"], "2026-11-28 至 2026-11-29")
        self.assertEqual(task["calibration"]["page_price"], "¥488–¥888")
        self.assertFalse(task["calibration"]["mismatch"])
        self.now += 86_400_000
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 1)
        task = self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE})
        self.assertEqual(task["prewarm_at"], "2026-11-09 14:55:00")
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 1)

        self.now += 1
        self.state = "home"
        self.interrupt_at = self.now + 5000
        self.manager._tick()
        run = self.wait_run(tid)
        self.assertEqual(run["outcome"], "needs_human")
        self.assertEqual(self.recording_starts, [SALE_MS - 300000])
        self.assertEqual(len(self.evidence_times), 1)
        self.assertEqual(self.recording_stops, [self.evidence_times[0] + 60_000])
        video = self.request("GET", f"/api/runs/{run['id']}")["recording_url"]
        self.assertEqual(self.request("GET", video, expected=206, raw=True,
                                      headers=((b"range", b"bytes=0-3"),)), b"test")
        self.assertEqual(self.nav_times[1], SALE_MS - 300000)
        self.assertEqual(len(self.nav_times), 3)  # calibration, initial navigation, guard recovery
        self.assertEqual([t[0] for t in self.taps], [SALE_MS])
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 2)

    def test_calibration_reports_mismatch_and_blocks_formal_schedule(self):
        self.create_draft(session="", expected=400)
        self.create_draft(price_tiers=["票价待定"], expected=400)
        self.create_draft(price_tiers=["688", "788"], expected=400)
        tid = self.create_draft(session="2026-11-30", price_tiers=["988"])
        self.now -= 86_400_000
        self.request("POST", f"/api/tasks/{tid}/calibrate")
        self.assertEqual(self.wait_run(tid)["outcome"], "calibrated")
        result = self.request("GET", f"/api/tasks/{tid}")["calibration"]
        self.assertTrue(result["mismatch"])
        self.assertIn("预填日期与页面不符", result["date_check"])
        self.assertIn("¥988", result["price_check"])
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE}, 409)

    def test_calibration_uses_the_actual_reserved_date_and_price(self):
        tid = self.create_draft(session="2026-11-29", price_tiers=["888"])
        self.now -= 86_400_000
        with patch.object(runner, "read_reservation", return_value=("2026-11-28 19:30", "588")):
            self.request("POST", f"/api/tasks/{tid}/calibrate")
            self.assertEqual(self.wait_run(tid)["outcome"], "calibrated")
        task = self.request("GET", f"/api/tasks/{tid}")
        self.assertEqual((task["session"], task["price_tiers"]), ("2026-11-28 19:30", ["588"]))
        self.assertEqual(task["calibration"]["reservation_price"], "588")

    def test_confirm_uses_current_selection_without_reading_price_or_date(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"], session="2026-11-28")
        enabled = False
        dev = device_with_xml(lambda: node(children=node("确定", bounds="[50,450][100,550]")
            + node(rid="id_sku_bottom_bar_container", children=
                   node("确定", bounds="[600,2100][1000,2240]", enabled=enabled))))
        adb = self.adb()
        adb.screencap_raw = lambda: self.fail("可直接使用确定控件，不应截图核对")
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(), Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000
        self.assertIsNone(instance._from_tier_sheet())
        self.assertEqual(self.taps, [])
        enabled = True
        dev.refresh()
        clicked_at = self.now
        self.assertIsNone(instance._from_tier_sheet())
        self.assertEqual(self.taps, [(clicked_at, (800, 2170))])

    def test_order_retry_shares_cooldown_and_caps_submissions(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"])
        submit = node("立即提交", bounds="[700,2100][1000,2240]")
        retry = node("继续尝试", bounds="[500,1200][800,1320]") + node("返回重新选购")
        dev = device_with_xml(lambda: submit + node(clazz="android.widget.CheckBox", checked=True)
                              + (retry if self.state == "retry" else ""))
        messages = []
        adb = self.adb()
        def tap(x, y):
            self.taps.append((self.now - SALE_MS, (x, y)))
            self.state = "retry" if y == 2170 else "order"
        adb.tap = tap
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(critical=messages.append),
                                   Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 30000
        self.state = "order"
        def attempt(offset):
            self.now = SALE_MS + offset
            dev.refresh(fast=True)
            return instance._attempt()
        for offset in (0, 799, 800, 1599, 1600, 2400, 3200, 4000, 4800):
            self.assertIsNone(attempt(offset))
        self.assertEqual(attempt(5600), grabber.GrabOutcome.NEEDS_HUMAN)
        self.assertEqual(self.taps, [(i * 800, (850, 2170) if i % 2 == 0 else (650, 1260))
                                     for i in range(7)])
        self.assertIn("已提交 4 次", messages[0])

    def test_timed_rehearsal_does_not_click_or_requeue(self):
        tid = self.create_draft()
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE, "dry_run": True})
        self.now += 1
        self.manager._tick()
        run = self.wait_run(tid)
        self.assertEqual((run["outcome"], run["dry_run"]), ("guard_verified", 1))
        self.assertEqual(self.nav_times, [SALE_MS - 300000])
        self.assertEqual(self.taps, [])
        self.assertEqual(self.recording_starts, [])
        self.assertIsNone(self.request("GET", f"/api/runs/{run['id']}")["recording_url"])
        self.assertEqual(self.now, SALE_MS)
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 1)

    def test_probe_starts_before_sale_and_keeps_loading_page_open(self):
        self.now = SALE_MS - 3000
        self.state = "detail"
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        probe_times, backs = [], []
        adb = self.adb()
        adb.back = lambda: backs.append(self.now)
        def xml():
            if self.state == "detail":
                probe_times.append(self.now)
                self.advance(0.85)
                return self.xml()
            if self.now < self.taps[0][0] + 600:
                return ""
            return node(children=node("票档") + node("提交缺货登记"))
        dev = device_with_xml(xml)
        dev._d.dump_hierarchy = lambda: self.fail("热路径不应调用带秒级重试的读取")
        notifier = SimpleNamespace(critical=lambda _: None)
        instance = grabber.Grabber(adb, dev, task, notifier, Path(self.tmp.name),
                                   tier_reader=lambda *_: self.fail("不应解析缺货登记页"))
        self.assertEqual(instance.run(SALE_MS, verify_detail=True), "sold_out")
        self.assertEqual(probe_times, [SALE_MS - 750])
        self.assertEqual([tap[0] for tap in self.taps], [SALE_MS + 100])
        self.assertEqual(backs, [])

    def test_pre_sale_button_is_rechecked_before_clicking(self):
        self.now = SALE_MS - 750
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        def xml():
            button = "立即预订" if self.now < SALE_MS else "缺货登记"
            return node(children=node(f"{SHOW} · 杭州站", "info_v2_title")
                        + node(button, "purchase_status_bar"))
        dev = device_with_xml(xml)
        instance = grabber.Grabber(self.adb(), dev, task, SimpleNamespace(),
                                   Path(self.tmp.name), stop_check=lambda: self.now > SALE_MS + 300)
        self.assertEqual(instance.run(SALE_MS, verify_detail=True), "sold_out")
        self.assertEqual(self.taps, [])

    def test_opening_uses_ready_parallel_frame_without_another_capture(self):
        self.now = SALE_MS + 800
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        dev = device_with_xml(lambda: node(children=
            node(f"{SHOW} · 杭州站", "info_v2_title") +
            node("", "purchase_status_bar", "[320,2200][1050,2360]")))
        adb = self.adb()
        adb.screencap_raw = lambda: self.fail("不应再读一帧")
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(), Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000
        instance._sale_at_ms = SALE_MS
        instance._verify_detail = True
        instance._opening_probe = SimpleNamespace(
            result=lambda _: ((340, 2210, 1020, 2350), 800), close=lambda: None)
        self.assertIsNone(instance._attempt())
        self.assertEqual([tap[0] for tap in self.taps], [SALE_MS + 800])

    def test_calibration_never_overwrites_existing_device_owner(self):
        tid = self.create_draft()
        self.manager.device_busy["fake-phone"] = 712
        self.request("POST", f"/api/tasks/{tid}/calibrate", expected=409)
        self.assertEqual(self.manager.device_busy["fake-phone"], 712)
        self.assertEqual(db.list_runs(task_id=tid), [])

    def test_guard_stops_after_two_failed_navigations(self):
        tid = self.create_draft()
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE, "dry_run": True})
        self.now += 1
        with patch.object(navigator, "navigate_to_detail", return_value=False) as navigate:
            self.manager._tick()
            self.assertEqual(self.wait_run(tid)["outcome"], "needs_human")
            self.assertEqual(navigate.call_count, 2)
        self.assertEqual(self.taps, [])

    def test_due_offline_recovers_and_expired_task_is_not_started(self):
        tid = self.create_draft()
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE, "dry_run": True})
        self.now += 1
        with patch.object(self.manager, "scan_devices", return_value={}):
            self.manager._tick()
        self.assertEqual(db.get_task(tid)["status"], "waiting_device")
        self.now = SALE_MS + 90000
        self.manager._tick()
        self.assertEqual(db.get_task(tid)["status"], "failed")
        self.assertEqual(db.list_runs(task_id=tid), [])


if __name__ == "__main__":
    unittest.main()
