"""設定画面と DB 管理画面。"""

import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk

from notiontask import notion_mcp
from notiontask.backends import OAuthStore
from notiontask.config import new_connection
from notiontask.hotkey import parse_hotkey
from notiontask.secretstore import protect


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def _make_dialog(app, title):
    win = tk.Toplevel(app.root)
    win.title(title)
    win.transient(app.root)
    win.attributes("-topmost", True)
    frame = ttk.Frame(win, padding=12)
    frame.pack(fill="both", expand=True)
    return win, frame


class SettingsDialog:
    def __init__(self, app):
        self.app = app
        self.cancel_login = threading.Event()
        cfg = app.cfg
        win, frame = _make_dialog(app, "設定")
        self.win = win
        win.resizable(False, False)
        frame.columnconfigure(1, weight=1)

        # ---- 接続 ----
        conn_box = ttk.LabelFrame(frame, text="接続（変更はすぐ反映されます）", padding=8)
        conn_box.grid(row=0, column=0, columnspan=3, sticky="ew")
        conn_box.columnconfigure(0, weight=1)
        self.conn_list = tk.Listbox(conn_box, height=4, activestyle="none", exportselection=False)
        self.conn_list.grid(row=0, column=0, rowspan=5, sticky="nsew")
        buttons = [("MCP で追加…", self.add_mcp), ("トークンで追加…", self.add_token),
                   ("ログイン", self.login_selected), ("名前変更", self.rename), ("削除", self.delete)]
        for i, (text, command) in enumerate(buttons):
            ttk.Button(conn_box, text=text, command=command).grid(row=i, column=1, sticky="ew", padx=(6, 0), pady=1)
        self.conn_status = ttk.Label(conn_box, text="", style="Status.TLabel", wraplength=380)
        self.conn_status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        hint = ttk.Label(conn_box, style="Status.TLabel", text=(
            "MCP: ブラウザで Notion にログインするだけ\n"
            "トークン: Internal Integration を使用（取得はここをクリック）"))
        hint.grid(row=6, column=0, columnspan=2, sticky="w")
        hint.bind("<Button-1>", lambda e: webbrowser.open("https://www.notion.so/my-integrations"))
        self.refresh_conn_list()

        # ---- その他 ----
        self.hotkey_var = tk.StringVar(value=cfg["hotkey"])
        self.alpha_var = tk.IntVar(value=round(float(cfg["alpha"]) * 100))
        self.open_var = tk.BooleanVar(value=cfg["open_after_create"])
        self.app_var = tk.BooleanVar(value=cfg["open_in_app"])

        ttk.Label(frame, text="ホットキー").grid(row=2, column=0, sticky="w", pady=(10, 2))
        ttk.Entry(frame, textvariable=self.hotkey_var).grid(row=2, column=1, sticky="ew", pady=(10, 2))
        ttk.Label(frame, text="例: Ctrl+Alt+N / Ctrl+Shift+Space / Win+F9", style="Status.TLabel").grid(
            row=3, column=1, sticky="w")

        ttk.Label(frame, text="不透明度").grid(row=4, column=0, sticky="w", pady=(8, 2))
        alpha_row = ttk.Frame(frame)
        alpha_row.grid(row=4, column=1, sticky="ew", pady=(8, 2))
        alpha_row.columnconfigure(0, weight=1)
        alpha_label = ttk.Label(alpha_row, width=5)
        alpha_label.grid(row=0, column=1)

        def on_alpha(_=None):
            value = self.alpha_var.get()
            alpha_label.configure(text=f"{value}%")
            app.root.attributes("-alpha", value / 100)  # プレビュー

        ttk.Scale(alpha_row, from_=30, to=100, variable=self.alpha_var,
                  command=lambda v: (self.alpha_var.set(round(float(v))), on_alpha())).grid(
            row=0, column=0, sticky="ew")
        on_alpha()

        ttk.Checkbutton(frame, text="作成後にページを開く", variable=self.open_var).grid(
            row=5, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(frame, text="ブラウザではなく Notion アプリで開く", variable=self.app_var).grid(
            row=6, column=1, sticky="w")

        _, errors = app.queue.counts()
        if errors:
            ttk.Button(frame, text=f"送信失敗した {errors} 件を破棄", command=self.clear_errors).grid(
                row=7, column=1, sticky="w", pady=(8, 0))

        self.error_label = ttk.Label(frame, text="", style="Error.TLabel")
        self.error_label.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        bottom = ttk.Frame(frame)
        bottom.grid(row=9, column=0, columnspan=3, sticky="e", pady=(8, 0))
        ttk.Button(bottom, text="キャンセル", command=self.cancel).pack(side="right")
        ttk.Button(bottom, text="保存", command=self.save).pack(side="right", padx=(0, 6))

        win.bind("<Escape>", lambda e: self.cancel())
        win.protocol("WM_DELETE_WINDOW", self.cancel)
        win.grab_set()
        win.focus_set()

    # ---- 接続の操作 ----

    def refresh_conn_list(self):
        self.conn_list.delete(0, "end")
        for conn in self.app.cfg["connections"]:
            if conn["type"] == "mcp":
                oauth = conn.get("oauth") or {}
                logged_in = bool(oauth.get("refresh_token") or oauth.get("access_token"))
                state = "要再ログイン" if conn["id"] in self.app.login_needed else \
                    ("ログイン済み" if logged_in else "未ログイン")
                text = f"{conn['label']}（MCP・{state}）"
            else:
                text = f"{conn['label']}（トークン）"
            self.conn_list.insert("end", text)

    def selected_conn(self):
        sel = self.conn_list.curselection()
        conns = self.app.cfg["connections"]
        if not sel or sel[0] >= len(conns):
            self.conn_status.configure(text="接続を選択してください", style="Error.TLabel")
            return None
        return conns[sel[0]]

    def _ask(self, title, prompt, **kwargs):
        return simpledialog.askstring(title, prompt, parent=self.win, **kwargs)

    def add_mcp(self):
        label = self._ask("MCP で追加", "接続の名前（例: メイン）", initialvalue="メイン")
        if not label or not label.strip():
            return
        conn = new_connection(label.strip(), "mcp")
        self.app.cfg["connections"].append(conn)
        self.app.connections_changed()
        self.refresh_conn_list()
        self.conn_list.selection_set(len(self.app.cfg["connections"]) - 1)
        self.login(conn)

    def add_token(self):
        label = self._ask("トークンで追加", "接続の名前（例: 個人）", initialvalue="個人")
        if not label or not label.strip():
            return
        token = self._ask("トークンで追加", "Internal Integration のトークン（ntn_…）", show="•")
        if not token or not token.strip():
            return
        conn = new_connection(label.strip(), "token")
        conn["token"] = protect(token.strip())
        self.app.cfg["connections"].append(conn)
        self.app.connections_changed()
        self.refresh_conn_list()
        self.app.refresh_data_sources()
        self.conn_status.configure(text=f"「{conn['label']}」を追加しました。共有された DB を取得しています",
                                   style="Status.TLabel")

    def login_selected(self):
        conn = self.selected_conn()
        if not conn:
            return
        if conn["type"] != "mcp":
            token = self._ask("トークンの変更", f"「{conn['label']}」の新しいトークン", show="•")
            if token and token.strip():
                conn["token"] = protect(token.strip())
                self.app.connections_changed(conn["id"])
                self.app.refresh_data_sources()
                self.conn_status.configure(text="トークンを更新しました", style="Status.TLabel")
            return
        self.login(conn)

    def login(self, conn):
        store = OAuthStore(self.app.cfg, conn)
        saved = store.get()
        base = {k: saved[k] for k in ("client_id", "redirect_uri") if saved.get(k)}
        self.cancel_login.clear()
        self.conn_status.configure(
            text="ブラウザが開きます。Notion にログインし、ワークスペースを選んで「許可」を押してください…",
            style="Status.TLabel")

        def done(oauth):
            store.set(oauth)
            self.app.connections_changed(conn["id"])
            self.app.flush_queue()
            if _alive(self.win):
                self.refresh_conn_list()
                self.conn_status.configure(text=f"「{conn['label']}」にログインしました。メイン画面の ＋ から DB を追加してください",
                                           style="Status.TLabel")

        def failed(e):
            if _alive(self.win):
                self.conn_status.configure(text=f"ログインに失敗: {e}", style="Error.TLabel")

        self.app.run_bg(lambda: notion_mcp.login(base, cancel_event=self.cancel_login), on_ok=done, on_err=failed)

    def rename(self):
        conn = self.selected_conn()
        if not conn:
            return
        label = self._ask("名前変更", "接続の名前", initialvalue=conn["label"])
        if label and label.strip():
            conn["label"] = label.strip()
            self.app.connections_changed()
            self.refresh_conn_list()

    def delete(self):
        conn = self.selected_conn()
        if not conn:
            return
        if not messagebox.askyesno("削除", f"接続「{conn['label']}」と、その DB の登録を削除しますか？", parent=self.win):
            return
        self.app.cfg["connections"].remove(conn)
        self.app.connections_changed(conn["id"])
        self.refresh_conn_list()

    # ---- その他 ----

    def clear_errors(self):
        self.app.queue.clear_errors()
        self.app.update_queue_label()

    def save(self):
        hotkey = self.hotkey_var.get().strip()
        try:
            parse_hotkey(hotkey)
        except ValueError as e:
            self.error_label.configure(text=f"ホットキー: {e}")
            return
        self.close()
        self.app.apply_new_settings({
            "hotkey": hotkey,
            "alpha": self.alpha_var.get() / 100,
            "open_after_create": self.open_var.get(),
            "open_in_app": self.app_var.get(),
        })

    def cancel(self):
        self.close()
        self.app._apply_settings()  # 不透明度のプレビューを戻す

    def close(self):
        self.cancel_login.set()
        self.win.destroy()


class DbManagerDialog:
    def __init__(self, app):
        self.app = app
        self.results = []
        win, frame = _make_dialog(app, "DB 管理")
        self.win = win
        win.geometry("460x480")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(5, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="接続").grid(row=0, column=0, sticky="w", padx=(0, 6))
        conns = app.cfg["connections"]
        self.conn_combo = ttk.Combobox(top, state="readonly", values=[
            f"{c['label']}（{'MCP' if c['type'] == 'mcp' else 'トークン'}）" for c in conns])
        self.conn_combo.grid(row=0, column=1, sticky="ew")
        current = app.current_entry()
        ids = [c["id"] for c in conns]
        self.conn_combo.current(ids.index(current["conn"]) if current and current["conn"] in ids else 0)

        search = ttk.Frame(frame)
        search.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        search.columnconfigure(0, weight=1)
        self.query_var = tk.StringVar()
        entry = ttk.Entry(search, textvariable=self.query_var)
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda e: self.search())
        ttk.Button(search, text="検索", command=self.search).grid(row=0, column=1, padx=(4, 0))
        ttk.Label(search, text="DB 名で検索、または DB の URL を貼り付けて Enter", style="Status.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w")

        self.result_list = self._listbox(frame, row=2)
        self.result_list.bind("<Double-Button-1>", lambda e: self.add_selected())
        row3 = ttk.Frame(frame)
        row3.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        row3.columnconfigure(1, weight=1)
        ttk.Button(row3, text="↓ 追加", command=self.add_selected).grid(row=0, column=0)
        self.status = ttk.Label(row3, text="", style="Status.TLabel", wraplength=340)
        self.status.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="登録済み（上から Ctrl+1〜9）").grid(row=4, column=0, sticky="w", pady=(10, 2))
        self.registered = self._listbox(frame, row=5)
        row6 = ttk.Frame(frame)
        row6.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(row6, text="↑", width=3, command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(row6, text="↓", width=3, command=lambda: self.move(1)).pack(side="left", padx=(4, 0))
        ttk.Button(row6, text="一覧から外す", command=self.remove).pack(side="left", padx=(4, 0))
        ttk.Button(row6, text="閉じる", command=win.destroy).pack(side="right")

        self.refresh_registered()
        win.bind("<Escape>", lambda e: win.destroy())
        win.grab_set()
        entry.focus_set()

    def _listbox(self, parent, row):
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        listbox = tk.Listbox(box, height=6, activestyle="none", exportselection=False)
        listbox.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, command=listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        listbox.configure(yscrollcommand=scroll.set)
        return listbox

    def conn(self):
        idx = self.conn_combo.current()
        conns = self.app.cfg["connections"]
        return conns[idx] if 0 <= idx < len(conns) else None

    def set_status(self, text, style="Status.TLabel"):
        if _alive(self.win):
            self.status.configure(text=text, style=style)

    def search(self):
        conn = self.conn()
        query = self.query_var.get().strip()
        if not conn:
            return
        if not query and conn["type"] == "mcp":
            self.set_status("キーワードか URL を入力してください", "Error.TLabel")
            return
        self.set_status("検索中…")
        conn_id = conn["id"]

        def done(items):
            if not _alive(self.win):
                return
            self.results = items
            self.result_list.delete(0, "end")
            for item in items:
                label = f"URL / ID: {item['title']}" if item["kind"] == "id" else item["title"]
                if item.get("path"):
                    label += f"   — {item['path']}"
                self.result_list.insert("end", label)
            self.set_status(f"{len(items)} 件見つかりました" if items else "見つかりませんでした")
            if items:
                self.result_list.selection_set(0)

        def failed(e):
            self.app._note_error(conn_id, e)
            self.set_status(f"検索に失敗: {e}", "Error.TLabel")

        self.app.run_bg(lambda: self.app.backend(conn_id).search(query), on_ok=done, on_err=failed)

    def add_selected(self):
        sel = self.result_list.curselection()
        conn = self.conn()
        if not sel or not conn or sel[0] >= len(self.results):
            self.set_status("追加する DB を選択してください", "Error.TLabel")
            return
        item = self.results[sel[0]]
        conn_id = conn["id"]
        self.set_status("DB を確認中…")

        def done(sources):
            added = self.app.add_data_sources(conn_id, sources)
            if _alive(self.win):
                self.refresh_registered()
                self.set_status(f"{added} 件追加しました" if added else "すでに登録済みです")

        self.app.run_bg(lambda: self.app.backend(conn_id).expand(item), on_ok=done,
                        on_err=lambda e: self.set_status(f"追加できませんでした: {e}", "Error.TLabel"))

    def refresh_registered(self, select=None):
        self.registered.delete(0, "end")
        for i, d in enumerate(self.app.cfg["data_sources"]):
            prefix = f"Ctrl+{i + 1}  " if i < 9 else "        "
            self.registered.insert("end", f"{prefix}{self.app.conn_label(d['conn'])}｜{d['title']}")
        if select is not None:
            self.registered.selection_set(select)
            self.registered.see(select)

    def move(self, delta):
        sel = self.registered.curselection()
        sources = self.app.cfg["data_sources"]
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if not 0 <= j < len(sources):
            return
        sources[i], sources[j] = sources[j], sources[i]
        self.app.cfg.save()
        self.app.fill_db_combo()
        self.refresh_registered(select=j)

    def remove(self):
        sel = self.registered.curselection()
        if not sel:
            return
        del self.app.cfg["data_sources"][sel[0]]
        self.app.cfg.save()
        self.app.fill_db_combo()
        self.refresh_registered()
