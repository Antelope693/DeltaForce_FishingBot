# -*- coding: utf-8 -*-
"""
钓鱼挂机 Web 控制台（声音版）
============================
本地网页：改配置、选输出设备、自测检测、启动/停止挂机。

用法：
  python webui.py            （自动申请管理员权限）
  python webui.py --port 9000
然后浏览器打开 http://127.0.0.1:8765
"""

import argparse
import json
import os
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soundcard as sc  # noqa: F401  必须在主线程导入（soundcard 导入时初始化 COM）

import sound_bot as fb

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "page.html")

# ---------------- 全局状态 ----------------
STATE = {
    "running": False,
    "score": 0.0,        # 最近一次互相关得分
    "peak": 0.0,         # 自上次触发以来的最高得分（调阈值用）
    "level": 0.0,        # 当前音频电平（RMS）
    "count": 0,          # 已触发次数
    "last": None,        # 上次触发时间
    "error": None,
    "test": {"running": False, "result": None},
    "diag": {"running": False, "result": None},
}
_lock = threading.Lock()
_bot_thread = None


def bot_loop():
    """后台挂机线程：监听 loopback，听到咬钩音效即按键。"""
    # soundcard 依赖 COM，而 COM 按线程初始化，工作线程必须自己初始化一次
    fb.init_com()
    while STATE["running"]:
        try:
            cfg = fb.load_config()
            wav = fb.resolve_sound_file(cfg)
            if not os.path.exists(wav):
                raise FileNotFoundError(f"参考音效不存在: {wav}")
            rate = int(cfg["samplerate"])
            mic, _ = fb.open_loopback(cfg)
            tmpl = fb.trim_silence(fb.load_wav_mono(wav, rate), rate)
            det = fb.SoundDetector(tmpl, rate)
            buf = fb.RollingBuffer(len(tmpl) + int(rate * 0.2))
            chunk = max(int(rate * 0.05), 1024)
            last_press = 0.0
            with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
                while STATE["running"]:
                    cfg = fb.load_config()  # 热更新阈值/冷却/动作
                    data = rec.record(numframes=chunk)
                    mono = data.mean(axis=1) if data.ndim > 1 else data[:, 0]
                    buf.push(mono)
                    score = det.best_score(buf.buf)
                    STATE["score"] = round(score, 3)
                    STATE["peak"] = round(max(STATE["peak"], score), 3)
                    STATE["level"] = round(float(
                        (mono.astype("float32") ** 2).mean() ** 0.5), 4)
                    STATE["error"] = None
                    now = time.time()
                    if (score >= float(cfg["threshold"])
                            and now - last_press >= float(cfg["cooldown"])):
                        fb.do_action(cfg["action"])
                        last_press = now
                        STATE["count"] += 1
                        STATE["last"] = time.strftime("%H:%M:%S")
                        STATE["peak"] = 0.0  # 重置峰值，便于观察下一次咬钩
        except Exception as e:
            STATE["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            time.sleep(1.0)


def start_bot():
    global _bot_thread
    with _lock:
        if STATE["running"]:
            return
        STATE["running"] = True
        STATE["count"] = 0
        STATE["last"] = None
        STATE["score"] = 0.0
        STATE["peak"] = 0.0
        STATE["error"] = None
        _bot_thread = threading.Thread(target=bot_loop, daemon=True)
        _bot_thread.start()


def stop_bot():
    with _lock:
        STATE["running"] = False


def run_self_test_async():
    """离线自测：播放参考音效 -> 检测（不按键）。"""
    def work():
        fb.init_com()  # 工作线程必须初始化 COM，否则报 0x800401f0
        try:
            cfg = fb.load_config()
            wav = fb.resolve_sound_file(cfg)
            STATE["test"]["result"] = None
            mic, rate = fb.open_loopback(cfg)
            tmpl = fb.trim_silence(fb.load_wav_mono(wav, rate), rate)
            det = fb.SoundDetector(tmpl, rate)
            buf = fb.RollingBuffer(len(tmpl) + int(rate * 1.0))

            def play():
                time.sleep(0.5)
                try:
                    fb.play_file(wav, 48000)  # 内部初始化该线程 COM
                except Exception as e:
                    STATE["test"]["result"] = {
                        "ok": False,
                        "error": f"播放失败 {type(e).__name__}: {e}"}

            threading.Thread(target=play, daemon=True).start()
            best, detected = 0.0, False
            chunk = max(int(rate * 0.05), 1024)
            t0 = time.time()
            with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
                while time.time() - t0 < 5.0:
                    data = rec.record(numframes=chunk)
                    mono = data.mean(axis=1) if data.ndim > 1 else data[:, 0]
                    buf.push(mono)
                    s = det.best_score(buf.buf)
                    best = max(best, s)
                    if s >= float(cfg["threshold"]):
                        detected = True
                        break
            STATE["test"]["result"] = {
                "ok": detected,
                "score": round(best, 3),
                "threshold": float(cfg["threshold"]),
            }
        except Exception as e:
            traceback.print_exc()
            STATE["test"]["result"] = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc().splitlines()[-3:],
            }
        finally:
            STATE["test"]["running"] = False

    t = threading.Thread(target=work, daemon=True)
    if not STATE["test"]["running"]:
        STATE["test"]["running"] = True
        STATE["test"]["result"] = None
        t.start()


def run_diag_async():
    """环境诊断：逐项检查音频链路，定位失败原因。"""
    def work():
        fb.init_com()
        try:
            STATE["diag"]["result"] = {"steps": fb.run_diag(fb.load_config())}
        except Exception as e:
            traceback.print_exc()
            STATE["diag"]["result"] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            STATE["diag"]["running"] = False

    if not STATE["diag"]["running"]:
        STATE["diag"]["running"] = True
        STATE["diag"]["result"] = None
        threading.Thread(target=work, daemon=True).start()


def play_reference():
    """试听参考音效。"""
    def work():
        try:
            fb.play_file(fb.resolve_sound_file(fb.load_config()), 48000)
        except Exception as e:
            traceback.print_exc()
            STATE["error"] = f"播放失败 {type(e).__name__}: {e}"
    threading.Thread(target=work, daemon=True).start()


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = body.encode("utf-8") if isinstance(body, str) else body
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

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                with open(PAGE_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/status"):
                self._send(200, {"state": STATE, "config": fb.load_config()})
            elif self.path.startswith("/api/devices"):
                fb.init_com()  # 请求线程同样需要初始化 COM
                dev = sc.default_speaker().name
                self._send(200, {"default": dev,
                                 "devices": [m.name for m in
                                             sc.all_microphones(include_loopback=True)]})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/config":
                data = self._body_json()
                cfg = fb.load_config()
                for k in ("sound_file", "threshold", "cooldown", "action",
                          "samplerate", "device"):
                    if k in data:
                        cfg[k] = data[k]
                fb.save_config(cfg)
                self._send(200, {"ok": True, "config": cfg,
                                 "note": "采样率/设备改动在下次启动时生效"})
            elif self.path == "/api/start":
                start_bot()
                self._send(200, {"ok": True, "running": True})
            elif self.path == "/api/stop":
                stop_bot()
                self._send(200, {"ok": True, "running": False})
            elif self.path == "/api/test":
                if STATE["running"]:
                    play_reference()
                    self._send(200, {"ok": True,
                                     "note": "挂机运行中，已播放音效；若检测正常，几秒内会触发一次"})
                else:
                    run_self_test_async()
                    self._send(200, {"ok": True,
                                     "note": "自测已开始，约 5 秒后看结果"})
            elif self.path == "/api/diag":
                run_diag_async()
                self._send(200, {"ok": True, "note": "诊断中，稍后查看结果"})
            elif self.path == "/api/play":
                play_reference()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    parser = argparse.ArgumentParser(description="钓鱼挂机 Web 控制台（声音版）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-elevate", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.no_elevate and not fb.is_admin():
        print("[提示] 正在申请管理员权限 ...")
        fb.elevate_and_exit()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 50)
    print(f"钓鱼挂机 Web 控制台（声音版）已启动: {url}")
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
