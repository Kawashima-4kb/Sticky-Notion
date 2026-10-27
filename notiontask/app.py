"""メインウィンドウ（Tkinter）。"""

import ctypes
import os
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import ttk

from notiontask import APP_NAME
from notiontask.backends import make_backend
from notiontask.config import Config
from notiontask.hotkey import GlobalHotkey
from notiontask.markdown import split_title
from notiontask.notion_api import NotionError
from notiontask.properties import build_properties, parse_date
from notiontask.sendqueue import SendQueue
from notiontask.tray import TrayIcon, make_icon_image

FONT_FAMILY = "Yu Gothic UI"
RETRY_INTERVAL_MS = 60_000
POLL_INTERVAL_MS = 50


def open_url(url, in_app=False):
    if in_app and url.startswith("https://"):
        url = "notion://" + url[len("https://"):]
    if sys.platform == "win32":
        os.startfile(url)
    else:
        webbrowser.open(url)


def clamp_geometry(geometry):
    """保存位置が現在の画面外なら位置を捨ててサイズだけ使う（モニタ構成の変更対策）。"""
    m = re.fullmatch(r"(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)", geometry or "")
    if not m:
        return geometry
    if sys.platform != "win32":
        return geometry
    w, h, x, y = int(m[1]), int(m[2]), int(m[3]), int(m[4])
    gsm = ctypes.windll.user32.GetSystemMetrics
    vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)  # 仮想スクリーン全体
    if x + w < vx + 40 or x > vx + vw - 40 or y < vy or y > vy + vh - 40:
        return f"{w}x{h}"
    return geometry


def ds_key(entry):
    return f"{entry['conn']}/{entry['id']}"


class App:
    def __init__(self):
        if sys.platform == "win32":
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.cfg = Config()
        self.queue = SendQueue()
        self._ui_calls = queue.Queue()
        self._backends = {}
        self._backend_lock = threading.Lock()
        self.schemas = {}  # ds_key → 正規化スキーマ
        self.fields = []
        self.field_vars = {}
        self.sending = False
        self.last_url = None
        self.hotkey = None
        self.quitting = False
        self.login_needed = set()  # 再ログインが必要な接続 ID

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        if TrayIcon.available:
            from PIL import ImageTk
            self._icon = ImageTk.PhotoImage(make_icon_image(32))
            self.root.iconphoto(True, self._icon)
        self._setup_style()
        self._build()
        self._apply_settings()
        self.root.geometry(clamp_geometry(self.cfg["geometry"]) or "420x360")
        self.root.minsize(300, 220)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.tray = TrayIcon(APP_NAME, on_show=lambda: self.post(self.show),
                             on_settings=lambda: self.post(self.open_settings),
                             on_quit=lambda: self.post(self.quit))
        self.tray.start()
        self._start_hotkey()

        self.fill_db_combo()
        self.root.after(POLL_INTERVAL_MS, self._poll)
        self.root.after(RETRY_INTERVAL_MS, self._retry_tick)
        self.update_queue_label()
        if self.cfg["connections"]:
            self.refresh_data_sources(quiet=True)
            self.flush_queue()
        else:
            self.root.after(200, self.open_settings)
        self.show()

    # ---- スレッド間連携 ---------------------------------------------------

    def post(self, fn):
        """別スレッドから UI スレッドで fn を実行させる。"""
        self._ui_calls.put(fn)

    def _poll(self):
        try:
            while True:
                self._ui_calls.get_nowait()()
        except queue.Empty:
            pass
        if not self.quitting:
            self.root.after(POLL_INTERVAL_MS, self._poll)

    def run_bg(self, fn, on_ok=None, on_err=None):
        def worker():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 - UI に表示するため全例外を受ける
                if on_err:
                    self.post(lambda e=e: on_err(e))
                return
            if on_ok:
                self.post(lambda: on_ok(result))
        threading.Thread(target=worker, daemon=True).start()

    # ---- 接続 -----------------------------------------------------------

    def backend(self, conn_id):
        """接続 ID に対応する backend（キャッシュ）。どのスレッドから呼んでもよい。"""
        with self._backend_lock:
            if conn_id not in self._backends:
                conn = self.cfg.connection(conn_id)
                if conn is None:
                    raise NotionError("接続が削除されています")
                self._backends[conn_id] = make_backend(self.cfg, conn)
            return self._backends[conn_id]

    def connections_changed(self, conn_id=None):
        """接続の追加・削除・ログイン後に呼ぶ。"""
        with self._backend_lock:
            if conn_id:
                self._backends.pop(conn_id, None)
            else:
                self._backends.clear()
        ids = {c["id"] for c in self.cfg["connections"]}
        self.cfg["data_sources"] = [d for d in self.cfg["data_sources"] if d["conn"] in ids]
        self.login_needed &= ids
        if conn_id:
            self.login_needed.discard(conn_id)
        self.cfg.save()
        self.fill_db_combo()
        self.update_queue_label()

    def conn_label(self, conn_id):
        conn = self.cfg.connection(conn_id)
        return conn["label"] if conn else "?"

    def _note_error(self, conn_id, error):
        if isinstance(error, NotionError) and error.needs_login:
            self.login_needed.add(conn_id)
            self.update_queue_label()

    # ---- 画面構築 -------------------------------------------------------

    def _setup_style(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=FONT_FAMILY, size=10)
        style = ttk.Style(self.root)
        style.configure("Status.TLabel", foreground="#555")
        style.configure("Link.TLabel", foreground="#0b6bcb")
        style.configure("Error.TLabel", foreground="#c62828")
        style.configure("Queue.TLabel", foreground="#b26a00")

    def _build(self):
        root = self.root
        frame = ttk.Frame(root, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.db_var = tk.StringVar()
        self.db_combo = ttk.Combobox(top, textvariable=self.db_var, state="readonly")
        self.db_combo.grid(row=0, column=0, sticky="ew")
        self.db_combo.bind("<<ComboboxSelected>>", lambda e: self.on_db_selected())
        ttk.Button(top, text="＋", width=3, command=self.open_db_manager).grid(row=0, column=1, padx=(4, 0))
        ttk.Button(top, text="↻", width=3, command=self.refresh_data_sources).grid(row=0, column=2, padx=(4, 0))
        self.topmost_var = tk.BooleanVar(value=self.cfg["topmost"])
        ttk.Checkbutton(top, text="📌", style="Toolbutton", variable=self.topmost_var,
                        command=self.toggle_topmost).grid(row=0, column=3, padx=(4, 0))
        ttk.Button(top, text="⚙", width=3, command=self.open_settings).grid(row=0, column=4, padx=(4, 0))

        self.title_var = tk.StringVar()
        self.title_entry = ttk.Entry(frame, textvariable=self.title_var)
        self.title_entry.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        self.title_entry.bind("<Return>", lambda e: self.body_text.focus_set())

        body_frame = ttk.Frame(frame)
        body_frame.grid(row=2, column=0, sticky="nsew")
        body_frame.columnconfigure(0, weight=1)
        body_frame.rowconfigure(0, weight=1)
        self.body_text = tk.Text(body_frame, height=6, wrap="word", undo=True,
                                 font=(FONT_FAMILY, 10), relief="solid", borderwidth=1)
        self.body_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body_frame, command=self.body_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.body_text.configure(yscrollcommand=scroll.set)
        # Text の Tab はフォーカス移動に使う
        self.body_text.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])

        self.props_toggle = ttk.Button(frame, command=self.toggle_properties, style="Toolbutton")
        self.props_toggle.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.props_frame = ttk.Frame(frame)
        self.props_frame.columnconfigure(1, weight=1)
        self._layout_properties()

        bottom = ttk.Frame(frame)
        bottom.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(bottom, text="", style="Status.TLabel", anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.status_label.bind("<Button-1>", lambda e: self.last_url and open_url(
            self.last_url, self.cfg["open_in_app"]))
        self.queue_label = ttk.Label(bottom, text="", style="Queue.TLabel")
        self.queue_label.grid(row=0, column=1, padx=(4, 4))
        self.create_button = ttk.Button(bottom, text="作成 (Ctrl+Enter)", command=self.submit)
        self.create_button.grid(row=0, column=2)

        for widget in (self.title_entry, self.body_text):
            widget.bind("<Control-Return>", lambda e: (self.submit(), "break")[1])
        root.bind("<Escape>", lambda e: self.on_close())
        for n in range(1, 10):
            root.bind(f"<Control-Key-{n}>", lambda e, i=n - 1: (self.select_db_index(i), "break")[1])

    def _layout_properties(self):
        shown = self.cfg["show_properties"]
        self.props_toggle.configure(text=("▾" if shown else "▸") + " プロパティ")
        if shown:
            self.props_frame.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        else:
            self.props_frame.grid_remove()

    def toggle_properties(self):
        self.cfg["show_properties"] = not self.cfg["show_properties"]
        self._layout_properties()
        self.cfg.save()

    def _build_property_fields(self, fields, message=None):
        for child in self.props_frame.winfo_children():
            child.destroy()
        self.fields = fields
        self.field_vars = {}
        if not fields:
            ttk.Label(self.props_frame, text=message or "入力できるプロパティはありません",
                      style="Status.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
            return
        for row, field in enumerate(fields):
            ttk.Label(self.props_frame, text=field["name"]).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=1)
            ptype = field["type"]
            if ptype == "checkbox":
                var = tk.BooleanVar()
                ttk.Checkbutton(self.props_frame, variable=var).grid(row=row, column=1, sticky="w")
            elif ptype in ("status", "select"):
                var = tk.StringVar()
                ttk.Combobox(self.props_frame, textvariable=var, state="readonly",
                             values=[""] + field["options"]).grid(row=row, column=1, sticky="ew", pady=1)
            elif ptype == "multi_select":
                var = tk.StringVar()
                ttk.Combobox(self.props_frame, textvariable=var, values=field["options"]).grid(
                    row=row, column=1, sticky="ew", pady=1)
            else:  # date
                var = tk.StringVar()
                cell = ttk.Frame(self.props_frame)
                cell.grid(row=row, column=1, sticky="ew", pady=1)
                cell.columnconfigure(0, weight=1)
                ttk.Entry(cell, textvariable=var).grid(row=0, column=0, sticky="ew")
                ttk.Button(cell, text="今日", width=4, command=lambda v=var: v.set(parse_date("今日"))).grid(
                    row=0, column=1, padx=(4, 0))
                ttk.Button(cell, text="明日", width=4, command=lambda v=var: v.set(parse_date("明日"))).grid(
                    row=0, column=2, padx=(2, 0))
            self.field_vars[field["name"]] = var

    def _clear_inputs(self):
        self.title_var.set("")
        self.body_text.delete("1.0", "end")
        self.body_text.edit_reset()
        for var in self.field_vars.values():
            var.set(False if isinstance(var, tk.BooleanVar) else "")

    # ---- 表示制御 -------------------------------------------------------

    def _apply_settings(self):
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self.root.attributes("-alpha", max(0.3, min(1.0, float(self.cfg["alpha"]))))

    def toggle_topmost(self):
        self.cfg["topmost"] = self.topmost_var.get()
        self._apply_settings()
        self.cfg.save()

    def show(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        if sys.platform == "win32":
            ctypes.windll.user32.SetForegroundWindow(int(self.root.wm_frame(), 16))
        self.root.focus_force()
        self.title_entry.focus_set()
        if not self.cfg["topmost"]:
            self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def _save_window_state(self):
        if self.root.state() == "normal":
            self.cfg["geometry"] = self.root.geometry()
        self.cfg.save()

    def on_close(self):
        if TrayIcon.available:
            self._save_window_state()
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self.quitting = True
        self._save_window_state()
        if self.hotkey:
            self.hotkey.stop()
        self.tray.stop()
        self.root.destroy()

    def set_status(self, text, style="Status.TLabel", url=None):
        self.last_url = url
        self.status_label.configure(text=text, style=style, cursor="hand2" if url else "")

    def update_queue_label(self):
        pending, errors = self.queue.counts()
        parts = []
        if self.login_needed:
            parts.append("要ログイン: " + "・".join(self.conn_label(c) for c in sorted(self.login_needed)))
        if pending:
            parts.append(f"未送信 {pending}件")
        if errors:
            parts.append(f"失敗 {errors}件")
        self.queue_label.configure(text=" / ".join(parts))

    # ---- ホットキー -----------------------------------------------------

    def _start_hotkey(self):
        if self.hotkey:
            self.hotkey.stop()
        try:
            self.hotkey = GlobalHotkey(self.cfg["hotkey"], lambda: self.post(self.show))
            if not self.hotkey.start():
                self.set_status(self.hotkey.error, "Error.TLabel")
        except ValueError as e:
            self.hotkey = None
            self.set_status(f"ホットキー設定エラー: {e}", "Error.TLabel")

    # ---- データベース ---------------------------------------------------

    def fill_db_combo(self):
        sources = self.cfg["data_sources"]
        multi = len({d["conn"] for d in sources}) > 1 or len(self.cfg["connections"]) > 1
        self.db_combo.configure(values=[
            f"{self.conn_label(d['conn'])}｜{d['title']}" if multi else d["title"] for d in sources])
        keys = [ds_key(d) for d in sources]
        last = self.cfg["last_data_source"]
        if last in keys:
            self.db_combo.current(keys.index(last))
        elif keys:
            self.db_combo.current(0)
        else:
            self.db_var.set("")
        self.on_db_selected(save=False)

    def current_entry(self):
        idx = self.db_combo.current()
        sources = self.cfg["data_sources"]
        return sources[idx] if 0 <= idx < len(sources) else None

    def select_db_index(self, index):
        if index < len(self.cfg["data_sources"]):
            self.db_combo.current(index)
            self.on_db_selected()

    def on_db_selected(self, save=True):
        entry = self.current_entry()
        if not entry:
            message = "⚙ から接続を追加してください" if not self.cfg["connections"] else "＋ から DB を追加してください"
            self._build_property_fields([], message)
            return
        key = ds_key(entry)
        if save:
            self.cfg["last_data_source"] = key
            self.cfg.save()
        if key in self.schemas:
            self._build_property_fields(self.schemas[key]["fields"])
            return
        self._build_property_fields([], "読み込み中…")
        conn_id, ds_id = entry["conn"], entry["id"]

        def failed(e):
            self._note_error(conn_id, e)
            self._build_property_fields([], "プロパティを取得できませんでした")
            self.set_status(f"DB 情報の取得に失敗: {e}", "Error.TLabel")

        self.run_bg(lambda: self.backend(conn_id).get_schema(ds_id),
                    on_ok=lambda schema: self._on_schema(entry, schema), on_err=failed)

    def _on_schema(self, entry, schema):
        key = ds_key(entry)
        self.schemas[key] = schema
        self.remember_title_prop(entry["conn"], entry["id"], schema["title_prop"])
        current = self.current_entry()
        if current and ds_key(current) == key:
            self._build_property_fields(schema["fields"])

    def remember_title_prop(self, conn_id, ds_id, name):
        for d in self.cfg["data_sources"]:
            if d["conn"] == conn_id and d["id"] == ds_id and d.get("title_prop") != name:
                d["title_prop"] = name
                self.cfg.save()

    def add_data_sources(self, conn_id, items):
        """DB 管理画面から登録する。追加した件数を返す。"""
        existing = {ds_key(d) for d in self.cfg["data_sources"]}
        added = 0
        for item in items:
            entry = {"conn": conn_id, "id": item["id"], "title": item["title"]}
            if ds_key(entry) not in existing:
                self.cfg["data_sources"].append(entry)
                existing.add(ds_key(entry))
                added += 1
        self.cfg.save()
        self.fill_db_combo()
        return added

    def refresh_data_sources(self, quiet=False):
        """トークン接続は共有 DB 一覧を取り直す。あわせてプロパティのキャッシュも捨てる。"""
        self.schemas.clear()
        self.on_db_selected(save=False)
        token_conns = [c for c in self.cfg["connections"] if c["type"] == "token"]
        if not self.cfg["connections"]:
            self.set_status("⚙ から接続を追加してください", "Error.TLabel")
            return
        if not token_conns:
            if not quiet:
                self.set_status("プロパティを再読み込みしました（MCP 接続の DB は ＋ から追加）")
            return
        if not quiet:
            self.set_status("DB 一覧を取得中…")
        for conn in token_conns:
            conn_id = conn["id"]

            def done(items, conn_id=conn_id):
                # 並び順は保ったまま、共有が外れた DB は除き、新しく共有された DB は末尾に足す
                fetched = {item["id"]: item for item in items}
                merged = []
                for d in self.cfg["data_sources"]:
                    if d["conn"] != conn_id:
                        merged.append(d)
                    elif d["id"] in fetched:
                        item = fetched.pop(d["id"])
                        d["title"] = item["title"]
                        if item.get("title_prop"):
                            d["title_prop"] = item["title_prop"]
                        merged.append(d)
                for item in fetched.values():
                    entry = {"conn": conn_id, "id": item["id"], "title": item["title"]}
                    if item.get("title_prop"):
                        entry["title_prop"] = item["title_prop"]
                    merged.append(entry)
                self.cfg["data_sources"] = merged
                self.cfg.save()
                self.fill_db_combo()
                if not quiet:
                    self.set_status(f"「{self.conn_label(conn_id)}」の DB {len(items)} 件を取得しました")

            def failed(e, conn_id=conn_id):
                self.set_status(f"「{self.conn_label(conn_id)}」の DB 一覧の取得に失敗: {e}", "Error.TLabel")

            self.run_bg(lambda conn_id=conn_id: self.backend(conn_id).list_all(), on_ok=done, on_err=failed)

    # ---- 送信 -----------------------------------------------------------

    def send_request(self, request):
        """ページ作成（どのスレッドからでも可）。作成したページの URL を返す。"""
        backend = self.backend(request["conn"])
        title_prop = request.get("title_prop")
        if not title_prop:
            title_prop = backend.get_schema(request["data_source_id"])["title_prop"]
            request["title_prop"] = title_prop
            self.post(lambda: self.remember_title_prop(request["conn"], request["data_source_id"], title_prop))
        return backend.create_page(request["data_source_id"], title_prop, request["title"],
                                   request["fields"], request["values"], request["body"])

    def submit(self):
        if self.sending:
            return
        entry = self.current_entry()
        if not entry:
            if not self.cfg["connections"]:
                self.open_settings()
            else:
                self.set_status("投稿先の DB を選択してください（＋ で追加）", "Error.TLabel")
            return
        title, body = split_title(self.title_var.get(), self.body_text.get("1.0", "end"))
        if not title:
            self.set_status("タイトルか本文を入力してください", "Error.TLabel")
            self.title_entry.focus_set()
            return
        values = {name: var.get() for name, var in self.field_vars.items()}
        try:
            build_properties("", title, self.fields, values)  # 入力値の検証だけ先に行う
        except ValueError:
            self.set_status("日付は YYYY-MM-DD 形式で入力してください", "Error.TLabel")
            return
        schema = self.schemas.get(ds_key(entry))
        request = {"conn": entry["conn"], "data_source_id": entry["id"],
                   "title_prop": (schema or {}).get("title_prop") or entry.get("title_prop"),
                   "title": title, "fields": list(self.fields), "values": values, "body": body}

        self.sending = True
        self.create_button.state(["disabled"])
        self.set_status("送信中…")

        def work():
            try:
                return "sent", self.send_request(request)
            except NotionError as e:
                if not e.retryable:
                    raise
                self.queue.add(request)
                return "queued", e

        self.run_bg(work, on_ok=self._on_submitted, on_err=self._on_submit_error)

    def _on_submitted(self, result):
        kind, value = result
        self.sending = False
        self.create_button.state(["!disabled"])
        self._clear_inputs()
        self.title_entry.focus_set()
        if kind == "sent":
            if value:
                self.set_status("✓ 作成しました（クリックで開く）", "Link.TLabel", value)
                if self.cfg["open_after_create"]:
                    open_url(value, self.cfg["open_in_app"])
            else:
                self.set_status("✓ 作成しました")
            self.flush_queue()
        else:
            entry = self.current_entry()
            if entry:
                self._note_error(entry["conn"], value)
            self.set_status(f"送信できないためキューに保存しました（{value}）", "Queue.TLabel")
        self.update_queue_label()

    def _on_submit_error(self, error):
        self.sending = False
        self.create_button.state(["!disabled"])
        self.set_status(f"作成に失敗: {error}", "Error.TLabel")

    def flush_queue(self):
        if not self.queue.counts()[0]:
            return
        failures = []

        def send(item):
            try:
                self.send_request(item)
            except NotionError as e:
                failures.append((item.get("conn"), e))
                raise

        def done(sent):
            for conn_id, e in failures:
                self._note_error(conn_id, e)
            self.update_queue_label()
            if sent:
                self.set_status(f"キューから {sent} 件送信しました")

        self.run_bg(lambda: self.queue.flush(send), on_ok=done, on_err=lambda e: self.update_queue_label())

    def _retry_tick(self):
        if self.quitting:
            return
        self.flush_queue()
        self.root.after(RETRY_INTERVAL_MS, self._retry_tick)

    # ---- 設定 -----------------------------------------------------------

    def open_settings(self):
        from notiontask.dialogs import SettingsDialog
        SettingsDialog(self)

    def open_db_manager(self):
        if not self.cfg["connections"]:
            self.open_settings()
            return
        from notiontask.dialogs import DbManagerDialog
        DbManagerDialog(self)

    def apply_new_settings(self, values):
        hotkey_changed = values["hotkey"] != self.cfg["hotkey"]
        for key, value in values.items():
            self.cfg[key] = value
        self.cfg.save()
        self._apply_settings()
        if hotkey_changed:
            self._start_hotkey()

    def run(self):
        self.root.mainloop()


def main():
    App().run()
