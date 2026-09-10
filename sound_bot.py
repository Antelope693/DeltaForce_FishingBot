# -*- coding: utf-8 -*-
"""
钓鱼咬钩声音监控挂机脚本
========================
原理：持续捕获系统声音（WASAPI loopback），与参考音效（TIMETOUP.wav）
      做归一化互相关匹配。一旦听到咬钩音效，立即按下设定的键（默认鼠标左键）。

核心检测模块：SoundDetector（读参考 wav -> 滚动缓冲 -> FFT 互相关 -> 归一化得分）

用法（命令行）：
  python sound_bot.py           （自动申请管理员权限，F9 手动抛竿，F10 退出）
  python sound_bot.py --no-elevate

依赖：numpy、soundcard
"""

import argparse
import ctypes
import json
import os
import sys
import time
import wave

import numpy as np
import soundcard as sc

# 说明：soundcard 必须在**主线程**完成导入——它在导入时会初始化所在线程的 COM，
# 且无法容忍“本线程已初始化过”（会抛 Error 0x100000001）。
# 所以：主线程先导入（本行），其他工作线程使用前调用 init_com() 单独初始化。

# ---------------- 配置 ----------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    # 参考音效文件（相对本目录或绝对路径）
    "sound_file": "TIMETOUP.wav",
    # 归一化互相关得分阈值（0~1，越高越严格，建议 0.6~0.85）
    "threshold": 0.7,
    # 两次触发之间的最短冷却（秒）
    "cooldown": 2.0,
    # 触发动作："click" = 鼠标左键（当前光标位置）；或键名 "f" / "space" 等
    "action": "click",
    # 采样率（一般无需改动；loopback 常见为 48000）
    "samplerate": 48000,
    # 音频输出设备名（null = 系统默认输出；填设备名子串可指定如 "耳机"）
    "device": None,
    # ---- 防误触 + 防切走漏点 ----
    # 每次触发连击几下（>1 用于模拟咬钩瞬间的快速点击，1 = 单击）
    "click_count": 3,
    # 连击之间的间隔（毫秒）
    "click_interval_ms": 80,
    # 至少连续 N 个音频块命中阈值才触发（防单帧噪声；3 块 ≈ 150ms）
    "min_strikes": 3,
    # 仅当当前前台窗口标题含此子串时才点击；空 = 不限制。
    # 设成游戏窗口标题的关键字（如 "Delta"），可彻底避免切出游戏后还在按键。
    "foreground_window": "",
}

# ---------------- Win32 按键注入 ----------------
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]


def send_left_click():
    """在当前光标位置发送一次鼠标左键点击。"""
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        inp = INPUT(type=INPUT_MOUSE)
        inp.union.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def get_foreground_title():
    """获取当前前台窗口标题（best effort）。失败时返回空串。"""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""
    except Exception:
        return ""


def do_action(action, click_count=1, click_interval_ms=80, foreground_window=""):
    """
    执行一次"动作"。
    新参数（均向后兼容，不传 = 旧版行为）：
      - click_count    : 一次触发连击几下（>1 等同于快速点击，常用于"收杆"瞬间）
      - click_interval_ms : 每次点击之间的毫秒间隔（默认 80ms）
      - foreground_window : 非空时，仅当当前前台窗口标题含此子串才真的按键；
                          空 = 不限制（旧行为）。这样切出游戏窗口之后不会把按键漏给
                          浏览器/桌面，便于避免"切走后还在一直点"的 bug。
    """
    fg_title = ""
    if foreground_window:
        fg_title = get_foreground_title()
        if foreground_window not in fg_title:
            print(f"[跳过] 前台窗口不符（{fg_title or '(空)'!r}，需含 "
                  f"{foreground_window!r}），本轮不按键")
            return False
    n = max(1, int(click_count))
    delay = max(0.0, int(click_interval_ms) / 1000.0)
    for i in range(n):
        if i:
            # 微小间隔 0 也不要真"无延迟连发"，系统不一定跟得上，5ms 兜底
            time.sleep(delay if delay > 0 else 0.005)
        if action == "click":
            send_left_click()
        else:
            import keyboard
            keyboard.press_and_release(action)
    return True


# ---------------- 配置读写 ----------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[警告] 读取 config.json 失败，使用默认配置: {e}")
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 管理员权限 ----------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate_and_exit():
    params = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1)
    if ret <= 32:
        print("[错误] 管理员权限申请被拒绝，无法继续。")
    sys.exit(0 if ret > 32 else 1)


# ---------------- 音频解码 ----------------
def init_com():
    """
    初始化当前线程的 COM。

    soundcard 依赖 WASAPI(COM)，而 COM 是**按线程**初始化的：soundcard 只在
    导入它的那个线程里初始化了 COM，其他线程（挂机线程、网页请求线程）直接
    调用音频接口会报 Error 0x800401f0 (CO_E_NOTINITIALIZED)。
    因此每个用到音频的线程都必须先调用本函数。
    返回值无关紧要：S_FALSE=已初始化过，RPC_E_CHANGED_MODE=该线程已是 STA，都可用。
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # 0 = COINIT_MULTITHREADED
    except Exception:
        pass


def load_wav_mono(path, target_rate):
    """读取 wav -> 单声道 float32 [-1,1] -> 重采样到 target_rate。"""
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 1:      # 8-bit unsigned
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif sw == 2:    # 16-bit
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    elif sw == 3:    # 24-bit
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        arr = v.astype(np.float32) / (1 << 23)
    elif sw == 4:    # 32-bit int
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / (1 << 31)
    else:
        raise ValueError(f"不支持的位宽: {sw}")
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    if rate != target_rate:
        n_out = int(round(len(arr) * target_rate / rate))
        xs = np.linspace(0, len(arr) - 1, n_out)
        arr = np.interp(xs, np.arange(len(arr)), arr).astype(np.float32)
    return arr


def trim_silence(arr, rate, margin_ms=20, floor_db=-45):
    """去掉参考音前后过长的静音段（互相关对静音不敏感且拖慢速度）。"""
    floor = 10 ** (floor_db / 20)
    mask = np.abs(arr) > floor
    if not mask.any():
        return arr
    i0, i1 = int(np.argmax(mask)), len(mask) - int(np.argmax(mask[::-1]))
    m = int(rate * margin_ms / 1000)
    return arr[max(i0 - m, 0): min(i1 + m, len(arr))]


# ---------------- 声音检测器 ----------------
class SoundDetector:
    """归一化互相关（matched filter）检测参考音效。"""

    def __init__(self, template, rate):
        self.rate = rate
        self.w = np.ascontiguousarray(template, dtype=np.float32)
        self.L = len(self.w)
        self.w_energy = float(np.dot(self.w, self.w)) or 1.0
        self.w_fft_len = None  # 惰性计算
        self.w_spec = None

    def _spec(self, n):
        if self.w_spec is None or self.w_fft_len != n:
            self.w_fft_len = n
            self.w_spec = np.conj(np.fft.rfft(self.w, n))
        return self.w_spec

    def best_score(self, buf):
        """返回 buf 中与模板的最佳归一化互相关得分（0~1）。"""
        buf = np.asarray(buf, dtype=np.float32)
        n = len(buf)
        L = self.L
        if n < L:
            return 0.0
        corr = np.fft.irfft(np.fft.rfft(buf) * self._spec(n), n)
        cands = corr[: n - L + 1]
        # 各起点的局部能量（前缀和），用于归一化
        sq = np.concatenate([np.zeros(1, dtype=np.float64),
                             np.cumsum(buf.astype(np.float64) ** 2)])
        e = sq[L:] - sq[:-L] if len(sq) > L else np.array([sq[-1]])
        e = np.maximum(e[:len(cands)], 1e-12)
        denom = np.sqrt(e * self.w_energy)
        return float(np.max(np.abs(cands) / denom))


class RollingBuffer:
    def __init__(self, capacity):
        self.cap = capacity
        self.buf = np.zeros(0, dtype=np.float32)

    def push(self, x):
        self.buf = np.concatenate([self.buf, np.asarray(x, dtype=np.float32)])
        if len(self.buf) > self.cap:
            self.buf = self.buf[-self.cap:]

    def __len__(self):
        return len(self.buf)


def resolve_sound_file(cfg):
    p = cfg["sound_file"]
    if not os.path.isabs(p):
        here = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(here, p)
    return p


def play_file(path, samplerate=48000):
    """在调用线程内播放一个 wav（线程安全：内部会初始化 COM）。"""
    init_com()
    audio = load_wav_mono(path, samplerate)
    peak = float(np.abs(audio).max()) or 1.0
    sc.default_speaker().play(audio / peak, samplerate=samplerate)


# ---------------- 录制 / 裁剪 ----------------
def save_wav(path, audio, samplerate):
    """把 float32 单声道数组写为 16-bit PCM wav。"""
    audio = np.asarray(audio, dtype=np.float32)
    peak = float(np.abs(audio).max()) or 1.0
    pcm = np.clip(audio / peak * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(samplerate))
        w.writeframes(pcm.tobytes())


def record_loopback(cfg, duration_sec, output_path, on_progress=None, stop_flag=None):
    """
    从 loopback 设备录 `duration_sec` 秒到 `output_path`（wav）。
    `on_progress(elapsed, peak)` 每 0.1 秒回调一次，便于前端显示倒计时/电平。
    `stop_flag = {"flag": True/False}` 可中途打断（写盘保留已录制部分）。
    """
    init_com()
    mic, rate = open_loopback(cfg)
    chunk = max(int(rate * 0.05), 1024)
    chunks = []
    peak = 0.0
    t0 = time.time()
    last_tick = 0.0
    with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
        while True:
            data = rec.record(numframes=chunk)
            mono = data.mean(axis=1) if data.ndim > 1 else data[:, 0]
            mono = mono.astype(np.float32, copy=False)
            chunks.append(mono)
            peak = max(peak, float(np.abs(mono).max()))
            now = time.time()
            if on_progress and now - last_tick > 0.1:
                last_tick = now
                try:
                    on_progress(min(duration_sec, now - t0), peak)
                except Exception:
                    pass
            if now - t0 >= duration_sec:
                break
            if stop_flag is not None and stop_flag.get("flag"):
                break
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    save_wav(output_path, audio, rate)
    stopped_early = bool(stop_flag and stop_flag.get("flag"))
    return {"path": output_path, "samplerate": rate,
            "duration": len(audio) / rate, "peak": peak,
            "stopped": stopped_early}


def crop_wav(input_path, output_path, start_sec, end_sec):
    """从 wav 中截取 start_sec~end_sec（端点裁剪，可负数：相对末尾）。"""
    with wave.open(input_path, "rb") as w:
        rate = w.getframerate()
        sw = w.getsampwidth()
        ch = w.getnchannels()
        n = w.getnframes()
        i0 = max(0, int(round(start_sec * rate)))
        i1 = min(n, int(round(end_sec * rate)))
        if i1 <= i0:
            raise ValueError(f"无效区间: {start_sec}~{end_sec} (文件 {n/rate:.2f}s)")
        w.setpos(i0)
        raw = w.readframes(i1 - i0)
    # 重写 wav
    with wave.open(output_path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(raw)
    return {"path": output_path, "start": i0 / rate, "end": i1 / rate,
            "duration": (i1 - i0) / rate}


def run_diag(cfg):
    """
    环境诊断：逐步检查 COM、设备枚举、录音、播放、检测链路，
    返回每一步的结果，便于定位“自测失败”到底卡在哪。
    """
    from numpy import abs as np_abs

    steps = []

    def record(name, fn):
        try:
            detail = fn()
            steps.append({"name": name, "ok": True,
                          "detail": "" if detail is None else str(detail)})
            return True
        except Exception as e:
            steps.append({"name": name, "ok": False,
                          "detail": f"{type(e).__name__}: {e}"})
            return False

    rate = int(cfg["samplerate"])
    wav = resolve_sound_file(cfg)
    holder = {}

    record("初始化当前线程的 COM", lambda: init_com() or "OK")
    record("枚举输出/录音设备",
           lambda: f"共 {len(sc.all_microphones(include_loopback=True))} 个，"
                   f"默认输出 {sc.default_speaker().name}")

    def _open():
        holder["mic"], _ = open_loopback(cfg)
        return f"监听：{holder['mic'].name}"
    if record("打开 loopback 监听设备", _open):
        def _rec():
            with holder["mic"].recorder(samplerate=rate, blocksize=4800) as rec:
                data = rec.record(numframes=int(rate * 0.3))
            mono = data.mean(axis=1) if data.ndim > 1 else data[:, 0]
            return f"录制 0.3s 成功，电平 {(np_abs(mono).mean()):.4f}"
        record("录音测试（0.3 秒）", _rec)

    record("加载参考音效",
           lambda: f"{os.path.basename(wav)}，"
                   f"{len(trim_silence(load_wav_mono(wav, rate), rate))/rate:.2f}s")

    def _play():
        play_file(wav, 48000)
        return "已播放到默认输出设备"
    record("播放参考音效", _play)

    return steps


def open_loopback(cfg):
    """按配置打开 loopback 录音设备。返回 (mic, rate)。"""
    init_com()  # 必须在调用线程内初始化 COM
    rate = int(cfg["samplerate"])
    if cfg.get("device"):
        kw = str(cfg["device"])
        for m in sc.all_microphones(include_loopback=True):
            if kw in m.name:
                return m, rate
        raise RuntimeError(f"找不到含 “{kw}” 的输出设备")
    spk = sc.default_speaker()
    return sc.get_microphone(id=str(spk.name), include_loopback=True), rate


# ---------------- 命令行入口 ----------------
def main():
    parser = argparse.ArgumentParser(description="钓鱼咬钩声音监控挂机脚本")
    parser.add_argument("--no-elevate", action="store_true",
                        help="跳过管理员权限申请（仅供测试）")
    parser.add_argument("--test", action="store_true",
                        help="自测：播放参考音效并验证能否检测到")
    args = parser.parse_args()

    if not args.no_elevate and not is_admin():
        print("[提示] 正在申请管理员权限 ...")
        elevate_and_exit()

    cfg = load_config()
    wav = resolve_sound_file(cfg)

    if args.test:
        run_self_test(cfg, wav)
        return

    import keyboard

    init_com()
    running = {"on": True}
    keyboard.add_hotkey("f10", lambda: running.update(on=False))
    keyboard.add_hotkey("f9", lambda: do_action(cfg["action"]))  # 手动抛竿

    print("=" * 56)
    print("钓鱼挂机（声音版）已启动")
    print(f"  参考音效: {os.path.basename(wav)}")
    print(f"  得分阈值: {cfg['threshold']}  冷却: {cfg['cooldown']}s")
    n_clicks = int(cfg.get('click_count', 1))
    if n_clicks > 1:
        print(f"  触发动作: {cfg['action']} × {n_clicks} "
              f"(每 {cfg.get('click_interval_ms', 80)}ms)")
    else:
        print(f"  触发动作: {cfg['action']}")
    print(f"  连续命中门槛: {cfg.get('min_strikes', 3)} 块 (≈ "
          f"{int(cfg.get('min_strikes', 3)) * 50}ms)")
    fw = cfg.get('foreground_window', '') or ''
    print(f"  前台窗口限制: {fw if fw else '（无，切出游戏也会按键，请慎用）'}")
    print("  听到咬钩音效 -> 按键 | F9 手动抛竿 | F10 退出")
    print("=" * 56)

    mic, rate = open_loopback(cfg)
    tmpl = trim_silence(load_wav_mono(wav, rate), rate)
    det = SoundDetector(tmpl, rate)
    buf = RollingBuffer(len(tmpl) + int(rate * 0.2))
    chunk = max(int(rate * 0.05), 1024)

    last_press = 0.0
    with mic.recorder(samplerate=rate, blocksize=chunk) as rec:
        while running["on"]:
            data = rec.record(numframes=chunk)
            mono = data.mean(axis=1) if data.ndim > 1 else data[:, 0]
            buf.push(mono)
            score = det.best_score(buf.buf)
            now = time.time()
            if score >= float(cfg["threshold"]) and now - last_press >= float(cfg["cooldown"]):
                do_action(cfg["action"])
                last_press = now
                print(f"[{time.strftime('%H:%M:%S')}] 检测到咬钩音效 "
                      f"(得分 {score:.3f})，已触发 {cfg['action']}")
    print("已退出，祝钓鱼愉快！")


def run_self_test(cfg, wav):
    """自测：播放参考音效，验证 loopback 捕获 + 互相关检测全链路。"""
    import threading
    init_com()
    print("[自测] 打开 loopback ...")
    mic, rate = open_loopback(cfg)
    tmpl = trim_silence(load_wav_mono(wav, rate), rate)
    print(f"[自测] 模板 {len(tmpl)/rate:.2f}s @ {rate}Hz")
    det = SoundDetector(tmpl, rate)
    buf = RollingBuffer(len(tmpl) + int(rate * 1.0))

    def play():
        time.sleep(0.5)
        print("[自测] 播放参考音效 ...")
        play_file(wav, 48000)  # 内部会初始化该线程的 COM

    threading.Thread(target=play, daemon=True).start()

    best = 0.0
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
                print(f"[自测] ✓ 检测成功！得分 {s:.3f} (阈值 {cfg['threshold']})")
                return True
    print(f"[自测] ✗ 5 秒内未检测到（最高得分 {best:.3f}）。"
          f"请确认参考音效从默认输出设备播放。")
    return False


if __name__ == "__main__":
    main()
