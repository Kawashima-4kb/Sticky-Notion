import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from notiontask.config import Config, app_dir
from notiontask.hotkey import parse_hotkey
from notiontask.markdown import split_title, text_to_blocks
from notiontask.notion_api import NotionClient, NotionError
from notiontask.properties import build_properties, editable_properties, parse_date, title_property_name
from notiontask.secretstore import protect, unprotect
from notiontask.sendqueue import SendQueue


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    """リクエストを記録し、用意したレスポンス（dict か例外）を順に返す。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append((req.get_method(), req.full_url, body, dict(req.header_items())))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return FakeResponse(json.dumps(resp).encode("utf-8"))


def http_error(code, message="err"):
    return urllib.error.HTTPError("https://api.notion.com", code, "x", {},
                                  io.BytesIO(json.dumps({"message": message}).encode()))


SCHEMA = {"properties": {
    "名前": {"type": "title", "title": {}},
    "状態": {"type": "status", "status": {"options": [{"name": "未着手"}, {"name": "完了"}]}},
    "タグ": {"type": "multi_select", "multi_select": {"options": [{"name": "A"}]}},
    "期日": {"type": "date", "date": {}},
    "重要": {"type": "checkbox", "checkbox": {}},
    "メモ": {"type": "rich_text", "rich_text": {}},
}}


class MarkdownTest(unittest.TestCase):
    def test_block_types(self):
        text = "# 見出し\n## 中\n### 小\n- 箇条\n* 箇条2\n1. 番号\n[] やる\n[x] 済\n> 引用\n普通"
        types = [b["type"] for b in text_to_blocks(text)]
        self.assertEqual(types, ["heading_1", "heading_2", "heading_3", "bulleted_list_item",
                                 "bulleted_list_item", "numbered_list_item", "to_do", "to_do",
                                 "quote", "paragraph"])
        blocks = text_to_blocks("[] a\n[x] b")
        self.assertFalse(blocks[0]["to_do"]["checked"])
        self.assertTrue(blocks[1]["to_do"]["checked"])
        self.assertEqual(blocks[1]["to_do"]["rich_text"][0]["text"]["content"], "b")

    def test_blank_lines_collapsed(self):
        blocks = text_to_blocks("\n\na\n\n\n\nb\n\n")
        self.assertEqual([b["type"] for b in blocks], ["paragraph"] * 3)
        self.assertEqual(blocks[1]["paragraph"]["rich_text"], [])

    def test_long_text_split(self):
        blocks = text_to_blocks("x" * 4500)
        self.assertEqual([len(t["text"]["content"]) for t in blocks[0]["paragraph"]["rich_text"]],
                         [2000, 2000, 500])

    def test_split_title(self):
        self.assertEqual(split_title(" T ", "body"), ("T", "body"))
        self.assertEqual(split_title("", "\n# 件名\n本文\n- a"), ("件名", "本文\n- a"))
        self.assertEqual(split_title("", "[ ] やること"), ("やること", ""))
        self.assertEqual(split_title("", "  \n "), ("", ""))


class PropertiesTest(unittest.TestCase):
    def test_schema(self):
        self.assertEqual(title_property_name(SCHEMA), "名前")
        fields = editable_properties(SCHEMA)
        self.assertEqual([f["name"] for f in fields], sorted(["状態", "タグ", "期日", "重要"]))
        self.assertEqual(next(f for f in fields if f["name"] == "状態")["options"], ["未着手", "完了"])

    def test_build(self):
        fields = editable_properties(SCHEMA)
        props = build_properties("名前", "T", fields, {
            "状態": "完了", "タグ": "A, B、C", "期日": "2026/10/01", "重要": True})
        self.assertEqual(props["名前"]["title"][0]["text"]["content"], "T")
        self.assertEqual(props["状態"], {"status": {"name": "完了"}})
        self.assertEqual(props["タグ"]["multi_select"], [{"name": "A"}, {"name": "B"}, {"name": "C"}])
        self.assertEqual(props["期日"], {"date": {"start": "2026-10-01"}})
        self.assertEqual(props["重要"], {"checkbox": True})

    def test_empty_values_skipped(self):
        props = build_properties("名前", "T", editable_properties(SCHEMA),
                                 {"状態": "", "タグ": " ", "期日": "", "重要": False})
        self.assertEqual(list(props), ["名前"])

    def test_parse_date(self):
        today = datetime.date(2026, 12, 31)
        self.assertEqual(parse_date("今日", today), "2026-12-31")
        self.assertEqual(parse_date("明日", today), "2027-01-01")
        with self.assertRaises(ValueError):
            parse_date("あした")


class NotionClientTest(unittest.TestCase):
    def test_list_data_sources_paginates(self):
        opener = FakeOpener([
            {"results": [{"id": "2", "title": [{"plain_text": "B"}],
                          "properties": {"Name": {"type": "title"}}}],
             "has_more": True, "next_cursor": "c1"},
            {"results": [{"id": "1", "title": []}], "has_more": False},
        ])
        items = NotionClient("tok", opener).list_data_sources()
        self.assertEqual(items, [{"id": "1", "title": "(無題)"},
                                 {"id": "2", "title": "B", "title_prop": "Name"}])
        method, url, body, headers = opener.requests[1]
        self.assertEqual((method, url), ("POST", "https://api.notion.com/v1/search"))
        self.assertEqual(body["start_cursor"], "c1")
        self.assertEqual(body["filter"], {"property": "object", "value": "data_source"})
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["Notion-version"], "2025-09-03")

    def test_create_page_chunks_children(self):
        children = text_to_blocks("\n".join(f"l{i}" for i in range(250)))
        opener = FakeOpener([{"id": "p1", "url": "u"}, {}, {}])
        page = NotionClient("tok", opener).create_page("ds", {"Name": {}}, children)
        self.assertEqual(page["id"], "p1")
        self.assertEqual(opener.requests[0][2]["parent"], {"type": "data_source_id", "data_source_id": "ds"})
        self.assertEqual([len(r[2]["children"]) for r in opener.requests], [100, 100, 50])
        self.assertEqual(opener.requests[1][:2], ("PATCH", "https://api.notion.com/v1/blocks/p1/children"))

    def test_errors(self):
        cases = [(http_error(400, "bad"), False, 400), (http_error(429), True, 429),
                 (http_error(502), True, 502), (urllib.error.URLError("offline"), True, None)]
        for exc, retryable, status in cases:
            with self.assertRaises(NotionError) as ctx:
                NotionClient("t", FakeOpener([exc])).get_data_source("x")
            self.assertEqual((ctx.exception.retryable, ctx.exception.status), (retryable, status))
        with self.assertRaises(NotionError) as ctx:
            NotionClient("t", FakeOpener([http_error(400, "bad")])).get_data_source("x")
        self.assertIn("bad", str(ctx.exception))

    def test_append_failure_not_retryable(self):
        children = text_to_blocks("\n".join("x" for _ in range(150)))
        opener = FakeOpener([{"id": "p1"}, urllib.error.URLError("offline")])
        with self.assertRaises(NotionError) as ctx:
            NotionClient("t", opener).create_page("ds", {}, children)
        self.assertFalse(ctx.exception.retryable)


class FakeSender:
    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.sent = []

    def __call__(self, item):
        outcome = self.outcomes.get(item["data_source_id"])
        if outcome:
            raise outcome
        self.sent.append(item["data_source_id"])


class SendQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "queue.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_flush(self):
        q = SendQueue(self.path)
        for conn, ds in (("c1", "a"), ("c1", "b"), ("c1", "c"), ("c1", "d"), ("c2", "e")):
            q.add({"conn": conn, "data_source_id": ds, "title": ds})
        self.assertEqual(SendQueue(self.path).counts(), (5, 0))  # 永続化されている
        sender = FakeSender({"b": NotionError("bad", 400), "c": NotionError("net", None, True)})
        self.assertEqual(q.flush(sender), 2)
        # c1 は c の通信エラーで打ち切り（d は送らない）、別接続の e は送る
        self.assertEqual(sender.sent, ["a", "e"])
        self.assertEqual(q.counts(), (2, 1))  # c,d は再送待ち、b はエラー
        self.assertEqual(q.flush(FakeSender({})), 2)
        self.assertEqual(q.counts(), (0, 1))
        q.clear_errors()
        self.assertEqual(SendQueue(self.path).counts(), (0, 0))

    def test_needs_login_is_retryable(self):
        q = SendQueue(self.path)
        q.add({"conn": "c1", "data_source_id": "a"})
        q.flush(FakeSender({"a": NotionError("login", 401, needs_login=True)}))
        self.assertEqual(q.counts(), (1, 0))


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text('{"hotkey": "Ctrl+Alt+M", "unknown": 1}', encoding="utf-8")
            cfg = Config(path)
            self.assertEqual(cfg["hotkey"], "Ctrl+Alt+M")
            self.assertTrue(cfg["topmost"])
            self.assertNotIn("unknown", cfg.data)
            cfg["alpha"] = 0.5
            cfg.save()
            self.assertEqual(Config(path)["alpha"], 0.5)

    def test_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(Config(path)["connections"], [])

    def test_app_dir_migrates_legacy_folder(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "NotionTask"
            legacy.mkdir()
            (legacy / "config.json").write_text('{"hotkey": "Ctrl+Alt+M"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"APPDATA": d}):
                path = app_dir()
                self.assertEqual(path.name, "Sticky-Notion")
                self.assertFalse(legacy.exists())
                self.assertEqual(Config(path / "config.json")["hotkey"], "Ctrl+Alt+M")
                # 2 回目以降は何もしない
                self.assertEqual(app_dir(), path)

    def test_migrate_single_token(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"token": "secret", "last_data_source_id": "ds1",
                                        "data_sources": [{"id": "ds1", "title": "T"}]}), encoding="utf-8")
            cfg = Config(path)
            conn = cfg["connections"][0]
            self.assertEqual(conn["type"], "token")
            self.assertEqual(unprotect(conn["token"]), "secret")
            self.assertEqual(cfg["data_sources"][0]["conn"], conn["id"])
            self.assertEqual(cfg["last_data_source"], f"{conn['id']}/ds1")


class SecretStoreTest(unittest.TestCase):
    def test_roundtrip(self):
        enc = protect("ntn_abc")
        if sys.platform == "win32":
            self.assertTrue(enc.startswith("dpapi:"))
            self.assertNotIn("ntn_abc", enc)
        self.assertEqual(unprotect(enc), "ntn_abc")
        self.assertEqual(unprotect("plain"), "plain")
        self.assertEqual(unprotect("dpapi:broken!!"), "")
        self.assertEqual(protect(""), "")


class HotkeyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_hotkey("Ctrl+Alt+N"), (0x0003, ord("N")))
        self.assertEqual(parse_hotkey("ctrl + shift + space"), (0x0006, 0x20))
        self.assertEqual(parse_hotkey("Win+F9"), (0x0008, 0x78))
        for bad in ("N", "Ctrl+", "Hyper+N", "Ctrl+??"):
            with self.assertRaises(ValueError):
                parse_hotkey(bad)


if __name__ == "__main__":
    unittest.main()
