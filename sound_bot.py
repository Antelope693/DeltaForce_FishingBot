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


def do_action(action):
    if action == "click":
        send_left_click()
    else:
        import keyboard
        keyboard.press_and_release(action)


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


def open_loopback(cfg):
    """按配置打开 loopback 录音设备。返回 (mic, rate)。"""
    import soundcard as sc
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
    import soundcard as sc

    running = {"on": True}
    keyboard.add_hotkey("f10", lambda: running.update(on=False))
    keyboard.add_hotkey("f9", lambda: do_action(cfg["action"]))  # 手动抛竿

    print("=" * 56)
    print("钓鱼挂机（声音版）已启动")
    print(f"  参考音效: {os.path.basename(wav)}")
    print(f"  得分阈值: {cfg['threshold']}  冷却: {cfg['cooldown']}s")
    print(f"  触发动作: {cfg['action']}")
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
    import soundcard as sc
    print("[自测] 打开 loopback ...")
    mic, rate = open_loopback(cfg)
    tmpl = trim_silence(load_wav_mono(wav, rate), rate)
    print(f"[自测] 模板 {len(tmpl)/rate:.2f}s @ {rate}Hz")
    det = SoundDetector(tmpl, rate)
    buf = RollingBuffer(len(tmpl) + int(rate * 1.0))

    def play():
        time.sleep(0.5)
        print("[自测] 播放参考音效 ...")
        sc.default_speaker().play(load_wav_mono(wav, 48000) / max(
            1e-9, np.abs(load_wav_mono(wav, 48000)).max()), samplerate=48000)

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
