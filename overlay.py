# -*- coding: utf-8 -*-
"""
FishingBot Overlay
------------------
A tiny always-on-top floating window that lives above your game without
stealing focus.

Modes
-----
- info   (default): semi-transparent, mouse-click-through
                   (WS_EX_TRANSPARENT). Won't disturb your game at all.
- control:         opaque, shows Start / Stop / Quit buttons.

Switching
---------
- Right-click anywhere on the window
- Global hotkey: Ctrl+Alt+O
- Esc key (in control mode -> back to info)

Quit
----
- Global hotkey: Ctrl+Alt+X
- Click "退出" in control mode

If the game runs in *exclusive fullscreen* (not windowed-fullscreen /
borderless), no overlay can appear above it on Windows. Set the game
to "无边框" / "窗口化全屏" if needed.
"""

import argparse
import ctypes
import json
import os
import threading
import time
import urllib.request

import win32api
import win32con
import win32gui


# ---------------------------- Win32 常量 ----------------------------
GWL_EXSTYLE = -20
WS_EX_TOPMOST     = 0x00000008
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_NOACTIVATE  = 0x08000000
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_POPUP          = 0x80000000

LWA_ALPHA = 0x02

WM_PAINT       = 0x000F
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP   = 0x0202
WM_MOUSEMOVE   = 0x0200
WM_RBUTTONUP   = 0x0205
WM_DESTROY     = 0x0002

IDT_REDRAW = 1
IDT_POLL = 2

# SetTimer/ KillTimer 不在 pywin32.win32gui 里，自己调 user32
_SetTimer = ctypes.windll.user32.SetTimer
_SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
_SetTimer.restype = ctypes.c_void_p
_KillTimer = ctypes.windll.user32.KillTimer
_KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint]

# GDI 文本 - 用 ctypes 直接调（pywin32 的 DrawText 签名不稳）
from ctypes import wintypes as _wt

_DrawTextW = ctypes.windll.user32.DrawTextW
_DrawTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
                        ctypes.POINTER(_wt.RECT), ctypes.c_uint]
_DrawTextW.restype = ctypes.c_int

_GetTextExtentPoint32W = ctypes.windll.gdi32.GetTextExtentPoint32W
_GetTextExtentPoint32W.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                   ctypes.c_int, ctypes.POINTER(_wt.SIZE)]
_GetTextExtentPoint32W.restype = ctypes.c_int


def _rgb(r, g, b):
    """转换成 Windows GDI 的 COLORREF (0x00BBGGRR)。"""
    return (b << 16) | (g << 8) | r


# 主题色
C_BG      = _rgb(0x1A, 0x1A, 0x22)
C_BTN_BG  = _rgb(0x2A, 0x2A, 0x33)
C_GREEN   = _rgb(0x3F, 0xD0, 0x5B)
C_RED     = _rgb(0xE0, 0x55, 0x55)
C_GREY    = _rgb(0x88, 0x88, 0x88)
C_TEXT    = _rgb(0xDD, 0xDD, 0xDD)

APP_TOGGLE_KEY = 0x400 + 1   # 自定义: 切换模式
APP_QUIT_KEY   = 0x400 + 2   # 自定义: 退出

API = "http://127.0.0.1:8765"   # 默认 webui 端口
POLL_INTERVAL = 0.4             # API 轮询间隔（秒）

# 窗口位置持久化（信息模式鼠标穿透所以不能拖，需切控制模式拖，位置记下来）
POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "overlay_pos.json")

# 全局句柄 -> Overlay 实例映射（WNDPROC 是全局函数，必须这样 dispatch）
_WINDOW_INSTANCES = {}


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,    # LRESULT
    ctypes.c_void_p,  # HWND
    ctypes.c_uint,    # uMsg
    ctypes.c_void_p,  # wParam (raw)
    ctypes.c_void_p,  # lParam (raw)
)


@WNDPROC
def _global_wnd_proc(hwnd, msg, wp, lp):
    inst = _WINDOW_INSTANCES.get(hwnd)
    if inst is None:
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)
    return inst._wnd_proc(hwnd, msg, wp, lp)


# ---------------------------- 网络 ----------------------------
def api_get(path):
    with urllib.request.urlopen(API + path, timeout=1.0) as r:
        return json.load(r)


def api_post(path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body or {}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return json.load(r)


# --------------------------- GDI 字体缓存 ---------------------------
class FontCache:
    """避免每帧 CreateFontIndirect / DeleteObject 造成 GDI handle 泄漏。"""

    def __init__(self):
        self._fonts = {}

    def get(self, size, bold=False, face="Microsoft YaHei UI"):
        key = (size, bool(bold), face)
        if key not in self._fonts:
            lf = win32gui.LOGFONT()
            lf.lfHeight = -abs(size)
            lf.lfWeight = win32con.FW_BOLD if bold else win32con.FW_NORMAL
            lf.lfFaceName = face
            lf.lfQuality = win32con.CLEARTYPE_QUALITY
            self._fonts[key] = win32gui.CreateFontIndirect(lf)
        return self._fonts[key]


# ------------------------------- 窗口 -------------------------------
class Overlay:

    W, H_INFO, H_CTRL = 320, 74, 104
    PAD = 10

    def __init__(self):
        self.mode = "info"
        self.data = {"running": False, "count": 0,
                     "score": 0.0, "peak": 0.0, "level": 0.0,
                     "last": None, "error": None,
                     "rec": {"state": "idle"}}
        self.fonts = FontCache()

        # 拖动
        self.dragging = False
        self.drag_off = (0, 0)

        # 位置：优先读上次保存的
        self.win_x, self.win_y = self._load_pos()

        # 控制按钮区
        self.buttons = []

        self._register_window_class()
        self._create_window()
        self._apply_mode(self.mode)
        self._install_timers()

        # 后台轮询线程
        threading.Thread(target=self._poll_loop, daemon=True).start()

        # 全局热键（可选）
        self._register_hotkeys()

    # ---- 窗口类 ----
    def _load_pos(self):
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            x, y = int(d["x"]), int(d["y"])
            sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            if 0 <= x <= sw - 100 and 0 <= y <= sh - 40:
                return x, y
        except Exception:
            pass
        # 默认右上角
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        return max(0, sw - self.W - 40), 40

    def _save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.win_x, "y": self.win_y}, f)
        except Exception:
            pass

    def _register_window_class(self):
        # pywin32 不暴露 WNDCLASSEX，用 ctypes 自己定义并调 RegisterClassExW
        class WNDCLASSEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p),
                ("hIconSm", ctypes.c_void_p),
            ]
        WNDCLASSEXEX = WNDCLASSEX
        WCE = WNDCLASSEXEX()
        WCE.cbSize = ctypes.sizeof(WCE)
        WCE.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        WCE.lpfnWndProc = ctypes.cast(_global_wnd_proc, ctypes.c_void_p).value
        WCE.cbClsExtra = 0
        WCE.cbWndExtra = 0
        WCE.hInstance = win32api.GetModuleHandle(None)
        WCE.hIcon = 0
        WCE.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        WCE.hbrBackground = win32con.COLOR_WINDOW + 1
        WCE.lpszMenuName = None
        WCE.lpszClassName = "FishingBotOverlay"
        WCE.hIconSm = 0
        if not ctypes.windll.user32.RegisterClassExW(ctypes.byref(WCE)):
            err = ctypes.GetLastError()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise OSError(f"RegisterClassExW failed: {err}")

    # ---- 创建窗口 ----
    def _create_window(self):
        self.hwnd = win32gui.CreateWindowEx(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
            "FishingBotOverlay",
            "FishingBot",
            WS_POPUP,
            self.win_x, self.win_y, self.W, self.H_INFO,
            0, 0, win32gui.GetModuleHandle(None), None)
        _WINDOW_INSTANCES[self.hwnd] = self
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)

    # ---- 定时器 ----
    def _install_timers(self):
        _SetTimer(self.hwnd, IDT_REDRAW, 33, 0)        # ~30 fps 重绘
        _SetTimer(self.hwnd, IDT_POLL,
                  int(POLL_INTERVAL * 1000), 0)

    # ---- 后台轮询 ----
    def _poll_loop(self):
        while True:
            try:
                new = api_get("/api/status").get("state", {})
                if isinstance(new, dict):
                    self.data = new
            except Exception as e:
                if not self.data.get("count") and not self.data.get("running"):
                    self.data = {"error": str(e)[:120]}
            time.sleep(POLL_INTERVAL)

    # ---- 全局热键 ----
    def _register_hotkeys(self):
        try:
            import keyboard
            hwnd = self.hwnd
            keyboard.add_hotkey("ctrl+alt+o", lambda:
                ctypes.windll.user32.PostMessageW(hwnd, APP_TOGGLE_KEY, 0, 0))
            keyboard.add_hotkey("ctrl+alt+x", lambda:
                ctypes.windll.user32.PostMessageW(hwnd, APP_QUIT_KEY, 0, 0))
        except Exception:
            pass

    # ---- 模式切换 ----
    def _apply_mode(self, mode):
        self.mode = mode
        cur = win32gui.GetWindowLong(self.hwnd, GWL_EXSTYLE)
        if mode == "info":
            new = cur | WS_EX_TRANSPARENT
            alpha = 178
            height = self.H_INFO
        else:
            new = cur & ~WS_EX_TRANSPARENT
            alpha = 245
            height = self.H_CTRL
        if new != cur:
            win32gui.SetWindowLong(self.hwnd, GWL_EXSTYLE, new)
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, alpha, LWA_ALPHA)
        win32gui.SetWindowPos(self.hwnd, 0, self.win_x, self.win_y,
                              self.W, height,
                              win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
        self._layout_buttons()
        win32gui.InvalidateRect(self.hwnd, None, True)

    def toggle_mode(self):
        self._apply_mode("control" if self.mode == "info" else "info")

    def quit_app(self):
        win32gui.PostMessage(self.hwnd, WM_DESTROY, 0, 0)

    # ---- 控制模式按钮布局 ----
    def _layout_buttons(self):
        if self.mode != "control":
            self.buttons = []
            return
        y1, y2 = 54, 96
        gap = 8
        n = 3
        bw = (self.W - self.PAD * 2 - gap * (n - 1)) // n
        specs = [
            ("▶ 启动", C_GREEN, "start"),
            ("■ 停止", C_RED, "stop"),
            ("✕ 退出", C_GREY, "quit"),
        ]
        self.buttons = []
        for i, (label, fg, key) in enumerate(specs):
            x1 = self.PAD + i * (bw + gap)
            x2 = x1 + bw
            self.buttons.append({
                "label": label, "fg": fg, "action": key,
                "rect": (x1, y1, x2, y2),
            })

    def _hit_button(self, x, y):
        for b in self.buttons:
            x1, y1, x2, y2 = b["rect"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return b
        return None

    def _do_action(self, key):
        try:
            if key == "start":
                api_post("/api/start", {})
            elif key == "stop":
                api_post("/api/stop", {})
            elif key == "quit":
                self.quit_app()
        except Exception as e:
            self.data = {"error": str(e)[:120]}

    # ---- 窗口过程 ----
    def _wnd_proc(self, hwnd, msg, wp, lp):
        if msg == WM_PAINT:
            self._draw(hwnd)
            return 0

        if msg == WM_LBUTTONDOWN:
            x = win32api.LOWORD(lp); y = win32api.HIWORD(lp)
            b = self._hit_button(x, y) if self.mode == "control" else None
            if b:
                self._do_action(b["action"])
                return 0
            # 进入拖动
            self.dragging = True
            rx, ry = win32gui.GetWindowRect(hwnd)[:2]
            self.win_x, self.win_y = rx, ry
            self.drag_off = (rx - x, ry - y)
            return 0

        if msg == WM_MOUSEMOVE:
            if self.dragging:
                x = win32api.LOWORD(lp); y = win32api.HIWORD(lp)
                new_x = x + self.drag_off[0]
                new_y = y + self.drag_off[1]
                self.win_x, self.win_y = new_x, new_y
                win32gui.SetWindowPos(hwnd, 0, new_x, new_y, 0, 0,
                                      win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
            return 0

        if msg == WM_LBUTTONUP:
            if self.dragging:
                self.dragging = False
                self._save_pos()
            return 0

        if msg == WM_RBUTTONUP:
            self.toggle_mode()
            return 0

        if msg in (win32con.WM_KEYDOWN,):
            if wp == win32con.VK_ESCAPE and self.mode == "control":
                self._apply_mode("info")
                return 0

        if msg == APP_TOGGLE_KEY:
            self.toggle_mode()
            return 0
        if msg == APP_QUIT_KEY:
            self.quit_app()
            return 0

        if msg == WM_DESTROY:
            _KillTimer(hwnd, IDT_REDRAW)
            _KillTimer(hwnd, IDT_POLL)
            _WINDOW_INSTANCES.pop(hwnd, None)
            win32gui.DestroyWindow(hwnd)
            ctypes.windll.user32.PostQuitMessage(0)
            return 0

        return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    # ---- 绘制 ----
    def _draw(self, hwnd):
        rect = win32gui.GetClientRect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        hdc = win32gui.GetDC(hwnd)
        if not hdc:
            return
        try:
            # 背景（深色，覆盖整窗，确保 alpha 透明）
            bg = win32gui.CreateSolidBrush(C_BG)
            win32gui.FillRect(hdc, (0, 0, w, h), bg)
            win32gui.DeleteObject(bg)

            s = self.data
            offline = ("error" in s and not s.get("running")
                       and not s.get("count"))
            if offline:
                self._draw_text(hdc, 10, 8, 22, "FishingBot  ✕ 离线",
                                color=C_RED)
                self._draw_text(hdc, 10, 38, 14,
                                "请先运行 webui (start.bat)",
                                color=C_GREY)
                return

            running = bool(s.get("running"))
            score = float(s.get("score") or 0.0)
            peak = float(s.get("peak") or 0.0)
            count = int(s.get("count") or 0)
            last = s.get("last") or "—"
            err = s.get("error")
            rec = (s.get("rec") or {}).get("state", "idle")

            title_color = C_GREEN if running else (C_RED if err else C_TEXT)
            title = "▶ 运行中" if running else "■ 已停止"
            if rec == "recording":
                title = "⏺ 录制中  " + title
            if err:
                title = "⚠ 异常  " + title

            self._draw_text(hdc, 10, 6, 24, title, color=title_color)

            line2 = (f"命中 {count}   上次 {last}   "
                     f"score {score:.2f}   peak {peak:.2f}")
            self._draw_text(hdc, 10, 34, 15, line2, color=C_TEXT)

            if self.mode == "control":
                # 按钮占 y=54..96（控制模式窗口高 104）
                self._draw_buttons(hdc, h)
            else:
                hint = "右键 / Ctrl+Alt+O 切换模式"
                self._draw_text(hdc, 10, 55, 13, hint, color=C_GREY)
        finally:
            win32gui.ReleaseDC(hwnd, hdc)
            win32gui.ValidateRect(hwnd, None)

    def _draw_buttons(self, hdc, win_h):
        for b in self.buttons:
            x1, y1, x2, y2 = b["rect"]
            brush = win32gui.CreateSolidBrush(C_BTN_BG)
            win32gui.FillRect(hdc, (x1, y1, x2, y2), brush)
            win32gui.DeleteObject(brush)
            pen = win32gui.CreatePen(win32con.PS_SOLID, 1, b["fg"])
            old = win32gui.SelectObject(hdc, pen)
            win32gui.MoveToEx(hdc, x1, y1)
            win32gui.LineTo(hdc, x2 - 1, y1)
            win32gui.LineTo(hdc, x2 - 1, y2 - 1)
            win32gui.LineTo(hdc, x1, y2 - 1)
            win32gui.LineTo(hdc, x1, y1)
            win32gui.SelectObject(hdc, old)
            win32gui.DeleteObject(pen)
            # 文字居中（ExtTextOut + 先测量宽度，最稳）
            self._draw_text_centered(hdc, (x1, y1, x2, y2), b["label"],
                                     size=17, color=b["fg"])

    def _draw_text_centered(self, hdc, rc, text, size=17, color=0xFFFFFF):
        x1, y1, x2, y2 = rc
        font = self.fonts.get(size, bold=True)
        old = win32gui.SelectObject(hdc, font)
        win32gui.SetTextColor(hdc, color)
        win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
        sz = _wt.SIZE()
        _GetTextExtentPoint32W(int(hdc), text, len(text), ctypes.byref(sz))
        tx = x1 + ((x2 - x1) - sz.cx) // 2
        ty = y1 + ((y2 - y1) - sz.cy) // 2
        win32gui.ExtTextOut(hdc, tx, ty, 0, None, text, None)
        win32gui.SelectObject(hdc, old)

    def _draw_text(self, hdc, x, y, size, text, color=0xFFFFFF):
        font = self.fonts.get(size)
        old = win32gui.SelectObject(hdc, font)
        win32gui.SetTextColor(hdc, color)
        win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
        win32gui.ExtTextOut(hdc, x, y, 0, None, text, None)
        win32gui.SelectObject(hdc, old)

    # ---- 消息循环 ----
    def run(self):
        win32gui.PumpMessages()


# ----------------------------- main -----------------------------
def main():
    parser = argparse.ArgumentParser(description="FishingBot 悬浮信息条")
    parser.add_argument("--port", type=int, default=8765,
                        help="webui 端口（默认 8765）")
    parser.add_argument("--no-hotkey", action="store_true",
                        help="禁用全局热键（用右键切换）")
    args = parser.parse_args()

    global API
    API = f"http://127.0.0.1:{args.port}"

    o = Overlay()
    if args.no_hotkey:
        o._register_hotkeys = lambda: None  # noqa: SLF001
        o._register_hotkeys()
    o.run()


if __name__ == "__main__":
    main()
