"""設定ファイル（%APPDATA%\\Sticky-Notion\\config.json）の読み書き。"""

import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from notiontask import APP_NAME, LEGACY_APP_NAME
from notiontask.secretstore import protect

DEFAULTS = {
    # 接続: [{"id", "label", "type": "mcp"|"token", "token", "oauth": {...}}]
    # token と oauth 内のトークンは secretstore で暗号化した文字列
    "connections": [],
    "hotkey": "Ctrl+Alt+N",
    "alpha": 1.0,
    "topmost": True,
    "open_after_create": False,
    "open_in_app": False,
    "geometry": "",
    # 前回選択した DB（"接続ID/data source ID"）
    "last_data_source": "",
    # ドロップダウンに出す DB: [{"conn", "id", "title", "title_prop"?}]
    "data_sources": [],
    "show_properties": False,
}


def app_dir():
    base = Path(os.environ.get("APPDATA") or Path.home())
    path = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    if not path.exists() and legacy.is_dir():
        # 旧名（NotionTask）時代の設定・キューを引き継ぐ
        try:
            legacy.rename(path)
        except OSError:
            shutil.copytree(legacy, path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_connection(label, conn_type):
    return {"id": uuid.uuid4().hex[:8], "label": label, "type": conn_type, "token": "", "oauth": {}}


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else app_dir() / "config.json"
        self.data = json.loads(json.dumps(DEFAULTS))
        # バックグラウンドスレッド（OAuth トークン更新など）からも保存するためのロック
        self.lock = threading.RLock()
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        self.data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
        self._migrate(loaded)

    def _migrate(self, loaded):
        """v1.0（単一トークン）の設定を接続 1 件に変換する。"""
        if loaded.get("token") and not self.data["connections"]:
            conn = new_connection("Notion", "token")
            conn["token"] = protect(loaded["token"])
            self.data["connections"].append(conn)
            for d in self.data["data_sources"]:
                d.setdefault("conn", conn["id"])
            if loaded.get("last_data_source_id"):
                self.data["last_data_source"] = f"{conn['id']}/{loaded['last_data_source_id']}"

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def connection(self, conn_id):
        return next((c for c in self.data["connections"] if c["id"] == conn_id), None)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
