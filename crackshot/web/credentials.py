"""使用 macOS 钥匙串保存每台设备的登录凭据。"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Optional


class CredentialError(RuntimeError):
    pass


class DeviceCredentials:
    def __init__(self, db_path: Path):
        namespace = hashlib.sha256(str(db_path.resolve()).encode()).hexdigest()
        self.service = f"Crack-shot Damai Login {namespace}"

    @staticmethod
    def _keyring():
        if sys.platform != "darwin":
            raise CredentialError("大麦登录配置需要 macOS 钥匙串")
        try:
            from keyring.backends.macOS import Keyring
            return Keyring()
        except ImportError:
            raise CredentialError("请安装项目依赖以使用 macOS 钥匙串") from None

    @staticmethod
    def _account(device: dict) -> str:
        return f"{device['id']}:{device['serial']}"

    def read(self, device: dict) -> Optional[tuple[str, str]]:
        keyring = self._keyring()
        try:
            value = keyring.get_password(self.service, self._account(device))
            if value is None:
                return None
            phone, password = json.loads(value)
            if not isinstance(phone, str) or not isinstance(password, str):
                raise ValueError("invalid credential types")
            return phone, password
        except Exception:
            raise CredentialError("读取钥匙串失败，请检查 macOS 钥匙串权限") from None

    def save(self, device: dict, phone: str, password: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(self.service, self._account(device), json.dumps([phone, password]))
        except Exception:
            raise CredentialError("保存到钥匙串失败，请检查 macOS 钥匙串权限") from None

    def delete(self, device: dict) -> None:
        keyring = self._keyring()
        try:
            account = self._account(device)
            if keyring.get_password(self.service, account) is not None:
                keyring.delete_password(self.service, account)
        except Exception:
            raise CredentialError("清除钥匙串凭据失败，请检查 macOS 钥匙串权限") from None
