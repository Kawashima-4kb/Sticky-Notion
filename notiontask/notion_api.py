"""Notion API クライアント（標準 urllib のみ使用、API バージョン 2025-09-03）。"""

import json
import urllib.error
import urllib.request

from notiontask.net import USER_AGENT

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
MAX_CHILDREN = 100  # 1 リクエストで送れるブロック数の上限
TIMEOUT = 15


class NotionError(Exception):
    """API エラー。retryable が True なら時間をおいて再送すれば成功し得る。
    needs_login が True ならログインし直すまで送れない（キューには残す）。"""

    def __init__(self, message, status=None, retryable=False, needs_login=False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable or needs_login
        self.needs_login = needs_login


class NotionClient:
    def __init__(self, token, opener=None):
        self.token = token
        # テスト時に差し替えられるようにする
        self._open = opener or urllib.request.urlopen

    def _request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(API_BASE + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Notion-Version", NOTION_VERSION)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        try:
            with self._open(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("message", "")
            except (ValueError, OSError):
                detail = ""
            retryable = e.code == 429 or e.code >= 500
            raise NotionError(f"HTTP {e.code}: {detail or e.reason}", e.code, retryable) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NotionError(f"通信エラー: {getattr(e, 'reason', e)}", None, True) from e

    def list_data_sources(self):
        """Integration に共有されている data source 一覧を [{"id", "title"}] で返す。"""
        results = []
        cursor = None
        while True:
            body = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = self._request("POST", "/search", body)
            for item in resp.get("results", []):
                entry = {"id": item["id"], "title": plain_text(item.get("title")) or "(無題)"}
                # 検索結果にスキーマが含まれていればタイトル列名を控えておく（オフライン送信用）
                for name, prop in (item.get("properties") or {}).items():
                    if prop.get("type") == "title":
                        entry["title_prop"] = name
                results.append(entry)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        results.sort(key=lambda d: d["title"])
        return results

    def get_data_source(self, data_source_id):
        return self._request("GET", f"/data_sources/{data_source_id}")

    def create_page(self, data_source_id, properties, children):
        """ページを作成し、作成されたページオブジェクトを返す。100 ブロック超は追記で送る。"""
        body = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
            "children": children[:MAX_CHILDREN],
        }
        page = self._request("POST", "/pages", body)
        for i in range(MAX_CHILDREN, len(children), MAX_CHILDREN):
            try:
                self._request("PATCH", f"/blocks/{page['id']}/children",
                              {"children": children[i:i + MAX_CHILDREN]})
            except NotionError as e:
                # ページ自体は作成済みなので、再送すると重複する。再送不可として扱う
                raise NotionError(f"ページは作成されましたが本文の一部追加に失敗しました（{e}）",
                                  e.status, False) from e
        return page


def plain_text(rich_text):
    return "".join(t.get("plain_text", "") for t in rich_text or [])
