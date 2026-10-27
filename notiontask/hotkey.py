"""Win32 RegisterHotKey を使ったグローバルホットキー（追加ライブラリ不要）。"""

import ctypes
import sys
import threading
from ctypes import wintypes

MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 1

_NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
               "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
               "pageup": 0x21, "pagedown": 0x22}


def parse_hotkey(text):
    """'Ctrl+Alt+N' → (modifiers, virtual key code)。不正なら ValueError。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("修飾キー（Ctrl/Alt/Shift/Win）と通常キーを組み合わせてください")
    mods = 0
    for p in parts[:-1]:
        if p not in MODIFIERS:
            raise ValueError(f"不明な修飾キー: {p}")
        mods |= MODIFIERS[p]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"不明なキー: {parts[-1]}")
    return mods, vk


class GlobalHotkey:
    """専用スレッドでホットキーを登録し、押されたら callback を（そのスレッドから）呼ぶ。"""

    def __init__(self, hotkey, callback):
        self.hotkey = hotkey
        self.callback = callback
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self.error = None

    def start(self):
        """登録に成功したら True。失敗時は self.error に理由が入る。"""
        if sys.platform != "win32":
            self.error = "Windows 以外ではホットキーは使えません"
            return False
        mods, vk = parse_hotkey(self.hotkey)
        self.error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, args=(mods, vk), daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self.error is None

    def _run(self, mods, vk):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            self.error = f"{self.hotkey} は他のアプリで使用中のため登録できません"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if self._thread and self._thread.is_alive() and self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread = None
