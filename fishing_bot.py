# -*- coding: utf-8 -*-
"""
钓鱼浮标监控挂机脚本
====================
原理：定时截取设定的屏幕区域，检测浮标颜色是否还在。
      一旦区域内看不到该颜色（浮标下沉/消失），立即按下设定的键（默认鼠标左键）。

使用方法：
  1. 校准（二选一）：
     a) 运行: python fishing_bot.py --calibrate
        把鼠标悬停在游戏里浮标的颜色上，按 F9 采样 —— 会自动记录颜色，
        并把监控区域设为鼠标周围的一个方框，写入 config.json。
     b) 直接手改 config.json（region = [x, y, w, h]，color = [R, G, B]）。
  2. 启动: python fishing_bot.py（会自动申请管理员权限，游戏全屏/反激活时也能正常按键）
  3. 挂机中：F10 随时退出，F9 手动强制抛竿一次。

依赖：mss、keyboard（已安装在随附虚拟环境中）
"""

import argparse
import ctypes
import json
import os
import sys
import time

# ---------------- 配置 ----------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    # 监控区域 [x, y, w, h]（屏幕像素坐标，左上角为原点）
    "region": [960, 540, 120, 120],
    # 浮标颜色 [R, G, B]
    "color": [255, 60, 60],
    # 颜色容差（每个通道允许的偏差，越大越宽松）
    "tolerance": 30,
    # 检测间隔（秒），越小反应越快
    "interval": 0.05,
    # 触发动作："click" = 鼠标左键点击（在当前光标位置）；或填键名如 "f"、"space"
    "action": "click",
    # 两次触发之间的最短冷却（秒），防止连点
    "cooldown": 1.5,
    # 校准时，以鼠标为中心生成的监控方框边长（像素）
    "calib_size": 120,
}

# ---------------- Win32 常量与结构 ----------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_F9 = 0x78


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]


def send_left_click():
    """在当前光标位置发送一次鼠标左键点击（SendInput，对游戏更可靠）。"""
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        inp = INPUT(type=INPUT_MOUSE)
        inp.union.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


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


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate_and_exit():
    """以管理员身份重新启动自身（触发 UAC 弹窗）。"""
    params = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1)
    if ret <= 32:
        print("[错误] 管理员权限申请被拒绝，无法继续。")
    sys.exit(0 if ret > 32 else 1)


def color_present(mss_obj, region, rgb, tolerance):
    """检测区域内是否存在目标颜色。返回 True/False。"""
    x, y, w, h = region
    monitor = {"left": x, "top": y, "width": w, "height": h}
    try:
        img = mss_obj.grab(monitor)
    except Exception:
        return True  # 抓屏失败时保守处理，不触发按键
    # mss 返回 BGRA 字节流，步长 = width * 4
    tr, tg, tb = rgb
    data = img.raw
    stride = w * 4
    for row in range(h):
        base = row * stride
        for col in range(w):
            i = base + col * 4
            b, g, r = data[i], data[i + 1], data[i + 2]
            if (abs(r - tr) <= tolerance and
                    abs(g - tg) <= tolerance and
                    abs(b - tb) <= tolerance):
                return True
    return False


def do_action(action):
    if action == "click":
        send_left_click()
    else:
        import keyboard
        keyboard.press_and_release(action)


def calibrate(cfg):
    """校准模式：鼠标悬停在浮标上按 F9，采样颜色并生成监控区域。"""
    import keyboard
    import mss
    print("=" * 50)
    print("校准模式")
    print("1) 把鼠标移到游戏里浮标的颜色上")
    print("2) 按 F9 采样（以鼠标为中心生成监控方框）")
    print("3) 按 ESC 取消")
    print("=" * 50)
    size = cfg["calib_size"]
    with mss.MSS() as sct:
        print("等待 F9 ...")
        while True:
            if keyboard.is_pressed("f9"):
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                # 采样鼠标正下方 1x1 区域的颜色
                px = sct.grab({"left": pt.x, "top": pt.y, "width": 1, "height": 1})
                b, g, r = px.raw[0], px.raw[1], px.raw[2]
                half = size // 2
                cfg["color"] = [r, g, b]
                cfg["region"] = [max(pt.x - half, 0), max(pt.y - half, 0), size, size]
                save_config(cfg)
                print(f"[OK] 已采样颜色 RGB({r},{g},{b})，"
                      f"监控区域 = 以 ({pt.x},{pt.y}) 为中心的 {size}x{size} 方框")
                print(f"[OK] 已写入 {CONFIG_FILE}，现在可以运行 fishing_bot.py 挂机。")
                break
            if keyboard.is_pressed("esc"):
                print("[取消] 未做任何修改。")
                break
            time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser(description="钓鱼浮标监控挂机脚本")
    parser.add_argument("--calibrate", action="store_true",
                        help="校准模式：鼠标悬停在浮标上按 F9 采样颜色")
    parser.add_argument("--no-elevate", action="store_true",
                        help="跳过管理员权限申请（仅供测试）")
    args = parser.parse_args()

    cfg = load_config()

    # 游戏通常前台独占输入，需要管理员权限才能全局注入按键
    if not args.no_elevate and not is_admin():
        print("[提示] 正在申请管理员权限 ...")
        elevate_and_exit()

    if args.calibrate:
        calibrate(cfg)
        return

    import keyboard
    import mss

    running = {"on": True}

    def quit_bot():
        running["on"] = False

    keyboard.add_hotkey("f10", quit_bot)
    keyboard.add_hotkey("f9", lambda: do_action(cfg["action"]))  # 手动抛竿

    region = cfg["region"]
    rgb = cfg["color"]
    tol = cfg["tolerance"]
    print("=" * 50)
    print("钓鱼挂机已启动")
    print(f"  监控区域: x={region[0]} y={region[1]} "
          f"w={region[2]} h={region[3]}")
    print(f"  浮标颜色: RGB{tuple(rgb)}  容差: {tol}")
    print(f"  触发动作: {cfg['action']}  冷却: {cfg['cooldown']}s")
    print("  颜色消失 -> 按键 | F9 手动抛竿 | F10 退出")
    print("=" * 50)

    last_press = 0.0
    with mss.MSS() as sct:
        while running["on"]:
            try:
                present = color_present(sct, region, rgb, tol)
                now = time.time()
                if not present and now - last_press >= cfg["cooldown"]:
                    do_action(cfg["action"])
                    last_press = now
                    print(f"[{time.strftime('%H:%M:%S')}] 浮标颜色消失，已触发 {cfg['action']}")
                time.sleep(cfg["interval"])
            except KeyboardInterrupt:
                break
    print("已退出，祝钓鱼愉快！")


if __name__ == "__main__":
    import ctypes.wintypes  # noqa: F401（calibrate 中使用）
    main()
