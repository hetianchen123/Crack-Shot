"""大麦官方手机号密码登录。"""

import time

from .adb import Adb
from .device import Device
from .navigator import LoginState, _signed_in_profile, open_phone_login

_ACCOUNT = "cn.damai:id/aliuser_login_account_et"
_PASSWORD = "cn.damai:id/aliuser_login_password_et"
_SUBMIT = "cn.damai:id/aliuser_login_login_btn"
_CHALLENGES = ["请完成验证", "滑动验证", "滑块验证", "拖动滑块", "安全验证",
               "人机验证"]


class LoginError(RuntimeError):
    """可以在登录配置页显示的登录错误。"""


def _wait_for_page(adb: Adb, device: Device, seconds: int):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and adb.is_damai_foreground():
        device.refresh()
        if device.exists_contains_any(_CHALLENGES):
            raise LoginError("大麦要求安全验证，请在手机上完成")
        yield
        time.sleep(0.4)


def _open_password_form(adb: Adb, device: Device) -> None:
    clicked = set()
    for _ in _wait_for_page(adb, device, 10):
        account = device.find_by_id(_ACCOUNT)
        password = device.find_by_id(_PASSWORD)
        if account and password and account.enabled and password.enabled:
            return
        button = device.find(["换个账号登录"])
        if not button and password:
            button = device.find_by_id("cn.damai:id/aliuser_menu_item_more")
        if not button:
            button = device.find_by_id("cn.damai:id/aliuser_login_switch_pwdlogin")
        if button and button.enabled and button.resource_id not in clicked:
            adb.tap(*button.center)
            clicked.add(button.resource_id)
    raise LoginError("未找到可填写手机号的密码登录页")


def login_with_password(adb: Adb, device: Device, phone: str, password: str) -> bool:
    """成功时返回 True，已有登录会话时返回 False。"""
    if adb.is_damai_foreground():
        device.refresh()
        if device.exists_contains_any(_CHALLENGES):
            raise LoginError("大麦要求安全验证，请在手机上完成")
    if open_phone_login(adb, device, accept_agreement=True) == LoginState.SIGNED_IN:
        return False
    _open_password_form(adb, device)
    if not (device.set_field_text(_ACCOUNT, phone) and device.set_field_text(_PASSWORD, password)):
        raise LoginError("手机号或密码输入框填写失败")

    for _ in _wait_for_page(adb, device, 4):
        button = device.find(["登录"])
        if button and button.resource_id == _SUBMIT and button.enabled:
            adb.tap(*button.center)
            break
    else:
        raise LoginError("登录按钮未就绪，请在手机上检查表单")

    opened_mine = profile_loaded = False
    for _ in _wait_for_page(adb, device, 12):
        if device.find_by_id("cn.damai:id/aliuser_register_sms_code_et"):
            raise LoginError("大麦要求短信验证，请在手机上完成")
        if device.exists_contains_any(["密码错误", "密码不正确", "账号或密码", "登录失败",
                                       "操作频繁", "登录异常"]):
            raise LoginError("大麦未接受这次登录，请检查凭据或在手机上查看提示")
        signed_in = _signed_in_profile(device)
        if signed_in and profile_loaded:
            return True
        profile_loaded = signed_in
        if not signed_in and not opened_mine and not device.find_by_id("aliuser_main_content"):
            mine = device.find(["我的"])
            if mine and mine.enabled and device.find(["首页"]):
                adb.tap(*mine.center)
                opened_mine = True
    raise LoginError("未能确认登录成功，请在手机上查看大麦提示")
