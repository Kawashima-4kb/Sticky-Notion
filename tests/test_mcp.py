import io
import json
import threading
import time
import unittest
import urllib.parse
import urllib.request
from email.message import Message
from unittest import mock

from notiontask import notion_mcp
from notiontask.backends import McpBackend
from notiontask.markdown import text_to_notion_markdown
from notiontask.notion_api import NotionError
from notiontask.properties import build_mcp_properties, editable_properties_mcp, title_property_name_mcp

SCHEMA_TEXT = """<data-source url="{{collection://12345678-90ab-4cde-8f01-23456789abcd}}">
<data-source-state>
{"name":"タスク","schema":{
 "名前":{"name":"名前","type":"title"},
 "状態":{"name":"状態","type":"status","options":[{"name":"未着手"},{"name":"完了"}]},
 "種別":{"name":"種別","type":"select","options":[{"name":"A"}]},
 "タグ":{"name":"タグ","type":"multi_select","options":[{"name":"x"},{"name":"y"}]},
 "期日":{"name":"期日","type":"date"},
 "重要":{"name":"重要","type":"checkbox"},
 "URL":{"name":"URL","type":"url"},
 "メモ":{"name":"メモ","type":"text"}}}
</data-source-state>
</data-source>"""


class NotionMarkdownTest(unittest.TestCase):
    def test_conversion(self):
        md = text_to_notion_markdown("# 見出し\n本文 <b>\n\n\n- a\n- b\n[] やる\n[x] 済\n> 引用\n1. 一")
        self.assertEqual(md, "# 見出し\n\n本文 \\<b>\n\n<p />\n\n- a\n- b\n\n- [ ] やる\n- [x] 済"
                             "\n\n> 引用\n\n1. 一")

    def test_empty(self):
        self.assertEqual(text_to_notion_markdown("  \n"), "")


class McpPropertiesTest(unittest.TestCase):
    def setUp(self):
        self.schema = notion_mcp.parse_schema(SCHEMA_TEXT)["schema"]

    def test_fields(self):
        self.assertEqual(title_property_name_mcp(self.schema), "名前")
        fields = {f["name"]: f for f in editable_properties_mcp(self.schema)}
        self.assertEqual(set(fields), {"状態", "種別", "タグ", "期日", "重要"})
        self.assertEqual(fields["状態"]["options"], ["未着手", "完了"])

    def test_status_groups(self):
        fields = editable_properties_mcp({"S": {"type": "status", "groups": [
            {"options": [{"name": "a"}]}, {"options": [{"name": "b"}]}]}})
        self.assertEqual(fields[0]["options"], ["a", "b"])

    def test_build(self):
        fields = editable_properties_mcp(self.schema)
        props = build_mcp_properties("名前", "T", fields, {
            "状態": "完了", "種別": "", "タグ": "x、y", "期日": "2026/1/2", "重要": True})
        self.assertEqual(props, {
            "名前": "T", "状態": "完了", "タグ": '["x", "y"]',
            "date:期日:start": "2026-01-02", "date:期日:is_datetime": 0, "重要": "__YES__"})

    def test_reserved_names(self):
        props = build_mcp_properties("URL", "T", [{"name": "id", "type": "select", "options": []}], {"id": "a"})
        self.assertEqual(props, {"userDefined:URL": "T", "userDefined:id": "a"})


class ParseTest(unittest.TestCase):
    def test_parse_schema(self):
        parsed = notion_mcp.parse_schema(SCHEMA_TEXT)
        self.assertEqual(parsed["name"], "タスク")
        with self.assertRaises(NotionError):
            notion_mcp.parse_schema("no state")

    def test_data_source_urls(self):
        text = ('<data-source url="{{collection://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}}">'
                ' collection://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee collection://11111111222233334444555555555555')
        self.assertEqual(notion_mcp.parse_data_source_urls(text),
                         ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "11111111222233334444555555555555"])

    def test_normalize_id(self):
        expected = "01234567-89ab-4cde-af01-23456789abcd"
        for text in ("https://app.notion.com/p/0123456789ab4cdeaf0123456789abcd?pvs=204",
                     "https://www.notion.so/ws/Cafe-0123456789ab4cdeaf0123456789abcd?v=1",
                     "01234567-89AB-4CDE-AF01-23456789ABCD"):
            self.assertEqual(notion_mcp.normalize_id(text), expected, text)
        self.assertIsNone(notion_mcp.normalize_id("タスク管理"))

    def test_find_url(self):
        self.assertEqual(notion_mcp.find_url({"pages": [{"id": "x", "url": "https://www.notion.so/p1"}]}),
                         "https://www.notion.so/p1")
        self.assertEqual(notion_mcp.find_url("Created https://app.notion.com/p/abc ok"),
                         "https://app.notion.com/p/abc")
        self.assertIsNone(notion_mcp.find_url({"ok": True}))


class FakeHttpResponse(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for k, v in (headers or {}).items():
            self.headers[k] = v

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeMcpServer:
    """McpClient の opener として使う、最小限の MCP サーバー。"""

    def __init__(self, sse=False, fail_first_with=None):
        self.sse = sse
        self.fail_first_with = fail_first_with
        self.requests = []
        self.created = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        headers = dict(req.header_items())
        self.requests.append((body, headers))
        if self.fail_first_with:
            status, self.fail_first_with = self.fail_first_with, None
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, status, "x", Message(), io.BytesIO(b"{}"))
        if "id" not in body:
            return FakeHttpResponse(202, b"")
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": notion_mcp.PROTOCOL_VERSION, "capabilities": {}}
        elif method == "tools/list":
            result = {"tools": [{"name": n} for n in ("notion-search", "notion-fetch", "notion-create-pages")]}
        else:
            name, args = body["params"]["name"], body["params"]["arguments"]
            if name == "notion-create-pages":
                self.created.append(args)
                payload = {"pages": [{"id": "p1", "url": "https://app.notion.com/p/p1"}]}
            elif name == "notion-fetch":
                payload = {"metadata": {"type": "data_source"}, "title": "タスク", "text": SCHEMA_TEXT}
            else:
                payload = {"results": [{"id": "db1", "title": "タスクDB", "type": "database", "path": "ホーム"},
                                       {"id": "pg1", "title": "ページ", "type": "page"}]}
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            data = f"event: message\ndata: {json.dumps(message)}\n\n".encode()
            return FakeHttpResponse(200, data, {"Content-Type": "text/event-stream", "Mcp-Session-Id": "s1"})
        return FakeHttpResponse(200, json.dumps(message).encode(),
                                {"Content-Type": "application/json", "Mcp-Session-Id": "s1"})


class MemoryStore:
    def __init__(self, oauth):
        self.oauth = oauth

    def get(self):
        return dict(self.oauth)

    def set(self, oauth):
        self.oauth = oauth


def logged_in_store():
    return MemoryStore({"access_token": "at", "refresh_token": "rt", "client_id": "c",
                        "expires_at": time.time() + 3600})


class McpClientTest(unittest.TestCase):
    def test_backend_flow(self):
        for sse in (False, True):
            server = FakeMcpServer(sse=sse)
            backend = McpBackend(notion_mcp.McpClient(logged_in_store(), opener=server))
            results = backend.search("タスク")
            self.assertEqual([r["id"] for r in results], ["db1"])
            schema = backend.get_schema("12345678-90ab-4cde-8f01-23456789abcd")
            self.assertEqual(schema["title_prop"], "名前")
            url = backend.create_page("ds1", "名前", "T", schema["fields"], {"重要": True}, "- a")
            self.assertEqual(url, "https://app.notion.com/p/p1")
            args = server.created[0]
            self.assertEqual(args["parent"], {"type": "data_source_id", "data_source_id": "ds1"})
            self.assertEqual(args["pages"][0], {"properties": {"名前": "T", "重要": "__YES__"}, "content": "- a"})
            # 初期化は 1 回だけ。2 回目以降は Mcp-Session-Id を付ける
            methods = [b.get("method") for b, _ in server.requests]
            self.assertEqual(methods.count("initialize"), 1)
            last_headers = server.requests[-1][1]
            self.assertEqual(last_headers["Mcp-session-id"], "s1")
            self.assertEqual(last_headers["Authorization"], "Bearer at")
            self.assertTrue(last_headers["User-agent"].startswith("Sticky-Notion/"))

    def test_url_query_skips_search(self):
        server = FakeMcpServer()
        backend = McpBackend(notion_mcp.McpClient(logged_in_store(), opener=server))
        items = backend.search("https://app.notion.com/p/0123456789ab4cdeaf0123456789abcd")
        self.assertEqual(items[0]["id"], "01234567-89ab-4cde-af01-23456789abcd")
        self.assertEqual(server.requests, [])
        self.assertEqual(backend.expand(items[0]), [{"id": items[0]["id"], "title": "タスク"}])

    def test_refresh_on_401(self):
        store = logged_in_store()
        server = FakeMcpServer(fail_first_with=401)
        refreshed = {**store.oauth, "access_token": "new", "expires_at": time.time() + 3600}
        with mock.patch.object(notion_mcp, "refresh", return_value=refreshed) as refresh:
            notion_mcp.McpClient(store, opener=server).call_tool("search", {"query": "x"})
        refresh.assert_called_once()
        self.assertEqual(store.oauth["access_token"], "new")
        self.assertEqual(server.requests[-1][1]["Authorization"], "Bearer new")

    def test_expired_token_refreshed_before_call(self):
        store = MemoryStore({"access_token": "old", "refresh_token": "rt", "client_id": "c", "expires_at": 0})
        refreshed = {**store.oauth, "access_token": "new", "expires_at": time.time() + 3600}
        with mock.patch.object(notion_mcp, "refresh", return_value=refreshed):
            server = FakeMcpServer()
            notion_mcp.McpClient(store, opener=server).call_tool("search", {"query": "x"})
        self.assertTrue(all(h["Authorization"] == "Bearer new" for _, h in server.requests))

    def test_not_logged_in(self):
        with self.assertRaises(NotionError) as ctx:
            notion_mcp.McpClient(MemoryStore({}), opener=FakeMcpServer()).call_tool("search", {})
        self.assertTrue(ctx.exception.needs_login)
        self.assertTrue(ctx.exception.retryable)

    def test_session_expired_reinitializes(self):
        server = FakeMcpServer()
        client = notion_mcp.McpClient(logged_in_store(), opener=server)
        client.call_tool("search", {"query": "x"})
        server.fail_first_with = 404
        client.call_tool("search", {"query": "y"})
        self.assertEqual([b.get("method") for b, _ in server.requests].count("initialize"), 2)


class LoginTest(unittest.TestCase):
    def test_login_flow(self):
        calls = []
        meta = {"authorization_endpoint": "https://auth.example/authorize",
                "token_endpoint": "https://auth.example/token",
                "registration_endpoint": "https://auth.example/register"}

        def fake_http(method, url, body=None, headers=None, form=False, opener=None):
            calls.append((method, url, body))
            if url == notion_mcp.METADATA_URL:
                return 200, {}, json.dumps(meta).encode()
            if url == meta["registration_endpoint"]:
                return 201, {}, json.dumps({"client_id": "cid", **body}).encode()
            if url == meta["token_endpoint"]:
                return 200, {}, json.dumps({"access_token": "at", "refresh_token": "rt", "expires_in": 3600}).encode()
            raise AssertionError(url)

        def browser(url):
            # ブラウザの代わりに、許可後のリダイレクトをローカルサーバーへ送る
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            self.assertEqual(q["code_challenge_method"], "S256")
            self.assertEqual(q["resource"], notion_mcp.MCP_URL)
            target = f"{q['redirect_uri']}?code=thecode&state={q['state']}"
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

        with mock.patch.object(notion_mcp, "_http", side_effect=fake_http):
            oauth = notion_mcp.login({}, open_browser=browser)
        self.assertEqual((oauth["client_id"], oauth["access_token"], oauth["refresh_token"]), ("cid", "at", "rt"))
        self.assertTrue(oauth["redirect_uri"].startswith("http://127.0.0.1:"))
        token_call = calls[-1][2]
        self.assertEqual(token_call["code"], "thecode")
        self.assertEqual(token_call["grant_type"], "authorization_code")
        self.assertIn("code_verifier", token_call)

    def test_refresh_failure_needs_login(self):
        with mock.patch.object(notion_mcp, "_http", return_value=(400, {}, b'{"error":"invalid_grant"}')):
            with self.assertRaises(NotionError) as ctx:
                notion_mcp.refresh({"refresh_token": "rt", "client_id": "c"})
        self.assertTrue(ctx.exception.needs_login)


if __name__ == "__main__":
    unittest.main()
