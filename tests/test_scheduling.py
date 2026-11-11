"""定时流程集成测试：真实 API / SQLite / 调度与执行线程；替换手机和时钟。"""
import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from crackshot import grabber, login, navigator, scheduler
from crackshot.login import LoginError, login_with_password
from crackshot.device import Device, _parse_nodes
from crackshot.config import TaskConfig
from crackshot.timeutil import parse_local
from crackshot.web import app as web_app, db, runner
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
        other_stop = device_with_xml(lambda: node(f"宁波 · {SHOW}", "info_v2_title_tv1")
                                    + node(rid="purchase_status_bar")
                                    + node("杭州站 今天15:00开抢", "count_sell_time"))
        self.assertFalse(navigator.is_target_detail(other_stop, self.task))

    def test_real_name_notice_closes_only_the_acknowledgement(self):
        dev = device_with_xml(lambda: node(children=
            node("实名制观演") + node("预选实名观演人", bounds="[100,1300][900,1400]")
            + node("知道了", bounds="[100,1420][900,1500]")))
        taps = []
        self.assertEqual(recognize(dev, self.task).state, PageState.TICKET_NOTICE)
        self.assertTrue(navigator.dismiss_ticket_notice(dev, SimpleNamespace(tap=lambda *xy: taps.append(xy))))
        self.assertEqual(taps, [(500, 1460)])

    def test_search_switches_from_mine_to_verified_home(self):
        scenes = [
            node("我的订单", "tv_mine_order") + node("首页", "cn.damai:id/bottom_tab_item_tv")
            + node(rid="pioneer_home_tab_container_1", bounds="[0,2200][220,2400]"),
            node("演唱会", "bricks_ball_name")
            + node(rid="cn.damai:id/homepage_header_search", bounds="[400,100][850,220]"),
            node("搜索内容", "header_search_v2_input", clazz="android.widget.EditText"),
        ]
        taps = []
        dev = device_with_xml(lambda: scenes[len(taps)])
        adb = SimpleNamespace(is_damai_foreground=lambda: True, tap=lambda *xy: taps.append(xy))
        with patch.object(navigator.time, "sleep", return_value=None), \
             patch.object(navigator, "sleep_jitter", return_value=None):
            self.assertFalse(navigator.open_search(dev, adb))
            self.assertTrue(navigator._ensure_home(adb, dev))
            self.assertTrue(navigator.open_search(dev, adb))
        self.assertEqual(taps, [(110, 2300), (625, 160)])

    def test_preheat_exits_old_retry_dialog_before_returning_home(self):
        scenes = [
            node("确认购买") + node("抢票人数太多啦，继续尝试别放弃")
            + node("继续尝试", "damai_theme_dialog_confirm_btn", "[195,1429][981,1549]")
            + node("返回重新选购", "damai_theme_dialog_cancel_btn", "[363,1594][813,1660]"),
            node("上一场演出", "info_v2_title") + node(rid="purchase_status_bar"),
            node("演唱会", "bricks_ball_name"),
        ]
        actions = []
        dev = device_with_xml(lambda: scenes[len(actions)])
        adb = SimpleNamespace(is_damai_foreground=lambda: True,
                              tap=lambda *xy: actions.append(xy),
                              back=lambda: actions.append("back"))
        with patch.object(navigator.time, "sleep", return_value=None), \
             patch.object(navigator, "sleep_jitter", return_value=None):
            self.assertTrue(navigator._ensure_home(adb, dev, task=self.task))
        self.assertEqual(actions, [(588, 1627), "back"])

    def test_search_and_detail_match_official_title_with_quotes(self):
        task = TaskConfig("test", "张杰1982开往未·LIVE", SALE, ["688"], city="南京")
        official = "张杰1982开往「未·LIVE」巡回演唱会"
        search = device_with_xml(lambda: node("搜索内容", "header_search_v2_input",
                                               clazz="android.widget.EditText")
                                 + node(official, "tv_word", "[100,240][1000,390]"))
        fields, taps = [], []
        search.set_field_text = lambda field, value: fields.append((field, value)) or True
        adb = SimpleNamespace(tap=lambda *xy: taps.append(xy), back=lambda: None)
        with patch.object(navigator, "sleep_jitter", return_value=None):
            self.assertTrue(navigator.submit_search(search, adb, task))
        self.assertEqual(fields, [("header_search_v2_input", task.show_name)])
        self.assertEqual(taps, [(550, 315)])

        for city, expected in (("南京", True), ("杭州", False)):
            detail = device_with_xml(lambda: node(f"【{city}】{official}", "info_v2_title")
                                     + node(rid="purchase_status_bar"))
            self.assertEqual(navigator.is_target_detail(detail, task), expected)

    def test_phone_login_selects_account_entry_and_stops_at_agreement(self):
        scenes = [
            node("演唱会", "bricks_ball_name") + node("我的", bounds="[900,2250][1080,2400]"),
            node("我的订单", "tv_mine_order") + node("立即登录", bounds="[50,250][400,400]")
            + node("登录后查看可用票", "mine_ticket_login_tip"),
            node(rid="aliuser_main_content", children=node("手机淘宝登录", "login_third_tb_btn")
                 + node(rid="login_third_account_btn", bounds="[90,2100][290,2250]")),
            node("请您阅读并同意以下协议") + node("同意", "damai_dialog_confirm_btn"),
        ]
        taps = []
        dev = device_with_xml(lambda: scenes[len(taps)])
        adb = SimpleNamespace(is_damai_foreground=lambda: True, tap=lambda *xy: taps.append(xy))
        with patch.object(navigator.time, "sleep", return_value=None):
            self.assertEqual(navigator.open_phone_login(adb, dev),
                             navigator.LoginState.CONSENT_REQUIRED)
        self.assertEqual(taps, [(990, 2325), (225, 325), (190, 2175)])

    def test_phone_login_preserves_existing_session(self):
        dev = device_with_xml(lambda: node("我的订单", "tv_mine_order"))
        adb = SimpleNamespace(is_damai_foreground=lambda: True)
        with patch.object(navigator.time, "sleep", return_value=None):
            self.assertEqual(navigator.open_phone_login(adb, dev), navigator.LoginState.SIGNED_IN)

    def test_login_check_recognizes_more_methods_page_without_tapping(self):
        dev = device_with_xml(lambda: node("更多登录方式", "login_more_title")
                              + node("手机号登录", "login_third_account_btn"))
        adb = SimpleNamespace(is_damai_foreground=lambda: True,
                              tap=lambda *_: self.fail("仅检查时不应点击登录"))
        self.assertEqual(navigator.open_phone_login(adb, dev, check_only=True),
                         navigator.LoginState.SIGNED_OUT)

    def test_password_login_fills_configured_phone_after_switching_saved_account(self):
        def control(name, label="", **kwargs):
            return node(label, f"cn.damai:id/{name}", **kwargs)

        consent = node("请您阅读并同意以下协议") + node("同意", "damai_dialog_confirm_btn")
        account = "aliuser_login_account_et"
        password = "aliuser_login_password_et"
        stages = [
            consent,
            control("aliuser_register_sms_code_et", "请输入验证码")
            + control("aliuser_login_switch_pwdlogin", "账号密码登录"),
            control("aliuser_login_account_tv") + control(password)
            + control("aliuser_menu_item_more"),
            control("aliuser_menu_item", "换个账号登录"),
            control(account) + control(password) + control("aliuser_login_login_btn", "登录"),
        ]
        for verified in (True, False):
            with self.subTest(verified=verified):
                last = node("我的订单", "tv_mine_order") if verified else node("请完成滑块验证")
                taps, fields = [], []
                dev = device_with_xml(lambda: (stages + [last])[len(taps)])
                dev.set_field_text = lambda field, value: fields.append((field, value)) or True
                adb = SimpleNamespace(is_damai_foreground=lambda: True,
                                      tap=lambda *xy: taps.append(xy))
                with patch.object(navigator.time, "sleep", return_value=None):
                    if verified:
                        self.assertTrue(login_with_password(adb, dev, "10000000000", "fixture"))
                    else:
                        with self.assertRaisesRegex(LoginError, "安全验证"):
                            login_with_password(adb, dev, "10000000000", "fixture")
                self.assertEqual(fields, [(f"cn.damai:id/{account}", "10000000000"),
                                          (f"cn.damai:id/{password}", "fixture")])
                self.assertEqual(len(taps), len(stages))

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
            patch.object(scheduler, "ensure_logged_in", return_value=False),
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
                         if self.now < SALE_MS else "")
            purchase = node("已预约" if countdown else "立即预订",
                            "purchase_status_bar", "[600,2000][1000,2250]")
            return node(children=node(f"{SHOW} · 杭州站", "info_v2_title", "[10,50][900,200]")
                        + countdown + purchase + node("2026.11.28-11.29 约120分钟", "info_v2_time_and_duration_tv")
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
                            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8780),
                            "query_string": b"", "scheme": "http", "root_path": "",
                            "http_version": "1.1"}, receive, send)
            payload = b"".join(m.get("body", b"") for m in messages)
            if not raw:
                payload = json.loads(payload)
            self.assertEqual(messages[0]["status"], expected, payload)
            return payload
        return asyncio.run(call())

    def test_device_login_is_local_and_uses_credentials_only_on_the_selected_device(self):
        path = f"/api/devices/{self.device_id}/login"
        saved = {}

        def save(_store, device, phone, password):
            saved[device["serial"]] = (phone, password)

        def read(_store, device):
            return saved.get(device["serial"])

        def delete(_store, device):
            saved.pop(device["serial"], None)

        def perform(adb, _device, phone, password):
            with self.assertRaises(ValueError):
                with self.manager.reserve_device(self.device_id):
                    self.fail("登录时不应能再次占用同一设备")
            return (adb.serial, phone, password) == ("fake-phone", "10000000000", "fixture")

        with patch.object(web_app.DeviceCredentials, "save", save), \
             patch.object(web_app.DeviceCredentials, "read", read), \
             patch.object(web_app.DeviceCredentials, "delete", delete), \
             patch.object(web_app, "find_adb", return_value="fake-adb"), \
             patch.object(web_app, "Adb") as adb, \
             patch.object(web_app, "Device"), \
             patch.object(web_app, "login_with_password", side_effect=perform) as login:
            adb.list_devices.return_value = [{"id": "fake-phone", "state": "device"}]
            adb.return_value.serial = "fake-phone"
            self.request("PUT", path, {"phone": "10000000000", "password": "fixture"}, 403,
                         headers=((b"origin", b"https://other.example"),))
            self.assertEqual(saved, {})
            self.request("PUT", path, {"phone": "10000000000", "password": "fixture"})
            self.assertEqual(self.request("GET", path), {"configured": True, "phone_tail": "0000"})
            self.assertEqual(self.request("POST", path), {"status": "signed_in"})
            adb.assert_called_once_with("fake-adb", "fake-phone")
            login.assert_called_once()
            self.assertEqual(self.manager.device_busy, {})
            self.request("DELETE", path)
            self.assertEqual(self.request("GET", path), {"configured": False, "phone_tail": ""})

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
        self.request("POST", f"/api/tasks/{tid}/schedule",
                     {"on_sale_at": "2026-11-09 15:01"}, 409)
        db.set_setting("clock_offset_ms", "125")
        task = self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE})
        self.assertEqual(task["prewarm_at"], "2026-11-09 14:55:00")
        self.assertEqual(task["trigger_at_ms"], SALE_MS + 125)
        with patch.object(web_app.extractor, "status", return_value={}):
            overview = self.request("GET", "/api/overview")
        self.assertEqual(overview["server_now_ms"], self.now)
        self.assertEqual(overview["next_up"][0]["trigger_at_ms"], SALE_MS + 125)
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 1)

        self.now += 126
        self.state = "home"
        self.interrupt_at = self.now + 5000
        active_countdown = []
        emit_phase = scheduler.TaskRunner._phase

        def inspect_phase(task_runner, name):
            emit_phase(task_runner, name)
            db.set_setting("clock_offset_ms", "500")
            active_countdown.extend(self.request("GET", "/api/overview")["next_up"])

        with patch.object(scheduler.TaskRunner, "_phase", inspect_phase), \
             patch.object(web_app.extractor, "status", return_value={}):
            self.manager._tick()
            run = self.wait_run(tid)
        self.assertEqual([(item["status"], item["trigger_at_ms"]) for item in active_countdown],
                         [("grabbing", SALE_MS + 125)])
        self.assertEqual(run["outcome"], "needs_human")
        self.assertEqual(self.recording_starts, [SALE_MS - 300000 + 125])
        self.assertEqual(len(self.evidence_times), 1)
        self.assertEqual(self.recording_stops, [self.evidence_times[0] + 60_000])
        video = self.request("GET", f"/api/runs/{run['id']}")["recording_url"]
        self.assertEqual(self.request("GET", video, expected=206, raw=True,
                                      headers=((b"range", b"bytes=0-3"),)), b"test")
        self.assertEqual(self.nav_times[1], SALE_MS - 300000 + 125)
        self.assertEqual(len(self.nav_times), 3)  # calibration, initial navigation, guard recovery
        self.assertEqual(self.taps, [(SALE_MS + 125, (800, 2125))])
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

    def test_confirm_uses_reservation_without_reading_cards_or_total(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"], session="2026-11-28")
        enabled = False
        overlay = False
        dev = device_with_xml(lambda: node(children=node("确定", bounds="[50,450][100,550]")
            + node(rid="id_sku_bottom_bar_container", children=
                   node(rid="btn_buy_view", bounds="[600,2100][1000,2240]", enabled=enabled)
                   + (node(rid="view_translucent") if overlay else ""))))
        adb = self.adb()
        adb.screencap_raw = dev.fast_screenshot = lambda: self.fail("应直接使用确定控件")
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(), Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000
        self.assertIsNone(instance._from_tier_sheet())
        self.assertEqual(self.taps, [])
        enabled = True
        overlay = True
        dev.refresh()
        self.assertIsNone(instance._from_tier_sheet())
        self.assertEqual(self.taps, [])
        overlay = False
        dev.refresh()
        clicked_at = self.now
        self.assertIsNone(instance._from_tier_sheet())
        self.assertEqual(self.taps, [(clicked_at, (800, 2170))])

    def test_calibration_scopes_countdown_to_the_target_city(self):
        self.now = SALE_MS - 3600000
        wrong_city = node(rid="city_card", children=node("宁波站", "tour_city_name")
                          + node("今天16:00开抢", "count_sell_time"))
        local = node("今天 19:30 演出，今天 15:00:00 开抢", "count_sell_time")
        dev = device_with_xml(lambda: node(rid="purchase_status_bar") + wrong_city + local)
        self.assertEqual(scheduler.read_page_sell_time(dev, "杭州"), SALE_MS)
        dev = device_with_xml(lambda: node(rid="purchase_status_bar")
                             + wrong_city + node("宁波站 今天16:00开抢", "count_sell_time"))
        self.assertIsNone(scheduler.read_page_sell_time(dev, "杭州"))

    def test_order_retry_shares_cooldown_and_continues_until_deadline(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"], max_attempts=2)
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
        instance.deadline_ms = SALE_MS + 90000
        self.state = "order"
        def attempt(offset):
            self.now = SALE_MS + offset
            dev.refresh(fast=True)
            return instance._attempt()
        for offset in (0, 799, 800, 1599):
            self.assertIsNone(attempt(offset))
        self.assertEqual(instance._run_until_end(SALE_MS), grabber.GrabOutcome.TIMEOUT)
        self.assertGreater(instance._order_submissions, 4)
        self.assertGreater(instance.attempts, task.max_attempts)
        self.assertGreaterEqual(self.now, instance.deadline_ms)
        self.assertTrue(all(t < 90000 for t, _ in self.taps))
        self.assertTrue(all(b[0] - a[0] >= 800 for a, b in zip(self.taps, self.taps[1:])))
        self.assertEqual([point for _, point in self.taps],
                         [(850, 2170) if i % 2 == 0 else (650, 1260)
                          for i in range(len(self.taps))])
        self.assertEqual(messages, [])

    def test_order_refresh_repeats_with_cooldown_while_known_notice_stays_open(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"])
        enabled = False
        dev = device_with_xml(lambda: node("确认购买")
                              + node("抢票人数太多啦，继续尝试别放弃")
                              + node("努力刷新", bounds="[350,1650][730,1800]", enabled=enabled)
                              + node("立即提交", bounds="[700,2100][1000,2240]"))
        instance = grabber.Grabber(self.adb(), dev, task, SimpleNamespace(critical=lambda _: None),
                                   Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000

        self.assertIsNone(instance._attempt())
        self.assertEqual(self.taps, [])
        enabled = True
        dev.refresh()
        refresh_at = self.now
        self.assertIsNone(instance._attempt())
        self.now = refresh_at + 800
        self.assertIsNone(instance._attempt())
        self.now = refresh_at + 2000
        self.assertIsNone(instance._attempt())
        self.assertEqual(self.taps, [(refresh_at + delay, (540, 1725))
                                     for delay in (0, 800, 2000)])

    def test_retry_stops_at_unknown_page_without_tapping_or_returning(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"])
        dev = device_with_xml(lambda: node("继续尝试") + node("返回重新选购")
                             if self.state == "retry" else node("新页面提示"))
        adb = self.adb()
        adb.tap = lambda *p: self.taps.append((self.now, p))
        adb.back = lambda: self.fail("未知页面不应自动返回")
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(critical=lambda _: None),
                                   Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 90000
        self.state = "retry"
        self.assertIsNone(instance._attempt())
        self.state = "unknown"
        self.now += 2100
        dev.refresh(fast=True)
        self.assertEqual(instance._attempt(), grabber.GrabOutcome.NEEDS_HUMAN)
        self.assertEqual(len(self.taps), 1)
        self.assertEqual(len(self.evidence_times), 1)

    def test_order_failure_notices_stop_without_dismissing(self):
        self.now = SALE_MS
        task = TaskConfig("test", SHOW, SALE, ["688"])
        cases = [
            ("当前访问页面失效，可能您停留时间过长，请重新提交申请", "",
             PageState.ORDER_REVIEW, grabber.GrabOutcome.NEEDS_HUMAN),
            ("商品已经过期", "", PageState.ORDER_REVIEW, grabber.GrabOutcome.NEEDS_HUMAN),
            ("网络开小差了，再试一次吧～", "继续尝试",
             PageState.ORDER_REVIEW, grabber.GrabOutcome.NEEDS_HUMAN),
            ("亲，同一时间下单人数过多，建议您稍后再试", "",
             PageState.ORDER_REVIEW, grabber.GrabOutcome.NEEDS_HUMAN),
            ("对不起，您选购的商品库存不足，请重新选购", "",
             PageState.ORDER_SOLD_OUT, grabber.GrabOutcome.SOLD_OUT),
            ("请完成滑块验证", "继续尝试",
             PageState.CAPTCHA, grabber.GrabOutcome.NEEDS_HUMAN),
        ]
        for message, action, state, outcome in cases:
            with self.subTest(message=message):
                dev = device_with_xml(lambda: node(message) + node(action)
                                      + node("返回重新选购") + node("立即提交"))
                instance = grabber.Grabber(self.adb(), dev, task,
                                           SimpleNamespace(critical=lambda _: None),
                                           Path(self.tmp.name))
                instance.deadline_ms = SALE_MS + 10000
                self.assertEqual(recognize(dev, task).state, state)
                self.assertEqual(instance._attempt(), outcome)
                self.assertEqual(self.taps, [])

    def test_queue_congestion_popup_differs_from_active_queue(self):
        task = TaskConfig("test", SHOW, SALE, ["688"])
        message = "当前排队人数较多"
        modal = device_with_xml(lambda: node(message + "，实在挤不进去了")
                                + node("继续尝试") + node("返回重新选购"))
        queue = device_with_xml(lambda: node("确认购买") + node("正在排队，" + message))
        self.assertEqual(recognize(modal, task).state, PageState.ORDER_RETRY)
        self.assertEqual(recognize(queue, task).state, PageState.QUEUE)

    def test_timed_rehearsal_does_not_click_or_requeue(self):
        tid = self.create_draft()
        self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE, "dry_run": True})
        self.now += 1
        with self.assertLogs("crackshot.grabber", level="INFO") as messages:
            self.manager._tick()
            run = self.wait_run(tid)
        self.assertTrue(any("定时首击 (800, 2125)，距触发时刻 +0ms" in line
                            for line in messages.output))
        self.assertEqual((run["outcome"], run["dry_run"]), ("guard_verified", 1))
        self.assertEqual(self.nav_times, [SALE_MS - 300000])
        self.assertEqual(self.taps, [])
        self.assertEqual(self.recording_starts, [])
        self.assertIsNone(self.request("GET", f"/api/runs/{run['id']}")["recording_url"])
        self.assertEqual(self.now, SALE_MS)
        self.manager._tick()
        self.assertEqual(len(db.list_runs(task_id=tid)), 1)

    def test_scheduled_preheat_checks_login_before_navigation(self):
        cases = [
            (navigator.LoginState.SIGNED_IN, None, None, "guard_verified"),
            (navigator.LoginState.SIGNED_OUT, ("10000000000", "fixture"), None, "guard_verified"),
            (navigator.LoginState.SIGNED_OUT, None, None, "needs_human"),
            (navigator.LoginState.SIGNED_OUT, ("10000000000", "fixture"),
             LoginError("大麦要求短信验证"), "needs_human"),
        ]
        for state, saved, error, expected in cases:
            with self.subTest(state=state, credentials_configured=bool(saved), outcome=expected):
                self.now = SALE_MS - 300001
                self.state = "home"
                self.nav_times.clear()
                tid = self.create_draft()
                self.request("POST", f"/api/tasks/{tid}/schedule", {"on_sale_at": SALE, "dry_run": True})
                self.now += 1

                def auto_login(*_args, **_kwargs):
                    self.assertEqual(self.nav_times, [])
                    self.assertIn("fake-phone", self.manager.device_busy)
                    if error:
                        raise error
                    return True

                with patch.object(scheduler, "ensure_logged_in", login.ensure_logged_in), \
                     patch.object(login, "open_phone_login", return_value=state) as check, \
                     patch.object(self.manager.credentials, "read", return_value=saved) as read, \
                     patch.object(login, "login_with_password", side_effect=auto_login) as perform:
                    self.manager._tick()
                    result = self.wait_run(tid)
                self.assertEqual(result["outcome"], expected)
                self.assertTrue(check.call_args.kwargs["check_only"])
                if state == navigator.LoginState.SIGNED_IN:
                    read.assert_not_called()
                else:
                    self.assertEqual(read.call_args.args[0]["id"], self.device_id)
                    self.assertEqual(read.call_args.args[0]["serial"], "fake-phone")
                self.assertEqual(perform.call_count, int(saved is not None))
                self.assertEqual(self.nav_times, [SALE_MS - 300000] if expected == "guard_verified" else [])
                self.assertEqual(self.taps, [])

    def test_probe_starts_before_sale_and_keeps_loading_page_open(self):
        self.now = SALE_MS - 3000
        self.state = "detail"
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州", blind_tap=False)
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
        self.assertEqual(probe_times, [SALE_MS - 750, SALE_MS + 100])
        self.assertEqual([tap[0] for tap in self.taps], [SALE_MS + 950])
        self.assertEqual(backs, [])

    def test_pre_sale_button_is_rechecked_before_clicking(self):
        self.now = SALE_MS - 750
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州", blind_tap=False)
        def xml():
            button = "立即预订" if self.now < SALE_MS else "缺货登记"
            return node(children=node(f"{SHOW} · 杭州站", "info_v2_title")
                        + node(button, "purchase_status_bar"))
        dev = device_with_xml(xml)
        instance = grabber.Grabber(self.adb(), dev, task, SimpleNamespace(),
                                   Path(self.tmp.name), stop_check=lambda: self.now > SALE_MS + 300)
        self.assertEqual(instance.run(SALE_MS, verify_detail=True), "sold_out")
        self.assertEqual(self.taps, [])

    def test_first_click_returns_from_early_reservation_then_verifies_and_completes_order(self):
        self.now = SALE_MS - 2000
        self.state = "detail"
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州", session="2026-11-28 19:30")
        reads, backs = [], []

        def xml():
            reads.append(self.now)
            if self.state == "detail":
                if self.now < SALE_MS + 400:
                    return (node(f"{SHOW} · 杭州站", "info_v2_title")
                            + node("今天15:00开抢", "count_sell_time")
                            + node("已预约", "purchase_status_bar", "[600,2000][1000,2250]"))
                return self.xml()
            return {
                "reserve": node("预约想看票档"),
                "tier": node("票档", "tv_price_name") + node(rid="id_sku_bottom_bar_container",
                             children=node(rid="btn_buy_view", bounds="[600,2100][1000,2240]")),
                "order": node("立即提交", bounds="[700,2100][1000,2240]")
                         + node(clazz="android.widget.CheckBox", checked=True),
                "success": node("立即支付"),
            }[self.state]

        adb = self.adb()
        def tap(*point):
            self.taps.append((self.now, point))
            self.state = {"detail": "reserve" if len(self.taps) == 1 else "tier",
                          "tier": "order", "order": "success"}[self.state]
        def back():
            backs.append(self.now)
            self.state = "detail"
        adb.tap, adb.back = tap, back
        adb.screencap_raw = lambda: self.fail("定时首击与原生控件均不需要截图")
        instance = grabber.Grabber(adb, device_with_xml(xml), task,
                                   SimpleNamespace(success=lambda _: None), Path(self.tmp.name))
        self.assertEqual(instance.run(SALE_MS, verify_detail=True), "success")
        self.assertEqual(reads[:2], [SALE_MS - 1750, SALE_MS])
        self.assertEqual(backs, [SALE_MS])
        self.assertEqual(self.taps[0], (SALE_MS, (800, 2125)))
        self.assertGreaterEqual(self.taps[1][0], SALE_MS + 400)
        self.assertEqual([point for _, point in self.taps[2:]], [(800, 2170), (850, 2170)])

    def test_first_click_stops_on_late_page_change_or_cancellation(self):
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                self.now = SALE_MS - 2000
                self.state = "detail"
                self.interrupt_at = None if cancelled else SALE_MS - 1800
                instance = grabber.Grabber(self.adb(), device_with_xml(self.xml), task,
                                           SimpleNamespace(critical=lambda _: None), Path(self.tmp.name),
                                           stop_check=lambda: cancelled and self.now >= SALE_MS - 25)
                self.assertEqual(instance.run(SALE_MS, verify_detail=True),
                                 "cancelled" if cancelled else "needs_human")
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
            result=lambda _, **kw: ((340, 2210, 1020, 2350), 800), close=lambda: None)
        self.assertIsNone(instance._attempt())
        self.assertEqual([tap[0] for tap in self.taps], [SALE_MS + 800])

    def test_tier_confirm_does_not_wait_for_inflight_purchase_capture(self):
        self.now = SALE_MS + 315
        self.state = "detail"
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        dev = device_with_xml(lambda: (
            node(f"{SHOW} · 杭州站", "info_v2_title") + node(rid="purchase_status_bar", bounds="[320,2200][1050,2360]")
            if self.state == "detail" else node("票档", "tv_price_name")
            + node(rid="id_sku_bottom_bar_container", children=node(
                rid="btn_buy_view", bounds="[720,2230][1050,2360]"))))
        adb = self.adb()
        adb.tap = lambda *xy: self.taps.append(xy)
        entered, release = Event(), Event()
        def capture():
            entered.set()
            if not release.wait(2):
                self.fail("视觉线程阻塞了主链路")
            return (340, 2210, 1020, 2350), "异步原始帧", self.now, self.now
        instance = grabber.Grabber(adb, dev, task, SimpleNamespace(), Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000
        instance._sale_at_ms = SALE_MS
        instance._verify_detail = True
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(instance, "_locate_purchase_button", capture):
            instance._purchase_pool = pool
            try:
                self.assertIsNone(instance._attempt())
                self.assertTrue(entered.wait(.5))
                future = instance._purchase_scan[1]
                self.state = "tier"
                dev.refresh(fast=True)
                self.assertIsNone(instance._attempt())
                self.assertEqual(self.taps, [(885, 2295)])
                self.assertFalse(future.done())
                self.assertIsNone(instance._purchase_scan)
            finally:
                release.set()

    def test_visual_purchase_requires_new_detail_snapshot_and_matching_button_width(self):
        self.now = SALE_MS + 500
        task = TaskConfig("test", SHOW, SALE, ["688"], city="杭州")
        bounds = (320, 2200, 1050, 2360)
        dev = device_with_xml(lambda: node(f"{SHOW} · 杭州站", "info_v2_title")
                             + node(rid="purchase_status_bar", bounds="[320,2200][1050,2360]"))
        instance = grabber.Grabber(self.adb(), dev, task, SimpleNamespace(), Path(self.tmp.name))
        instance.deadline_ms = SALE_MS + 10000
        instance._sale_at_ms = SALE_MS
        instance._verify_detail = True
        narrow = Future()
        narrow.set_result(((720, 2220, 1040, 2350), "异步原始帧", SALE_MS, self.now))
        instance._purchase_scan = (bounds, narrow)
        self.assertIsNone(instance._attempt())
        self.assertEqual(self.taps, [])
        wide = Future()
        wide.set_result(((340, 2210, 1020, 2350), "异步原始帧", SALE_MS, self.now))
        instance._purchase_scan = (bounds, wide)
        self.assertIsNone(instance._from_detail(self.now - 1))
        self.assertEqual(self.taps, [])
        dev.refresh(fast=True)
        self.assertIsNone(instance._attempt())
        self.assertEqual(len(self.taps), 1)

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
