# -*- coding: utf-8 -*-
"""
钓鱼咬钩声音监控挂机脚本
========================
原理：持续捕获系统声音（WASAPI loopback），与参考音效（TIMETOUP.wav）
      做归一化互相关匹配。一旦听到咬钩音效，自动执行一轮收鱼流程。

一轮流程（run_fishing_sequence）：
  1. 左键抬杆（播放轻柔提示音）
  2. 等待 interrupt_delay 秒（默认 2s）
  3. 左键打断检视
  4. 等待 recast_delay 秒（默认 4s）
  5. 左键再次抛竿

点击驱动：仅 mouse_event（实测三角洲里只有它不被吞）。

用法（命令行）：
  python sound_bot.py           （自动申请管理员权限，F9 手动抛竿，F10 退出）
  python sound_bot.py --no-elevate

依赖：numpy、soundcard
"""

import argparse
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time
import wave

import numpy as np
import soundcard as sc

# 说明：soundcard 必须在**主线程**完成导入——它在导入时会初始化所在线程的 COM，
# 且无法容忍"本线程已初始化过"（会抛 Error 0x100000001）。
# 所以：主线程先导入（本行），其他工作线程使用前调用 init_com() 单独初始化。

# ---------------- 配置 ----------------
def app_dir():
    """程序所在目录。打包成单文件 exe 后，config.json 和 TIMETOUP.wav
    应放在 exe 旁边（而不是解包临时目录），这样用户的配置和音频能保留。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_FILE = os.path.join(app_dir(), "config.json")

DEFAULT_CONFIG = {
    # 参考音效文件（自带 TIMETOUP.wav，一般无需改）
    "sound_file": "TIMETOUP.wav",
    # 归一化互相关得分阈值（0~1，越高越严格）
    "threshold": 0.35,
    # 两次触发之间的最短冷却（秒）
    "cooldown": 1.5,
    # ---- 收鱼流程 ----
    # 抬杆后等待多少秒再点一下打断检视
    "interrupt_delay": 2.0,
    # 打断检视后再等多少秒左键抛下一竿
    "recast_delay": 4.0,
    # 抬杆时是否播放轻柔提示音
    "notify_sound": True,
    # ---- 防切走误点 ----
    # 仅当当前前台窗口标题含此子串时才点击；空 = 不限制（强烈不推荐）。
    "foreground_window": "三角洲",
    # 采样率（一般无需改动；loopback 常见为 48000）
    "samplerate": 48000,
    # 音频输出设备名（null = 系统默认输出；填设备名子串可指定如 "耳机"）
    "device": None,
}

# ---------------- Win32 常量 ----------------
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004

# ---------------- 点击 / 前台窗口 ----------------
def click_mouse():
    """左键单击（mouse_event，实测三角洲里唯一可用的驱动）。"""
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.01)  # 10ms DOWN/UP 间隔，避免系统合并
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return True


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


def foreground_ok(foreground_window):
    """前台窗口是否匹配白名单。空 = 不限制。"""
    if not foreground_window:
        return True
    fg = get_foreground_title()
    if foreground_window.lower() not in fg.lower():
        print(f"[跳过] 前台窗口不符（当前={fg!r}，需含 {foreground_window!r}）")
        return False
    return True


def find_window_by_title_sub(sub, prefer_foreground=True):
    """
    在所有可见顶层窗口中查找标题含 `sub`（不区分大小写）的窗口。
    返回 (hwnd, title) 或 (0, "")。
    """
    if not sub:
        return 0, ""
    sub_l = sub.lower()
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    found = []

    def cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value or ""
        if sub_l in title.lower():
            found.append((hwnd, title))
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(cb), 0)
    if not found:
        return 0, ""
    if prefer_foreground:
        fg = ctypes.windll.user32.GetForegroundWindow()
        for hwnd, title in found:
            if hwnd == fg:
                return hwnd, title
    return found[0]


# ---------------- 抬杆提示音 ----------------
def play_notify_sound(volume=0.22):
    """
    抬杆时播放的轻柔提示音：一声短促、快速衰减的"叮~"（约 0.2 秒）。
    音量 0.22，刻意压低，不会吵。
    非阻塞：内部起线程播放。
    """
    def _work():
        try:
            init_com()
            rate = 44100
            dur = 0.20
            t = np.arange(int(rate * dur)) / rate
            env = np.exp(-t * 16)  # 指数衰减包络，柔和不刺耳
            audio = (0.7 * np.sin(2 * np.pi * 988.0 * t) * env
                     ).astype(np.float32) * float(volume)
            sc.default_speaker().play(audio, samplerate=rate)
        except Exception as e:
            print(f"[提示音] 播放失败: {e}")

    threading.Thread(target=_work, daemon=True).start()


# ---------------- 收鱼流程 ----------------
def run_fishing_sequence(cfg, should_stop=None):
    """
    执行一轮完整收鱼流程：
      1. 左键抬杆（+ 提示音）
      2. 等 interrupt_delay 秒
      3. 左键打断检视
      4. 等 recast_delay 秒
      5. 左键抛下一竿
    每次点击前都会重新检查前台窗口白名单；流程中若 should_stop() 返回
    True（如挂机被停止）则立即中止。返回 [(步骤名, 是否执行), ...]。
    """
    fw = str(cfg.get("foreground_window", "") or "")
    d1 = max(0.0, float(cfg.get("interrupt_delay", 2.0)))
    d2 = max(0.0, float(cfg.get("recast_delay", 4.0)))
    notify = bool(cfg.get("notify_sound", True))
    steps = []

    def _wait(sec):
        end = time.time() + sec
        while time.time() < end:
            if should_stop is not None and should_stop():
                return False
            time.sleep(0.05)
        return True

    # 1. 抬杆（流程开始前也检查一次停止，避免刚停止挂机还多点一下）
    if should_stop is not None and should_stop():
        return steps
    if not foreground_ok(fw):
        steps.append(("抬杆", False))
        return steps
    click_mouse()
    if notify:
        play_notify_sound()
    steps.append(("抬杆", True))
    # 2. 等待 -> 3. 打断检视
    if not _wait(d1):
        return steps
    if not foreground_ok(fw):
        steps.append(("打断检视", False))
        return steps
    click_mouse()
    steps.append(("打断检视", True))
    # 4. 等待 -> 5. 再次抛竿
    if not _wait(d2):
        return steps
    if not foreground_ok(fw):
        steps.append(("抛竿", False))
        return steps
    click_mouse()
    steps.append(("抛竿", True))
    return steps


def sequence_duration(cfg):
    """一轮收鱼流程的总时长（秒），供调用方设置抑制窗口。"""
    return (max(0.0, float(cfg.get("interrupt_delay", 2.0)))
            + max(0.0, float(cfg.get("recast_delay", 4.0))) + 0.5)


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


def elevate_args():
    """
    返回提权重启时应传给新进程的参数列表。

    注意：打包成 exe 后 sys.argv[0] 是 **exe 自身路径**，它不能再当作参数传回去，
    否则新进程的 argparse 会把它当成非法位置参数并报
    "unrecognized arguments: ...\\xxx.exe" 直接退出。
    脚本模式下 sys.argv[0] 是脚本路径，则必须保留（python.exe 需要它才能跑）。
    """
    if getattr(sys, "frozen", False):
        return list(sys.argv[1:])
    return [os.path.abspath(sys.argv[0])] + list(sys.argv[1:])


def elevate_and_exit():
    params = " ".join(f'"{a}"' for a in elevate_args())
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params or None, None, 1)
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

    def clear(self):
        self.buf = np.zeros(0, dtype=np.float32)

    def __len__(self):
        return len(self.buf)


def resolve_sound_file(cfg):
    p = cfg["sound_file"]
    if not os.path.isabs(p):
        p = os.path.join(app_dir(), p)
    return p


def play_file(path, samplerate=48000):
    """在调用线程内播放一个 wav（线程安全：内部会初始化 COM）。"""
    init_com()
    audio = load_wav_mono(path, samplerate)
    peak = float(np.abs(audio).max()) or 1.0
    sc.default_speaker().play(audio / peak, samplerate=samplerate)


def run_diag(cfg):
    """
    环境诊断：逐步检查 COM、设备枚举、录音、播放、检测链路，
    返回每一步的结果，便于定位"自测失败"到底卡在哪。
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

    # 窗口检查（前台白名单 / 游戏窗口）
    def _win():
        fw = cfg.get("foreground_window", "") or ""
        hwnd, title = find_window_by_title_sub(fw)
        fg = get_foreground_title()
        return (f"前台: {fg!r} | 游戏窗口: "
                + (f"hwnd={hwnd:#x} {title!r}" if hwnd else "未找到"))
    record("窗口检查", _win)

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
        raise RuntimeError(f"找不到含 「{kw}」 的输出设备")
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
    keyboard.add_hotkey("f9", lambda: click_mouse())  # 手动抛竿

    print("=" * 60)
    print("钓鱼挂机（声音版）已启动")
    print(f"  参考音效: {os.path.basename(wav)}（自带）")
    print(f"  得分阈值: {cfg['threshold']}  冷却: {cfg['cooldown']}s")
    print(f"  点击驱动: mouse_event（唯一可用）")
    print(f"  流程: 抬杆 → 等 {cfg.get('interrupt_delay', 2.0)}s 打断检视 → "
          f"等 {cfg.get('recast_delay', 4.0)}s 抛竿")
    print(f"  抬杆提示音: {'开' if cfg.get('notify_sound', True) else '关'}")
    fw = cfg.get("foreground_window", "") or ""
    if fw:
        print(f"  前台窗口限制: 含 {fw!r} 才按键")
        fg_now = get_foreground_title()
        print(f"  当前前台: {fg_now!r}  → "
              f"{'✓ 会触发' if fw in fg_now else '✗ 不会触发（切回游戏！）'}")
    else:
        print("  前台窗口限制: (无 —— 任何前台窗口都会按键！)")
    print("  听到咬钩音效 -> 自动收鱼 | F9 手动抛竿 | F10 退出")
    print("=" * 60)

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
            threshold = float(cfg["threshold"])
            cooldown = float(cfg["cooldown"])
            now = time.time()
            if score >= threshold and now - last_press >= cooldown:
                last_press = now
                print(f"[{time.strftime('%H:%M:%S')}] 检测到咬钩音效 "
                      f"(得分 {score:.3f})，开始收鱼流程")
                buf.clear()  # 清空缓冲，避免同一段声音重复触发
                run_fishing_sequence(cfg, should_stop=lambda: not running["on"])
    print("已退出，祝钓鱼愉快！")


def run_self_test(cfg, wav):
    """自测：播放参考音效，验证 loopback 捕获 + 互相关检测全链路。"""
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
