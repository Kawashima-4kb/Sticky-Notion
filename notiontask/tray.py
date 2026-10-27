"""タスクトレイ常駐（pystray）。pystray が無い環境では TrayIcon.available が False になる。"""

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - 依存が無い環境用
    pystray = None


def make_icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 6,
                           fill=(255, 255, 255, 255), outline=(30, 30, 30, 255), width=size // 16)
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
    except OSError:
        font = ImageFont.load_default()
    draw.text((size / 2, size / 2), "N", fill=(30, 30, 30, 255), font=font, anchor="mm")
    return img


class TrayIcon:
    """メニュー操作は on_show / on_settings / on_quit（トレイのスレッドから呼ばれる）に渡す。"""

    available = pystray is not None

    def __init__(self, title, on_show, on_settings, on_quit):
        self._icon = None
        if not self.available:
            return
        menu = pystray.Menu(
            pystray.MenuItem("表示", lambda: on_show(), default=True),
            pystray.MenuItem("設定", lambda: on_settings()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", lambda: on_quit()),
        )
        self._icon = pystray.Icon(title, make_icon_image(), title, menu)

    def start(self):
        if self._icon:
            self._icon.run_detached()

    def stop(self):
        if self._icon:
            self._icon.stop()
