"""SQLite 存储：账号 / 设备 / 任务 / 运行记录 / 事件 / 设置。

单文件 + WAL，一个共享连接配互斥锁，事务都很短。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import TaskConfig

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL UNIQUE,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  model TEXT DEFAULT '',
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  enabled INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  show_name TEXT NOT NULL,
  on_sale_at TEXT NOT NULL,            -- YYYY-MM-DD HH:MM（北京时间）
  price_tiers TEXT NOT NULL,           -- JSON 数组
  options TEXT DEFAULT '{}',           -- JSON：prewarm/lead/retry 等可调参数
  device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
  status TEXT DEFAULT 'scheduled',     -- scheduled/prewarming/grabbing/success/failed/needs_human/waiting_device/conflict/cancelled
  enabled INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT DEFAULT '',             -- success/needs_human/timeout/off_page/cancelled/error
  attempts INTEGER DEFAULT 0,
  dry_run INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  run_id INTEGER,
  ts TEXT NOT NULL,
  level TEXT DEFAULT 'info',
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id);
"""

DEFAULT_SETTINGS: Dict[str, str] = {
    "clock_offset_ms": "0",
    "ollama_url": "http://127.0.0.1:11434",
    "vlm_model": "",            # 空=自动从可用模型里挑视觉模型
    "default_prewarm_minutes": "5",
}


def init(db_path: Path) -> None:
    global _CONN
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _CONN = sqlite3.connect(str(db_path), check_same_thread=False)
    _CONN.row_factory = sqlite3.Row
    _CONN.execute("PRAGMA journal_mode=WAL")
    _CONN.execute("PRAGMA foreign_keys=ON")
    with _LOCK, _CONN:
        _CONN.executescript(_SCHEMA)
        _migrate(_CONN)
        for key, value in DEFAULT_SETTINGS.items():
            _CONN.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (key, value)
            )


def _migrate(conn: sqlite3.Connection) -> None:
    """存量库补列（share_url/city/session 2026-11 新增）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    for column in ("share_url", "city", "session"):
        if column not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT DEFAULT ''")
    for column, declaration in (
        ("calibrated_at", "TEXT DEFAULT ''"),
        ("scheduled_dry_run", "INTEGER DEFAULT 0"),
    ):
        if column not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {declaration}")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def query_all(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    with _LOCK:
        return _CONN.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    with _LOCK:
        return _CONN.execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    with _LOCK, _CONN:
        cur = _CONN.execute(sql, params)
        return cur.lastrowid


def rowdict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row else None


# ---- 设置 ------------------------------------------------------------


def get_settings() -> Dict[str, str]:
    rows = query_all("SELECT key, value FROM settings")
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---- 账号 ------------------------------------------------------------


def create_account(label: str, note: str = "") -> int:
    return execute(
        "INSERT INTO accounts(label, note, created_at) VALUES(?,?,?)",
        (label.strip(), note, _now()),
    )


def update_account(account_id: int, label: str = None, note: str = None) -> None:
    if label is not None:
        execute("UPDATE accounts SET label=? WHERE id=?", (label.strip(), account_id))
    if note is not None:
        execute("UPDATE accounts SET note=? WHERE id=?", (note, account_id))


def delete_account(account_id: int) -> None:
    execute("DELETE FROM accounts WHERE id=?", (account_id,))


def list_accounts() -> List[Dict[str, Any]]:
    return [dict(r) for r in query_all("SELECT * FROM accounts ORDER BY id")]


# ---- 设备 ------------------------------------------------------------


def upsert_device(serial: str, model: str = "", account_id: Optional[int] = None,
                  note: str = "") -> int:
    row = query_one("SELECT id FROM devices WHERE serial=?", (serial,))
    if row:
        execute("UPDATE devices SET model=? WHERE id=?", (model or row["model"] or "", row["id"]))
        return row["id"]
    return execute(
        "INSERT INTO devices(serial, model, account_id, note, created_at) VALUES(?,?,?,?,?)",
        (serial, model, account_id, note, _now()),
    )


def update_device(device_id: int, account_id: Optional[int] = None,
                  enabled: Optional[bool] = None, note: str = None) -> None:
    if account_id is not None:
        execute("UPDATE devices SET account_id=? WHERE id=?", (account_id, device_id))
    if enabled is not None:
        execute("UPDATE devices SET enabled=? WHERE id=?", (1 if enabled else 0, device_id))
    if note is not None:
        execute("UPDATE devices SET note=? WHERE id=?", (note, device_id))


def delete_device(device_id: int) -> None:
    execute("DELETE FROM devices WHERE id=?", (device_id,))


def list_devices() -> List[Dict[str, Any]]:
    return [dict(r) for r in query_all(
        "SELECT d.*, a.label AS account_label FROM devices d "
        "LEFT JOIN accounts a ON a.id=d.account_id ORDER BY d.id"
    )]


def get_device(device_id: int) -> Optional[Dict[str, Any]]:
    return rowdict(query_one(
        "SELECT d.*, a.label AS account_label FROM devices d "
        "LEFT JOIN accounts a ON a.id=d.account_id WHERE d.id=?", (device_id,)
    ))


# ---- 任务 ------------------------------------------------------------


def create_task(name: str, show_name: str, on_sale_at: str, price_tiers: List[str],
                device_id: Optional[int], options: Optional[Dict[str, Any]] = None,
                note: str = "", share_url: str = "", city: str = "",
                session: str = "", enabled: bool = True) -> int:
    return execute(
        "INSERT INTO tasks(name, show_name, on_sale_at, price_tiers, options, device_id,"
        " status, enabled, note, created_at, updated_at, share_url, city, session)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name.strip(), show_name.strip(), on_sale_at.strip(), json.dumps(price_tiers),
         json.dumps(options or {}), device_id, "draft", int(enabled), note, _now(), _now(),
         share_url.strip(), city.strip(), session.strip()),
    )


def update_task(task_id: int, fields: Dict[str, Any]) -> None:
    allowed = {"name", "show_name", "on_sale_at", "price_tiers", "options", "device_id",
               "enabled", "note", "share_url", "city", "session", "status",
               "calibrated_at", "scheduled_dry_run"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("price_tiers", "options"):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(_now())
    params.append(task_id)
    execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))


def delete_task(task_id: int) -> None:
    execute("DELETE FROM tasks WHERE id=?", (task_id,))


def set_task_status(task_id: int, status: str) -> None:
    execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, _now(), task_id))


def list_tasks() -> List[Dict[str, Any]]:
    rows = query_all(
        "SELECT t.*, d.serial AS device_serial, d.model AS device_model, a.label AS account_label,"
        " (SELECT COUNT(*) FROM runs r WHERE r.task_id=t.id) AS run_count,"
        " (SELECT outcome FROM runs r WHERE r.task_id=t.id ORDER BY id DESC LIMIT 1) AS last_outcome"
        " FROM tasks t LEFT JOIN devices d ON d.id=t.device_id"
        " LEFT JOIN accounts a ON a.id=d.account_id ORDER BY t.id DESC"
    )
    tasks = []
    for r in rows:
        t = dict(r)
        t["price_tiers"] = json.loads(t["price_tiers"])
        t["options"] = json.loads(t["options"] or "{}")
        t["enabled"] = bool(t["enabled"])
        tasks.append(t)
    return tasks


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        return None
    t = dict(row)
    t["price_tiers"] = json.loads(t["price_tiers"])
    t["options"] = json.loads(t["options"] or "{}")
    t["enabled"] = bool(t["enabled"])
    return t


def task_config_from_row(row: Dict[str, Any], settings=None) -> TaskConfig:
    """DB 任务行 → 抢票状态机用的 TaskConfig（缺省值与 YAML 版一致）。"""
    opts = row["options"] or {}
    fields: Dict[str, Any] = {
        "name": row["name"],
        "show_name": row["show_name"],
        "on_sale_at": row["on_sale_at"] or row.get("calibrated_at") or _now(),
        "prewarm_minutes": int((settings or {}).get("default_prewarm_minutes", "5")),
        "price_tiers": row["price_tiers"],
        "share_url": row.get("share_url", "") or "",
        "city": row.get("city", "") or "",
        "session": row.get("session", "") or "",
    }
    for key in ("prewarm_minutes", "arm_lead_seconds", "max_retry_seconds",
                "max_attempts", "queue_wait_seconds", "post_submit_watch_seconds"):
        if key in opts:
            fields[key] = int(opts[key])
    for key in ("retry_backoff_ms", "poll_interval_ms", "confirm_wait_ms"):
        if key in opts and isinstance(opts[key], list) and len(opts[key]) == 2:
            fields[key] = (int(opts[key][0]), int(opts[key][1]))
    for key in ("buy_texts", "wait_texts", "captcha_texts", "error_texts",
                "error_button_texts", "blind_tap_texts"):
        if key in opts and isinstance(opts[key], list):
            fields[key] = [str(v) for v in opts[key]]
    if "blind_tap" in opts:
        fields["blind_tap"] = bool(opts["blind_tap"])
    cfg = TaskConfig(**fields)
    errors = cfg.validate()
    if errors:
        raise ValueError("; ".join(errors))
    return cfg


# ---- 运行与事件 -------------------------------------------------------


def create_run(task_id: int, dry_run: bool = False) -> int:
    return execute(
        "INSERT INTO runs(task_id, started_at, outcome, dry_run) VALUES(?,?,?,?)",
        (task_id, _now(), "", 1 if dry_run else 0),
    )


def finish_run(run_id: int, outcome: str, attempts: int) -> None:
    execute("UPDATE runs SET finished_at=?, outcome=?, attempts=? WHERE id=?",
            (_now(), outcome, attempts, run_id))


def list_runs(limit: int = 100, task_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if task_id:
        rows = query_all(
            "SELECT r.*, t.name AS task_name FROM runs r JOIN tasks t ON t.id=r.task_id"
            " WHERE r.task_id=? ORDER BY r.id DESC LIMIT ?", (task_id, limit))
    else:
        rows = query_all(
            "SELECT r.*, t.name AS task_name FROM runs r JOIN tasks t ON t.id=r.task_id"
            " ORDER BY r.id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def add_event(task_id: Optional[int], run_id: Optional[int], level: str, message: str) -> None:
    execute("INSERT INTO events(task_id, run_id, ts, level, message) VALUES(?,?,?,?,?)",
            (task_id, run_id, _now(), level, message[:500]))


def list_events(run_id: Optional[int] = None, task_id: Optional[int] = None,
                limit: int = 200) -> List[Dict[str, Any]]:
    if run_id:
        rows = query_all("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                         (run_id, limit))
    elif task_id:
        rows = query_all("SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                         (task_id, limit))
    else:
        rows = query_all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
