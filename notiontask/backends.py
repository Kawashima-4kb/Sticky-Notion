"""接続方式（トークン / MCP）ごとの差を吸収し、同じ呼び出し方で DB 検索・スキーマ取得・ページ作成を行う。

スキーマは {"title_prop": タイトル列名, "fields": [{"name", "type", "options"}]} に正規化する。
"""

from notiontask import notion_mcp
from notiontask.markdown import text_to_blocks, text_to_notion_markdown
from notiontask.notion_api import NotionClient, NotionError
from notiontask.properties import (build_mcp_properties, build_properties, editable_properties,
                                   editable_properties_mcp, title_property_name,
                                   title_property_name_mcp)
from notiontask.secretstore import protect, unprotect

SECRET_KEYS = ("access_token", "refresh_token")


class RestBackend:
    """Internal Integration トークンで REST API を使う。共有された DB を一覧取得できる。"""

    can_list_all = True

    def __init__(self, token, client=None):
        self.client = client or NotionClient(token)

    def list_all(self):
        return self.client.list_data_sources()

    def search(self, query):
        query = query.strip().lower()
        return [{**d, "path": "", "kind": "data_source"} for d in self.list_all()
                if query in d["title"].lower()]

    def expand(self, item):
        return [{"id": item["id"], "title": item["title"]}]

    def get_schema(self, ds_id):
        schema = self.client.get_data_source(ds_id)
        return {"title_prop": title_property_name(schema), "fields": editable_properties(schema)}

    def create_page(self, ds_id, title_prop, title, fields, values, body):
        props = build_properties(title_prop, title, fields, values)
        page = self.client.create_page(ds_id, props, text_to_blocks(body))
        return page.get("url")


class McpBackend:
    """Notion MCP を使う。全 DB の一覧取得はできないので、検索か URL で登録する。"""

    can_list_all = False

    def __init__(self, client):
        self.client = client

    def search(self, query):
        ds_or_db_id = notion_mcp.normalize_id(query)
        if ds_or_db_id:  # URL / ID が貼られた場合は検索せずそのまま候補にする
            return [{"id": ds_or_db_id, "title": query.strip(), "path": "", "kind": "id"}]
        result = self.client.call_tool("search", {"query": query.strip(), "page_size": 25,
                                                  "max_highlight_length": 0})
        items = result.get("results", []) if isinstance(result, dict) else []
        return [{"id": r["id"], "title": r.get("title") or "(無題)", "path": r.get("path", ""),
                 "kind": r.get("type")}
                for r in items if r.get("type") in ("database", "data_source")]

    def _fetch(self, entity_id):
        result = self.client.call_tool("fetch", {"id": entity_id})
        if not isinstance(result, dict):
            return {"metadata": {}, "title": "", "text": str(result)}
        return result

    def expand(self, item):
        """DB（または data source）を、登録できる data source の一覧 [{"id", "title"}] に展開する。"""
        result = self._fetch(item["id"])
        text = result.get("text", "")
        if result.get("metadata", {}).get("type") == "data_source":
            return [{"id": item["id"], "title": notion_mcp.parse_schema(text)["name"] or result.get("title", "")}]
        ds_ids = notion_mcp.parse_data_source_urls(text)
        if not ds_ids:
            raise NotionError("データベースが見つかりませんでした（ページの URL ではありませんか？）")
        db_title = result.get("title") or item["title"]
        if len(ds_ids) == 1:
            return [{"id": ds_ids[0], "title": db_title}]
        sources = []
        for ds_id in ds_ids:
            name = notion_mcp.parse_schema(self._fetch(f"collection://{ds_id}").get("text", ""))["name"]
            sources.append({"id": ds_id, "title": f"{db_title} / {name}" if name else db_title})
        return sources

    def get_schema(self, ds_id):
        schema = notion_mcp.parse_schema(self._fetch(f"collection://{ds_id}").get("text", ""))["schema"]
        return {"title_prop": title_property_name_mcp(schema), "fields": editable_properties_mcp(schema)}

    def create_page(self, ds_id, title_prop, title, fields, values, body):
        page = {"properties": build_mcp_properties(title_prop, title, fields, values)}
        content = text_to_notion_markdown(body)
        if content:
            page["content"] = content
        result = self.client.call_tool("create-pages", {
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "pages": [page],
        })
        return notion_mcp.find_url(result)


class OAuthStore:
    """接続設定の oauth を暗号化して保存する（McpClient から別スレッドで呼ばれる）。"""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn

    def get(self):
        oauth = dict(self.conn.get("oauth") or {})
        for key in SECRET_KEYS:
            oauth[key] = unprotect(oauth.get(key, ""))
        return oauth

    def set(self, oauth):
        stored = dict(oauth)
        for key in SECRET_KEYS:
            stored[key] = protect(stored.get(key, ""))
        with self.cfg.lock:
            self.conn["oauth"] = stored
            self.cfg.save()


def make_backend(cfg, conn):
    if conn["type"] == "mcp":
        return McpBackend(notion_mcp.McpClient(OAuthStore(cfg, conn)))
    return RestBackend(unprotect(conn.get("token", "")))
