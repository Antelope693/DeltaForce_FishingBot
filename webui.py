# -*- coding: utf-8 -*-
"""
钓鱼挂机 Web 控制台
==================
在本地起一个网页，可视化地完成：改配置、截屏取色、启动/停止挂机。

用法：
  python webui.py            （自动申请管理员权限）
  python webui.py --port 9000
然后浏览器打开 http://127.0.0.1:8765
"""

import argparse
import io
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mss
import mss.tools

import fishing_bot as fb

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "page.html")

# ---------------- 全局状态 ----------------
STATE = {
    "running": False,
    "present": None,          # 当前是否检测到浮标颜色
    "count": 0,               # 已触发次数
    "last": None,             # 上次触发时间
    "error": None,
}
_lock = threading.Lock()
_bot_thread = None
_last_press = 0.0


def bot_loop():
    """后台挂机线程：监控颜色，消失即按键。"""
    global _last_press
    with mss.MSS() as sct:
        while STATE["running"]:
            try:
                cfg = fb.load_config()
                present = fb.color_present(sct, cfg["region"], cfg["color"],
                                           cfg["tolerance"])
                STATE["present"] = present
                STATE["error"] = None
                now = time.time()
                if not present and now - _last_press >= float(cfg["cooldown"]):
                    fb.do_action(cfg["action"])
                    _last_press = now
                    STATE["count"] += 1
                    STATE["last"] = time.strftime("%H:%M:%S")
                time.sleep(float(cfg["interval"]))
            except Exception as e:
                STATE["error"] = str(e)
                time.sleep(0.5)


def start_bot():
    global _bot_thread
    with _lock:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["count"] = 0
        STATE["last"] = None
        _bot_thread = threading.Thread(target=bot_loop, daemon=True)
        _bot_thread.start()


def stop_bot():
    with _lock:
        STATE["running"] = False


def grab_screen_png(delay=0.0):
    """截取整个（虚拟）屏幕，返回 PNG 字节。delay 秒后才开始截。"""
    if delay > 0:
        time.sleep(delay)
    with mss.MSS() as sct:
        img = sct.grab(sct.monitors[0])  # 0 = 所有显示器的合并区域
        return mss.tools.to_png(img.rgb, img.size)


def pick_color(x, y):
    """采样 (x,y) 处像素颜色，并把监控区域设为以该点为中心的方框。"""
    cfg = fb.load_config()
    w = int(cfg["region"][2])
    h = int(cfg["region"][3])
    with mss.MSS() as sct:
        px = sct.grab({"left": x, "top": y, "width": 1, "height": 1})
        b, g, r = px.raw[0], px.raw[1], px.raw[2]
    cfg["color"] = [r, g, b]
    cfg["region"] = [max(x - w // 2, 0), max(y - h // 2, 0), w, h]
    fb.save_config(cfg)
    return cfg


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def log_message(self, fmt, *args):  # 安静模式
        pass

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                with open(PAGE_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/status"):
                cfg = fb.load_config()
                self._send(200, {"state": STATE, "config": cfg})
            elif self.path.startswith("/api/screenshot"):
                delay = 0.0
                if "delay=" in self.path:
                    try:
                        delay = min(float(self.path.split("delay=")[1].split("&")[0]), 30)
                    except ValueError:
                        delay = 0.0
                png = grab_screen_png(delay)
                self._send(200, png, "image/png")
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/config":
                data = self._body_json()
                cfg = fb.load_config()
                for k in ("region", "color", "tolerance", "interval",
                          "action", "cooldown", "calib_size"):
                    if k in data:
                        cfg[k] = data[k]
                fb.save_config(cfg)
                self._send(200, {"ok": True, "config": cfg})
            elif self.path == "/api/start":
                start_bot()
                self._send(200, {"ok": True, "running": True})
            elif self.path == "/api/stop":
                stop_bot()
                self._send(200, {"ok": True, "running": False})
            elif self.path == "/api/pick":
                data = self._body_json()
                x, y = int(data["x"]), int(data["y"])
                cfg = pick_color(x, y)
                self._send(200, {"ok": True, "config": cfg,
                                 "message": f"已采样 RGB{tuple(cfg['color'])}，"
                                            f"监控区域已移动到 ({x},{y}) 附近"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="钓鱼挂机 Web 控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-elevate", action="store_true",
                        help="跳过管理员权限申请（仅供测试）")
    parser.add_argument("--no-browser", action="store_true",
                        help="不自动打开浏览器")
    args = parser.parse_args()

    if not args.no_elevate and not fb.is_admin():
        print("[提示] 正在申请管理员权限 ...")
        fb.elevate_and_exit()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 50)
    print(f"钓鱼挂机 Web 控制台已启动: {url}")
    print("浏览器关闭后，在本窗口按 Ctrl+C 退出。")
    print("=" * 50)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_bot()
        print("\n已退出。")


if __name__ == "__main__":
    main()
