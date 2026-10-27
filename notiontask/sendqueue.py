"""送信に失敗したページ作成リクエストを保存し、あとで再送するキュー。"""

import json
import os
import threading
import time
import uuid
from pathlib import Path

from notiontask.config import app_dir
from notiontask.notion_api import NotionError


class SendQueue:
    def __init__(self, path=None):
        self.path = Path(path) if path else app_dir() / "queue.json"
        self._lock = threading.Lock()
        self._items = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                items = json.load(f)
            return items if isinstance(items, list) else []
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, request):
        """request: ページ作成に必要な情報（接続方式に依存しない形）の dict。"""
        with self._lock:
            self._items.append({**request, "id": uuid.uuid4().hex, "queued_at": time.time(), "error": None})
            self._save()

    def counts(self):
        """(再送待ち件数, 再送不可エラー件数)"""
        with self._lock:
            errors = sum(1 for i in self._items if i.get("error"))
            return len(self._items) - errors, errors

    def clear_errors(self):
        with self._lock:
            self._items = [i for i in self._items if not i.get("error")]
            self._save()

    def flush(self, send):
        """再送待ちを順に send(item) で送り、送れた件数を返す。

        再送可能なエラー（通信エラー・要ログイン）が出た接続は今回は諦め、他の接続の分は続ける。
        """
        with self._lock:
            pending = [i for i in self._items if not i.get("error")]
        sent = 0
        skipped_conns = set()
        for item in pending:
            if item.get("conn") in skipped_conns:
                continue
            try:
                send(item)
            except NotionError as e:
                if e.retryable:
                    skipped_conns.add(item.get("conn"))
                    continue
                with self._lock:
                    item["error"] = str(e)
                    self._save()
                continue
            with self._lock:
                self._items = [i for i in self._items if i["id"] != item["id"]]
                self._save()
            sent += 1
        return sent
