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
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import soundcard as sc  # noqa: F401  必须在主线程导入（soundcard 导入时初始化 COM）

import sound_bot as fb

# 打包成 exe 后 page.html 被收进 exe 内部（--add-data）；源码运行时在脚本旁边
if getattr(sys, "frozen", False):
    PAGE_FILE = os.path.join(sys._MEIPASS, "page.html")
else:
    PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

# ---------------- 全局状态 ----------------
STATE = {
    "running": False,
    "score": 0.0,        # 最近一次互相关得分
    "peak": 0.0,         # 自上次触发以来的最高得分（调阈值用）
    "level": 0.0,        # 当前音频电平（RMS）
    "count": 0,          # 已触发轮数
    "last": None,        # 上次触发时间
    "error": None,
    "seq": {"busy": False, "steps": None},  # 当前/最近一轮收鱼流程
    "test": {"running": False, "result": None},
    "diag": {"running": False, "result": None},
    "act": {"running": False, "remaining": 0},  # 3 秒倒计时按键测试
}
_lock = threading.Lock()
_bot_thread = None


def bot_loop():
    """后台挂机线程：监听 loopback，听到咬钩音效即执行一轮收鱼流程。

    流程（在独立线程里跑，不阻塞音频读取）：
      抬杆(左键+提示音) → 等 interrupt_delay → 打断检视(左键)
      → 等 recast_delay → 抛竿(左键)
    触发后设置 busy_until 抑制窗口：整轮流程结束前不会再触发，
    同时清空音频缓冲，避免同一段咬钩声音重复计数。
    """
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
            busy_until = 0.0  # 收鱼流程结束前不再触发
            fw = str(cfg.get("foreground_window", "") or "")
            print(f"[bot_loop] 已就绪 | 阈值 {cfg['threshold']} | 冷却 "
                  f"{cfg['cooldown']}s | 驱动 mouse_event | 前台窗口: "
                  f"{fw if fw else '（不限——切出游戏也会按键！）'} | "
                  f"打断检视延迟 {cfg.get('interrupt_delay', 2.0)}s | "
                  f"再次抛竿延迟 {cfg.get('recast_delay', 4.0)}s | "
                  f"提示音 {'开' if cfg.get('notify_sound', True) else '关'}")
            if not fw:
                print("[!] 警告: foreground_window 为空，任何前台窗口都会收到按键")
            with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
                while STATE["running"]:
                    cfg = fb.load_config()  # 热更新阈值/冷却/延迟/前台等
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

                    threshold = float(cfg["threshold"])
                    cooldown = float(cfg["cooldown"])

                    if (score >= threshold
                            and now - last_press >= cooldown
                            and now >= busy_until):
                        last_press = now
                        busy_until = now + fb.sequence_duration(cfg)
                        seq_cfg = dict(cfg)  # 本轮流程用触发时的参数快照
                        STATE["count"] += 1
                        STATE["last"] = time.strftime("%H:%M:%S")
                        STATE["peak"] = 0.0  # 重置峰值，便于观察下一次咬钩
                        buf.clear()  # 清空缓冲，避免同一段声音重复触发
                        print(f"[{STATE['last']}] 检测到咬钩音效 "
                              f"(得分 {score:.3f})，开始收鱼流程")

                        def _run_seq(c=seq_cfg):
                            try:
                                fb.init_com()
                                steps = fb.run_fishing_sequence(
                                    c, should_stop=lambda: not STATE["running"])
                                STATE["seq"] = {
                                    "busy": any(s[1] for s in steps),
                                    "steps": [list(s) for s in steps],
                                }
                                print(f"[流程] " + " → ".join(
                                    f"{n}{'✓' if ok else '✗'}"
                                    for n, ok in steps))
                            except Exception as e:
                                traceback.print_exc()
                                STATE["error"] = f"流程异常: {e}"
                            finally:
                                STATE["seq"]["busy"] = False

                        threading.Thread(target=_run_seq, daemon=True).start()
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
        STATE["seq"] = {"busy": False, "steps": None}
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


def play_notify_preview():
    """试听抬杆提示音。"""
    fb.play_notify_sound()


def test_action(countdown=3.0):
    """
    倒计时 `countdown` 秒后，用 mouse_event 点一下左键。
    不强制前台窗口——你想测按键时并不一定要把焦点先切回游戏。
    """
    with _lock:
        if STATE["act"]["running"]:
            return False
        STATE["act"] = {"running": True, "remaining": float(countdown)}

    def work():
        try:
            t0 = time.time()
            while True:
                left = countdown - (time.time() - t0)
                STATE["act"]["remaining"] = round(max(0, left), 1)
                if left <= 0:
                    break
                time.sleep(0.1)
            fb.click_mouse()
        except Exception as e:
            traceback.print_exc()
            STATE["error"] = f"按键测试失败: {e}"
        finally:
            STATE["act"]["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return True


def register_global_hotkeys():
    """
    注册全局热键（键盘 hook，必须在主线程）：
      Ctrl+Alt+T  -> 3 秒倒计时后按键
    """
    try:
        import keyboard
    except Exception as e:
        print(f"[提示] 未安装 keyboard 库，全局热键不可用（{e}）。仍可点页面按钮。")
        return
    try:
        keyboard.add_hotkey("ctrl+alt+t",
                            lambda: test_action(3.0),
                            suppress=False)
        print("[热键] Ctrl+Alt+T 3 秒后按键")
    except Exception as e:
        print(f"[提示] 全局热键注册失败: {e}（仍可点页面按钮）")


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
            elif self.path == "/api/window":
                # 前台窗口 + 游戏窗口信息，方便诊断
                fg = fb.get_foreground_title()
                cfg = fb.load_config()
                hwnd, title = fb.find_window_by_title_sub(
                    cfg.get("foreground_window", "") or "")
                self._send(200, {
                    "foreground": fg,
                    "game_window": {"hwnd": hwnd, "title": title}
                    if hwnd else None,
                })
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/config":
                data = self._body_json()
                cfg = fb.load_config()
                for k in ("threshold", "cooldown",
                          "interrupt_delay", "recast_delay",
                          "notify_sound", "foreground_window",
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
            elif self.path == "/api/notify-preview":
                play_notify_preview()
                self._send(200, {"ok": True, "note": "已播放提示音"})
            elif self.path == "/api/test-action":
                ok = test_action(3.0)
                self._send(200, {"ok": ok,
                                 "note": "3 秒后按下" if ok else "已在倒计时中"})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})


def _pause_if_frozen(msg="按回车键关闭窗口 ..."):
    """双击 exe 时控制台会瞬间关闭，暂停一下让用户能看到提示。"""
    if getattr(sys, "frozen", False):
        try:
            input(f"\n{msg}")
        except Exception:
            pass


class _Parser(argparse.ArgumentParser):
    """参数报错时先停一下，避免控制台一闪而过看不到原因。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        _pause_if_frozen()
        sys.exit(2)


def _port_in_use(port):
    """Windows 下 SO_REUSEADDR 会让第二个进程也能绑定同一端口，所以先主动探测一次。"""
    import socket
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    parser = _Parser(description="钓鱼挂机 Web 控制台（声音版）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-elevate", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.no_elevate and not fb.is_admin():
        print("[提示] 正在申请管理员权限 ...")
        fb.elevate_and_exit()

    # 全局热键（必须在主线程注册）
    register_global_hotkeys()

    # 注意：必须在本进程 bind 之前探测，否则连到的是自己刚建好的监听端口
    if _port_in_use(args.port):
        print(f"[错误] 端口 {args.port} 已被占用（可能是之前没关掉的挂机窗口）。")
        print("       请先关掉那个窗口再运行本程序，否则会出现两个机器人同时乱点。")
        _pause_if_frozen()
        sys.exit(1)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(f"[错误] 端口 {args.port} 已被占用，可能已经有一个挂机窗口在运行。")
        print("       请先关掉已打开的窗口；或在此窗口内用 --port 8888 换个端口启动。")
        _pause_if_frozen()
        sys.exit(1)
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 50)
    print(f"钓鱼挂机 Web 控制台（声音版）已启动: {url}")
    print("浏览器关闭后，在本窗口按 Ctrl+C 退出。")
    print("-" * 50)
    print("作者：食叶羚_SYL ｜ 开源免费，倒卖必究！")
    print("=" * 50)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_bot()
        print("\n已退出。")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        _pause_if_frozen("出错了。按回车键关闭窗口 ...")
