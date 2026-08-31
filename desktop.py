"""大眼桌面壳：原生 Win32 窗口 + WebView2 内核，直接加载现有 Web 前端。

双击 大眼.bat 启动：无终端黑窗、不打开浏览器，前端只维护一套。
- 服务未运行时自动后台拉起 server.py（无控制台窗口）
- 窗口关闭时，仅终止本壳拉起的 server（用户手动起的不动）
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

PORT = 9890
# frozen 时 BASE = exe 所在目录（onedir），_MEIPASS 是打包临时目录，数据须持久化到 exe 旁
BASE = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(BASE, "图标.ico")
HEALTH = f"http://127.0.0.1:{PORT}/api/health"


_hwnd = None   # 主窗口句柄，供 JS API 拖拽/最大化用
_window = None  # pywebview 窗口对象
_splash = {"status": "正在启动", "done": False}  # 启动提示窗共享状态
_splash_closed = threading.Event()  # 卡片实际销毁信号：主窗口等它再显示，杜绝同屏跳动



def _splash_thread():
    """独立线程跑启动提示窗：双击后立刻有反馈，主窗口加载完立即消失。

    tkinter 全部调用只在本线程内；主线程通过 _splash 字典传状态（GIL 安全）。
    视觉：圆角深色卡片 + 单调递增进度条 + 状态小字；位置固定、淡入出现，
    主窗口加载完成立即销毁（不淡出，避免被主窗口弹出顶跳）。
    """
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        root = tk.Tk()
    except Exception:
        _splash_closed.set()  # 无 tkinter 环境静默跳过，放行主窗口
        return
    try:
        W, H, R = 420, 120, 14
        BG, EDGE, ACCENT, DIM = "#161616", "#2a2a2a", "#2ee6c0", "#8a8f98"
        MAGIC = "#010101"  # 透明键色，卡片圆角外区域透明
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=MAGIC)
        try:
            root.attributes("-transparentcolor", MAGIC)
        except Exception:
            pass
        root.update_idletasks()
        x0 = (root.winfo_screenwidth() - W) // 2
        y0 = (root.winfo_screenheight() - H) // 2
        root.geometry(f"{W}x{H}+{x0}+{y0}")  # 位置固定，动画只改透明度，避免跳动
        root.attributes("-alpha", 0.0)

        cv = tk.Canvas(root, width=W, height=H, bg=MAGIC, highlightthickness=0)
        cv.pack()
        # 圆角卡片：两矩形 + 四角扇形填充，边缘用弧线和直线描边
        cv.create_rectangle(R, 0, W - R, H, fill=BG, outline="")
        cv.create_rectangle(0, R, W, H - R, fill=BG, outline="")
        corners = ((R, R, 90), (W - R, R, 0), (W - R, H - R, 270), (R, H - R, 180))
        for cx, cy, a0 in corners:
            cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=a0, extent=90, fill=BG, outline="")
            cv.create_arc(cx - R, cy - R, cx + R, cy + R, start=a0, extent=90, style="arc", outline=EDGE)
        cv.create_line(R, .5, W - R, .5, fill=EDGE)
        cv.create_line(R, H - .5, W - R, H - .5, fill=EDGE)
        cv.create_line(.5, R, .5, H - R, fill=EDGE)
        cv.create_line(W - .5, R, W - .5, H - R, fill=EDGE)
        f_s = tkfont.Font(family="Microsoft YaHei", size=9)
        # 进度条：2px 轨道 + 单调递增填充（渐近 92%，完成即满格关闭）
        TX1, TX2, TY = 110, 310, 46
        cv.create_rectangle(TX1, TY, TX2, TY + 3, fill="#262626", outline="")
        seg = cv.create_rectangle(TX1, TY, TX1, TY + 3, fill=ACCENT, outline="")
        st = cv.create_text(W / 2, 80, text="", fill=DIM, font=f_s)

        A = {"v": 0.0, "p": 0.0}
        t0 = time.time()
        pos0 = None

        def close(reason="done"):
            try:
                # withdraw 同步调系统 ShowWindow(SW_HIDE)，卡片立刻不可见；
                # destroy 的实际摘窗可能滞后 1~2 帧，不能等它再放行主窗口
                root.withdraw()
                root.destroy()
            finally:
                _splash_log(f"card close reason={reason}")
                _splash_closed.set()

        def tick(n=0):
            nonlocal pos0
            if _splash["done"] or time.time() - t0 > 140:
                close("done" if _splash["done"] else "timeout")  # 完成立即关
                return
            if pos0 is None:
                pos0 = (root.winfo_x(), root.winfo_y())
            elif (root.winfo_x(), root.winfo_y()) != pos0:
                close("moved")  # 位置被系统/主窗口顶动 → 立即关，绝不让它跳
                return
            if A["v"] < 1.0:
                A["v"] = min(1.0, A["v"] + 0.08)
                root.attributes("-alpha", A["v"])
            A["p"] = min(0.92, A["p"] + (0.92 - A["p"]) * 0.015 + 0.0012)
            cv.coords(seg, TX1, TY, TX1 + (TX2 - TX1) * A["p"], TY + 3)
            cv.itemconfig(st, text=_splash["status"] + "." * (n // 15 % 3 + 1))
            root.after(30, tick, n + 1)

        root.after(30, tick)
        root.mainloop()
    except Exception:
        pass
    finally:
        _splash_closed.set()  # 任何退出路径都放行主窗口


def _splash_log(msg):
    """启动交接时序日志，复发"卡片跳"时可查证。"""
    try:
        with open(os.path.join(BASE, "data", "splash_debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {msg}\n")
    except Exception:
        pass


def start_splash():
    threading.Thread(target=_splash_thread, daemon=True).start()


def _set_window_icon():
    """窗口加载后按进程 ID 找到 Win32 窗口：设置图标 + 去掉标题栏（保留缩放边框）。"""
    global _hwnd
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        # 任务栏独立分组，不挂到 python.exe 下
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bigeye.desktop")
        pid = os.getpid()
        cands = []  # (面积, hwnd)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def enum_cb(hwnd, _):
            # 认主窗口：hidden 创建不可见，不能按可见性过滤；标题会被前端
            # 改成任务名，不能按标题过滤；进程内还有 WinForms/WebView2 的
            # 隐藏辅助顶层窗口，不能全收。取面积最大的顶层窗口=主窗口。
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                cands.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
            return True

        user32.EnumWindows(enum_cb, 0)
        cands.sort(reverse=True)
        hwnds = [h for _, h in cands[:1]]
        LR_LOADFROMFILE = 0x0010
        GWL_STYLE = -16
        WS_CAPTION = 0x00C00000   # 标题栏（含系统按钮）
        WS_THICKFRAME = 0x00040000  # 边缘拖拽缩放
        for hwnd in hwnds:
            _hwnd = hwnd
            small = user32.LoadImageW(None, ICON, 1, 16, 16, LR_LOADFROMFILE)
            big = user32.LoadImageW(None, ICON, 1, 32, 32, LR_LOADFROMFILE)
            if small:
                user32.SendMessageW(hwnd, 0x80, 0, small)  # WM_SETICON ICON_SMALL
            if big:
                user32.SendMessageW(hwnd, 0x80, 1, big)   # WM_SETICON ICON_BIG
            # 去标题栏、保留缩放边框
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            style = (style & ~WS_CAPTION) | WS_THICKFRAME
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
            SWP = 0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP)
    except Exception:
        pass


def _set_maximized_bounds():
    """把 WinForms 窗体的 MaximizedBounds 设为所在显示器工作区。

    行业通用解法（WinForms 社区多源验证）：Form.MaximizedBounds 会让
    窗体自己的 WndProc 在 WM_GETMINMAXINFO 里把最大化范围约束到工作区。
    按钮/双击/拖顶/Win+↑ 所有最大化路径都经过它，且保留全部原生行为
    （下拉还原、吸附、动画）。ctypes 子类化钩子改的值会被托管层覆盖，
    所以必须走托管属性。仅 UI 线程调用。
    """
    try:
        from System import IntPtr
        from System.Drawing import Rectangle
        from System.Windows.Forms import Application, Screen
        scr = Screen.FromHandle(IntPtr(_hwnd))
        wa = scr.WorkingArea
        # 遍历 OpenForms 按句柄匹配拿 Form 实例（pythonnet 可直接操作托管对象）
        form = None
        for f in Application.OpenForms:
            if f.Handle.ToInt64() == _hwnd:
                form = f
                break
        if form is not None:
            form.MaximizedBounds = Rectangle(wa.X, wa.Y, wa.Width, wa.Height)
    except Exception as e:
        _splash_log(f"maximized_bounds err: {e!r}")


def _on_loaded():
    """页面加载完：先改样式再显示，标题栏不闪现。卡片早已在 webview.start() 前销毁。"""
    _set_window_icon()
    _set_maximized_bounds()
    if _window:
        _window.show()


def _do_native_drag():
    """在 UI 线程上释放捕获并发起系统标题栏拖拽。"""
    import ctypes
    user32 = ctypes.windll.user32
    user32.ReleaseCapture()
    user32.SendMessageW(_hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION



class WinApi:
    """暴露给前端 window.pywebview.api 的窗口控制。"""

    def minimize(self):
        if _window:
            _window.minimize()

    def toggle_max(self):
        """系统真最大化/还原（尺寸已被 WM_GETMINMAXINFO 约束到工作区，不盖任务栏）。

        ShowWindow 同步生效，IsZoomed 立即反映新状态，直接读即可。
        """
        if not _hwnd:
            return False
        import ctypes
        user32 = ctypes.windll.user32
        user32.ShowWindow(_hwnd, 3 if not user32.IsZoomed(_hwnd) else 9)  # SW_MAXIMIZE/SW_RESTORE
        return bool(user32.IsZoomed(_hwnd))

    def is_maximized(self):
        import ctypes
        return bool(_hwnd and ctypes.windll.user32.IsZoomed(_hwnd))

    def close(self):
        if _window:
            _window.destroy()

    def start_drag(self):
        """原生标题栏拖拽：系统接管移动，自带 Win 手势（左右边缘分屏/拖顶最大化）。

        js_api 在后台线程执行，而 ReleaseCapture 只释放"调用线程"的捕获——
        鼠标捕获在 UI 线程的 WebView2 子窗口上，必须 marshal 回 UI 线程再发，
        否则系统拖拽循环收不到鼠标移动，窗口不动。
        """
        if not _hwnd:
            return
        try:
            import clr  # noqa: F401  pywebview 已加载 pythonnet
            from System import IntPtr, Action
            from System.Windows.Forms import Control
            ctl = Control.FromHandle(IntPtr(_hwnd))
            if ctl is not None:
                ctl.Invoke(Action(_do_native_drag))
                return
        except Exception:
            pass
        _do_native_drag()  # 兜底：直接调（理论上仅 UI 线程调用时有效）


def server_alive():
    try:
        with urllib.request.urlopen(HEALTH, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _server_cmd():
    """返回拉起后端的命令。
    frozen 后 sys.executable 是 desktop.exe 而非解释器，不能跑 server.py；
    需改为启动同目录（onedir）的 server.exe。"""
    if getattr(sys, 'frozen', False):
        server_exe = os.path.join(BASE, 'server.exe')
        if not os.path.isfile(server_exe):
            raise FileNotFoundError(f"找不到 server.exe: {server_exe}")
        return [server_exe, '--port', str(PORT)]
    return [sys.executable, '-u', 'server.py', '--port', str(PORT)]


def main():
    start_splash()  # 先起提示窗，再 import webview（导入本身也要几秒）
    import webview

    spawned = None
    if not server_alive():
        _splash["status"] = "正在启动服务"
        # 日志落盘，崩溃可查（与重启脚本同款带时间戳文件）
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_dir = os.path.join(BASE, "data")
        os.makedirs(log_dir, exist_ok=True)
        out = open(os.path.join(log_dir, f"server_stdout_{ts}.log"), "a", encoding="utf-8")
        err = open(os.path.join(log_dir, f"server_stderr_{ts}.log"), "a", encoding="utf-8")
        spawned = subprocess.Popen(
            _server_cmd(),
            cwd=BASE,
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(120):  # 首次启动要加载模型，最多等 120 秒
            if server_alive():
                break
            if spawned.poll() is not None:
                print("server 启动失败，见 data/server_stderr_*.log")
                _splash["status"] = "服务启动失败，见 data/server_stderr_*.log"
                time.sleep(3)  # 让用户看清提示再退出
                sys.exit(1)
            time.sleep(1)
        else:
            print("server 启动超时")
            spawned.terminate()
            _splash["status"] = "服务启动超时"
            time.sleep(3)
            sys.exit(1)

    _splash["status"] = "正在加载界面"
    global _window
    _window = webview.create_window(
        "大眼",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=860,
        min_size=(720, 480),
        js_api=WinApi(),
        hidden=True,  # 先隐藏创建，样式就绪后再显示，避免标题栏闪现
        text_select=True,  # pywebview 默认注入 body{user-select:none} 禁全页选择，关掉才能复制对话文字
    )
    _window.events.loaded += _on_loaded
    # 卡片必须在 webview.start() 前彻底销毁：webview 初始化 UI 框架会扰动
    # 同进程还活着的 tkinter 卡片（表现为卡片跳变）。代价是卡片关闭后有一
    # 小段页面加载空白期，本地页面加载很快，可接受。
    _splash["done"] = True
    _splash_closed.wait(timeout=1)
    _splash_log("main show")
    # private_mode 默认 True 会丢弃 localStorage/cookies，主题等设置重启即失；
    # 关闭后持久化到 %APPDATA%/pywebview。
    webview.start(private_mode=False)  # 阻塞直到窗口关闭（窗口 hidden 创建，loaded 后才显示）

    if spawned is not None:
        spawned.terminate()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw 无控制台，崩溃写盘便于排查
        import traceback
        try:
            log_dir = os.path.join(BASE, "data")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, "desktop_crash.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S\n"))
                traceback.print_exc(file=f)
                f.write("\n")
        except Exception:
            pass
        raise
