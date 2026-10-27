"""Notion 公式 MCP サーバー（https://mcp.notion.com/mcp）のクライアント。

認証は OAuth 2.0 Authorization Code + PKCE。クライアントは動的クライアント登録（RFC 7591）で
自動登録し、リダイレクト先はローカルの http://127.0.0.1:<port>/callback を使う。
"""

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from notiontask import APP_NAME, __version__
from notiontask.net import USER_AGENT
from notiontask.notion_api import NotionError

MCP_URL = "https://mcp.notion.com/mcp"
METADATA_URL = "https://mcp.notion.com/.well-known/oauth-authorization-server"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 30
LOGIN_TIMEOUT = 300  # ブラウザでの許可を待つ秒数


def _http(method, url, body=None, headers=None, form=False, opener=urllib.request.urlopen):
    """(status, headers, bytes) を返す。HTTP エラーも例外にせず返す。通信エラーは NotionError。"""
    if body is None:
        data = None
    elif form:
        data = urllib.parse.urlencode(body).encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise NotionError(f"通信エラー: {getattr(e, 'reason', e)}", None, True) from e


def _json(data):
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


# ---- OAuth ------------------------------------------------------------------

def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
    return verifier, challenge.decode("ascii")


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """/callback のクエリを server.result に格納する。"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = dict(urllib.parse.parse_qsl(parsed.query))
        ok = "code" in self.server.result
        message = "ログインしました。このタブを閉じてアプリに戻ってください。" if ok else \
            "ログインできませんでした。アプリに戻ってやり直してください。"
        body = (f"<!doctype html><meta charset='utf-8'><title>{APP_NAME}</title>"
                f"<body style='font-family:sans-serif;padding:2em'><h2>{APP_NAME}</h2><p>{message}</p>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _bind_server(port):
    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.result = None
    server.timeout = 1
    return server


def login(oauth, open_browser=webbrowser.open, cancel_event=None):
    """ブラウザで Notion にログインし、トークンを含む oauth 情報（dict）を返す。

    oauth には前回の client_id / redirect_uri があれば再利用する（無ければ登録する）。
    戻り値の access_token / refresh_token は平文なので、保存時に暗号化すること。
    """
    status, _, data = _http("GET", METADATA_URL)
    if status != 200:
        raise NotionError(f"認証情報の取得に失敗しました（HTTP {status}）", status)
    meta = _json(data)
    oauth = dict(oauth or {})

    server = None
    port = int(oauth["redirect_uri"].rsplit(":", 1)[1].split("/")[0]) if oauth.get("redirect_uri") else 0
    if port:
        try:
            server = _bind_server(port)
        except OSError:
            server = None  # 前回のポートが使用中なら登録し直す
    if server is None:
        server = _bind_server(0)
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/callback"
        status, _, data = _http("POST", meta["registration_endpoint"], {
            "client_name": APP_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        reg = _json(data)
        if status not in (200, 201) or "client_id" not in reg:
            server.server_close()
            raise NotionError(f"アプリの登録に失敗しました（HTTP {status}）", status)
        oauth["client_id"] = reg["client_id"]
        oauth["redirect_uri"] = redirect_uri

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": oauth["client_id"],
        "redirect_uri": oauth["redirect_uri"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "default",
        "resource": MCP_URL,
    })
    try:
        open_browser(f"{meta['authorization_endpoint']}?{query}")
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while server.result is None:
            if time.monotonic() > deadline:
                raise NotionError("ログインがタイムアウトしました")
            if cancel_event and cancel_event.is_set():
                raise NotionError("ログインを中止しました")
            server.handle_request()
    finally:
        server.server_close()

    result = server.result
    if result.get("state") != state:
        raise NotionError("ログイン応答が不正です（state 不一致）")
    if "code" not in result:
        raise NotionError(f"ログインが拒否されました: {result.get('error_description') or result.get('error')}")

    tokens = _token_request(meta["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": oauth["redirect_uri"],
        "client_id": oauth["client_id"],
        "code_verifier": verifier,
        "resource": MCP_URL,
    })
    oauth["token_endpoint"] = meta["token_endpoint"]
    oauth.update(tokens)
    return oauth


def _token_request(endpoint, params):
    status, _, data = _http("POST", endpoint, params, form=True)
    body = _json(data)
    if status != 200 or "access_token" not in body:
        if status in (400, 401):
            raise NotionError("ログインの有効期限が切れました。設定から再ログインしてください",
                              status, needs_login=True)
        raise NotionError(f"トークンの取得に失敗しました（HTTP {status}）", status, status >= 500 or status == 429)
    tokens = {"access_token": body["access_token"],
              "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60}
    if body.get("refresh_token"):
        tokens["refresh_token"] = body["refresh_token"]
    return tokens


def refresh(oauth):
    """refresh_token でアクセストークンを更新した oauth 情報を返す。"""
    if not oauth.get("refresh_token"):
        raise NotionError("ログインしていません。設定からログインしてください", needs_login=True)
    tokens = _token_request(oauth.get("token_endpoint", "https://mcp.notion.com/token"), {
        "grant_type": "refresh_token",
        "refresh_token": oauth["refresh_token"],
        "client_id": oauth["client_id"],
        "resource": MCP_URL,
    })
    return {**oauth, **tokens}


# ---- MCP --------------------------------------------------------------------

def _parse_rpc_response(content_type, data, request_id):
    """JSON または SSE（text/event-stream）から指定 id の JSON-RPC 応答を取り出す。"""
    if "text/event-stream" in (content_type or ""):
        for event in data.decode("utf-8").split("\n\n"):
            payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            msg = _json(payload.encode("utf-8")) if payload else {}
            if msg.get("id") == request_id:
                return msg
        return {}
    return _json(data)


class McpClient:
    """oauth_store: get() で oauth dict（平文トークン入り）を返し、set(dict) で保存する。"""

    def __init__(self, oauth_store, opener=urllib.request.urlopen):
        self.store = oauth_store
        self._open = opener
        self._session_id = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._tool_names = None

    def _access_token(self, force_refresh=False):
        oauth = self.store.get()
        if not oauth.get("access_token") and not oauth.get("refresh_token"):
            raise NotionError("ログインしていません。設定からログインしてください", needs_login=True)
        if force_refresh or time.time() >= oauth.get("expires_at", 0):
            oauth = refresh(oauth)
            self.store.set(oauth)
        return oauth["access_token"]

    def _post(self, message, retry_auth=True):
        headers = {"Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self._access_token()}",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        status, resp_headers, data = _http("POST", MCP_URL, message, headers, opener=self._open)
        if status == 401 and retry_auth:
            self._access_token(force_refresh=True)
            return self._post(message, retry_auth=False)
        if status == 401:
            raise NotionError("ログインの有効期限が切れました。設定から再ログインしてください", 401, needs_login=True)
        if status == 404 and self._session_id:
            self._session_id = None  # セッション切れ。呼び出し側で再初期化する
            raise _SessionExpired()
        if status >= 400:
            detail = _json(data).get("error", {})
            detail = detail.get("message") if isinstance(detail, dict) else detail
            raise NotionError(f"HTTP {status}: {detail or ''}".strip(), status, status == 429 or status >= 500)
        if resp_headers.get("Mcp-Session-Id"):
            self._session_id = resp_headers["Mcp-Session-Id"]
        return resp_headers.get("Content-Type", ""), data

    def _rpc(self, method, params):
        request_id = self._next_id
        self._next_id += 1
        content_type, data = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        msg = _parse_rpc_response(content_type, data, request_id)
        if "error" in msg:
            raise NotionError(f"MCP エラー: {msg['error'].get('message')}")
        if "result" not in msg:
            raise NotionError("MCP サーバーから応答がありませんでした", None, True)
        return msg["result"]

    def _initialize(self):
        self._session_id = None
        self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                 "clientInfo": {"name": APP_NAME, "version": __version__}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = self._rpc("tools/list", {}).get("tools", [])
        self._tool_names = {t["name"] for t in tools}

    def _tool(self, short_name):
        """サーバー側のツール名（notion-search / search など）の差異を吸収する。"""
        for name in (f"notion-{short_name}", short_name):
            if name in self._tool_names:
                return name
        raise NotionError(f"MCP サーバーに {short_name} ツールがありません")

    def call_tool(self, short_name, arguments):
        """ツールを呼び、結果テキストを JSON として解釈できればその値、できなければ文字列を返す。"""
        with self._lock:
            for attempt in range(2):
                try:
                    if self._tool_names is None or self._session_id is None:
                        self._initialize()
                    result = self._rpc("tools/call", {"name": self._tool(short_name), "arguments": arguments})
                    break
                except _SessionExpired:
                    if attempt:
                        raise NotionError("MCP セッションを確立できません", None, True)
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise NotionError(text[:300] or "MCP ツールがエラーを返しました")
        try:
            return json.loads(text)
        except ValueError:
            return text


class _SessionExpired(Exception):
    pass


# ---- 応答の解釈 ---------------------------------------------------------------

_UUID = re.compile(r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})"
                   r"(?![0-9a-f])", re.I)


def parse_data_source_urls(text):
    """データベースの fetch 結果から collection://<id> を順に取り出す（重複除去）。"""
    seen = []
    for ds_id in re.findall(r"collection://([0-9a-fA-F-]{32,36})", text):
        if ds_id not in seen:
            seen.append(ds_id)
    return seen


def parse_schema(text):
    """data source の fetch 結果（<data-source-state> の JSON）から {"name", "schema"} を取り出す。"""
    m = re.search(r"<data-source-state>\s*(\{.*?\})\s*</data-source-state>", text, re.S)
    if not m:
        raise NotionError("データベースの構成を読み取れませんでした")
    state = json.loads(m.group(1))
    return {"name": state.get("name", ""), "schema": state.get("schema", {})}


def find_url(obj):
    """create-pages の応答からページ URL を探す（形式の違いに強くするため再帰的に探す）。"""
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return url
        for v in obj.values():
            found = find_url(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_url(v)
            if found:
                return found
    elif isinstance(obj, str):
        m = re.search(r"https://[^\s\"'<>]*notion\.(?:so|com)/[^\s\"'<>]+", obj)
        if m:
            return m.group(0)
    return None


def normalize_id(text):
    """URL や ID 文字列から UUID 部分を取り出す。見つからなければ None。"""
    m = _UUID.search(text) if text else None
    if not m:
        return None
    raw = m.group(1).replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
