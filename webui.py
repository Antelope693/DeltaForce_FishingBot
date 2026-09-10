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
REC_FILE = os.path.join(HERE, "_recorded.wav")  # 录制临时文件

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
    "rec": {"state": "idle", "elapsed": 0.0,   # state: idle/recording/ready
            "duration": 0.0, "path": None, "error": None},
    "act": {"running": False, "remaining": 0}, # 3 秒倒计时按键测试
}
_lock = threading.Lock()
_bot_thread = None
_rec_thread = None
_rec_stop = {"flag": False}


def bot_loop():
    """后台挂机线程：监听 loopback，听到咬钩音效即按动作。

    点击策略（修复旧版两大 bug）：
      1. 单帧误触 → 引入「连续命中」：必须 min_strikes 个音频块连续超阈值才触发
      2. 切出游戏后漏点 → 引入「前台窗口白名单」：前台窗口标题不含
         cfg['foreground_window'] 时整轮跳过按键
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
            strike = 0  # 连续命中阈值的累计块数（每次触发后或低于阈值时清零）
            fw = str(cfg.get("foreground_window", "") or "")
            print(f"[bot_loop] 已就绪 | 阈值 {cfg['threshold']} | 冷却 "
                  f"{cfg['cooldown']}s | 驱动 {cfg.get('click_driver', 'sendinput')} | "
                  f"动作 {cfg['action']} × {cfg.get('click_count', 1)} | "
                  f"连续命中 ≥ {cfg.get('min_strikes', 2)} 块 | 前台窗口: "
                  f"{fw if fw else '（不限——切出游戏也会按键！）'} | "
                  f"key_fallback: {cfg.get('key_fallback', '') or '(无)'}")
            if not fw:
                print("[!] 警告: foreground_window 为空，任何前台窗口都会收到按键")
            with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
                while STATE["running"]:
                    cfg = fb.load_config()  # 热更新阈值/冷却/动作/连击/前台/驱动等
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
                    min_strikes = max(1, int(cfg.get("min_strikes", 2)))
                    click_count = max(1, int(cfg.get("click_count", 1)))
                    click_interval_ms = int(cfg.get("click_interval_ms", 80))
                    foreground_window = str(cfg.get("foreground_window", "") or "")
                    click_driver = str(cfg.get("click_driver", "sendinput"))
                    key_fallback = str(cfg.get("key_fallback", "") or "")

                    # 连续命中门槛：只有持续命中（min_strikes 块以上）才算真的咬钩
                    if score >= threshold:
                        strike += 1
                    else:
                        strike = 0

                    if (strike >= min_strikes
                            and now - last_press >= cooldown):
                        fb.do_action(
                            cfg["action"],
                            click_count=click_count,
                            click_interval_ms=click_interval_ms,
                            foreground_window=foreground_window,
                            click_driver=click_driver,
                            key_fallback=key_fallback,
                        )
                        last_press = now
                        STATE["count"] += 1
                        STATE["last"] = time.strftime("%H:%M:%S")
                        STATE["peak"] = 0.0  # 重置峰值，便于观察下一次咬钩
                        strike = 0
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


# ---------------- 录制 / 裁剪 ----------------
def start_recording(duration_sec=10.0):
    """
    开始录制：loopback 录 `duration_sec` 秒到 REC_FILE。
    若已有录制文件则覆盖。
    """
    with _lock:
        if STATE["rec"]["state"] == "recording":
            return False
        _rec_stop["flag"] = False
        STATE["rec"] = {"state": "recording", "elapsed": 0.0,
                        "duration": float(duration_sec),
                        "path": None, "error": None}

    def work():
        fb.init_com()
        try:
            def on_progress(elapsed, peak):
                STATE["rec"]["elapsed"] = round(elapsed, 1)
            info = fb.record_loopback(fb.load_config(), duration_sec,
                                      REC_FILE, on_progress=on_progress,
                                      stop_flag=_rec_stop)
            STATE["rec"]["path"] = info["path"]
            STATE["rec"]["duration"] = round(info["duration"], 2)
            STATE["rec"]["elapsed"] = STATE["rec"]["duration"]
            STATE["rec"]["state"] = "ready"
        except Exception as e:
            traceback.print_exc()
            STATE["rec"]["state"] = "idle"
            STATE["rec"]["error"] = f"{type(e).__name__}: {e}"

    global _rec_thread
    _rec_thread = threading.Thread(target=work, daemon=True)
    _rec_thread.start()
    return True


def stop_recording():
    """立即停止录制（保留已录制部分）。"""
    with _lock:
        if STATE["rec"]["state"] != "recording":
            return False
        _rec_stop["flag"] = True
        STATE["rec"]["error"] = "已标记停止，保存已录制部分…"
    return True


def save_cropped(start_sec, end_sec, save_as=None):
    """
    把上次录制结果裁剪为参考音效。
    `save_as` = None 时覆盖原参考音效；否则保存为新文件名（相对工作目录）。
    """
    rec_path = STATE["rec"].get("path")
    if not rec_path or not os.path.exists(rec_path):
        raise FileNotFoundError("尚未录制音频")
    if save_as:
        out = save_as if os.path.isabs(save_as) else os.path.join(HERE, save_as)
    else:
        out = fb.resolve_sound_file(fb.load_config())
    info = fb.crop_wav(rec_path, out, float(start_sec), float(end_sec))
    # 把 sound_file 指向新文件（如果用户改了名字）
    cfg = fb.load_config()
    if os.path.abspath(out) != os.path.abspath(fb.resolve_sound_file(cfg)):
        try:
            cfg["sound_file"] = os.path.basename(out)
            fb.save_config(cfg)
        except Exception:
            pass
    return info


def test_action(countdown=3.0):
    """
    倒计时 `countdown` 秒后，按配置中的 action。
    与挂机线程保持一致（也走连击），但不强制前台窗口——你想测按键时并不一定要把
    焦点先切回游戏。
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
            cfg = fb.load_config()
            fb.do_action(
                cfg["action"],
                click_count=int(cfg.get("click_count", 1)),
                click_interval_ms=int(cfg.get("click_interval_ms", 80)),
                click_driver=str(cfg.get("click_driver", "sendinput")),
                key_fallback=str(cfg.get("key_fallback", "") or ""),
                foreground_window="",  # 测试不限制前台
            )
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
      Ctrl+Alt+R  -> 开始录制 10 秒
      Ctrl+Alt+T  -> 3 秒倒计时后按键
    """
    try:
        import keyboard
    except Exception as e:
        print(f"[提示] 未安装 keyboard 库，全局热键不可用（{e}）。仍可点页面按钮。")
        return
    try:
        keyboard.add_hotkey("ctrl+alt+r",
                            lambda: start_recording(10.0),
                            suppress=False)
        keyboard.add_hotkey("ctrl+alt+t",
                            lambda: test_action(3.0),
                            suppress=False)
        print("[热键] Ctrl+Alt+R 录制 10 秒 ｜ Ctrl+Alt+T 3 秒后按键")
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
            elif self.path == "/api/drivers":
                # 返回所有可选驱动 + 当前游戏窗口信息，方便诊断
                fg = fb.get_foreground_title()
                cfg = fb.load_config()
                hwnd, title = fb.find_window_by_title_sub(
                    cfg.get("foreground_window", "") or "")
                self._send(200, {
                    "drivers": ["sendinput", "mouse_event",
                                "postmessage", "sendmessage"],
                    "current_driver": cfg.get("click_driver", "sendinput"),
                    "foreground": fg,
                    "game_window": {"hwnd": hwnd, "title": title}
                    if hwnd else None,
                })
            elif self.path.startswith("/api/record/audio"):
                # 加 ?ts=... 防缓存
                rec_path = STATE["rec"].get("path")
                if not rec_path or not os.path.exists(rec_path):
                    self._send(404, {"error": "尚未录制"})
                    return
                with open(rec_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
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
                          "samplerate", "device",
                          "click_driver", "click_count", "click_interval_ms",
                          "min_strikes", "foreground_window", "key_fallback"):
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
            elif self.path == "/api/record/start":
                # 可选 body: {"duration": 10}
                body = self._body_json()
                dur = float(body.get("duration", 10))
                ok = start_recording(dur)
                self._send(200, {"ok": ok,
                                 "note": "已开始录制" if ok else "已在录制中"})
            elif self.path == "/api/record/stop":
                ok = stop_recording()
                self._send(200, {"ok": ok,
                                 "note": "标记停止" if ok else "当前未录制"})
            elif self.path == "/api/record/save":
                body = self._body_json()
                start = float(body.get("start", 0))
                end = float(body.get("end", 0))
                save_as = (body.get("save_as") or "").strip() or None
                try:
                    info = save_cropped(start, end, save_as)
                    self._send(200, {"ok": True, "info": info,
                                     "note": f"已保存为 {os.path.basename(info['path'])}"})
                except Exception as e:
                    self._send(400, {"ok": False, "error": str(e)})
            elif self.path == "/api/test-action":
                # 支持指定驱动：{"driver": "postmessage"} 或 {"driver": "mouse_event"}
                body = self._body_json() or {}
                # 临时切换驱动到这次测试
                if body.get("driver"):
                    driver = str(body["driver"])
                    valid = ("sendinput", "mouse_event", "postmessage", "sendmessage")
                    if driver not in valid:
                        self._send(400, {"ok": False,
                                         "error": f"未知驱动 {driver!r}，"
                                                  f"可选: {list(valid)}"})
                        return
                    cfg = fb.load_config()
                    cfg["click_driver"] = driver
                    fb.save_config(cfg)
                ok = test_action(3.0)
                self._send(200, {"ok": ok,
                                 "note": "3 秒后按下" if ok else "已在倒计时中",
                                 "driver": body.get("driver", "默认")})
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

    # 全局热键（必须在主线程注册）
    register_global_hotkeys()

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
