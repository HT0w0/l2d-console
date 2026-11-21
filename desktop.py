"""桌面窗口启动器：把控制台装进一个原生窗口，双击就能用，不需要开浏览器、也不要点 bat。

用法：
    pythonw desktop.py        # 平时这样（无控制台窗口）
    python  desktop.py --debug # 调试时这样（能看到日志输出）

它会做三件事：
1. 在后台起 app.py（HTTP 服务）；
2. 用 WebView2 开一个原生窗口指向它（没有地址栏、没有标签页）；
3. 窗口关掉时，把后台服务一起收掉。

依赖：pywebview（WebView2 版）。没装也不会崩——会退化成用 Edge/Chrome 的 --app 模式开一个无边框窗口。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TITLE = "大肥鱼 · 直播 / 陪看 控制台"


def _console_port() -> int:
    cfg_path = ROOT / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return int(data.get("console_port", 61950))
    except Exception:
        return 61950


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _wait_ready(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
        time.sleep(0.35)
    return False


def _start_server(port: int):
    """后台起 HTTP 服务。端口已经被占（比如已经开着一个）就不再起第二个。

    打包成 exe 时没有 app.py 这个文件可执行，所以改成在同一进程的线程里起服务；
    源码运行时仍然用子进程（关窗时能干净地收掉，也不污染当前进程）。
    """
    if _port_open(port):
        return None

    if getattr(sys, "frozen", False):
        import importlib
        import threading
        from http.server import ThreadingHTTPServer

        mod = importlib.import_module("app")  # 冻结时 app 已作为模块打进去
        server = ThreadingHTTPServer(("127.0.0.1", port), mod.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [sys.executable, str(ROOT / "app.py"), "--port", str(port)],
        cwd=str(ROOT),
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_native_window(url: str) -> None:
    import webview  # pywebview

    webview.create_window(
        TITLE,
        url,
        width=1180,
        height=820,
        min_size=(940, 640),
        background_color="#0b111a",
    )
    webview.start()


def _open_browser_app_mode(url: str) -> None:
    """兜底：Edge / Chrome 的 --app 模式，外观也是无地址栏的独立窗口。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    exe = next((p for p in candidates if Path(p).exists()), None)
    if not exe:
        raise SystemExit("没找到 Edge/Chrome，也没有 pywebview 可用。")
    profile = Path.home() / "AppData" / "Local" / "dsh-l2d-console-app"
    subprocess.Popen(
        [
            exe,
            f"--app={url}",
            "--window-size=1180,820",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )
    # --app 模式没有 window 句柄可等，直接等它退（按进程名很脏，这里就等一个足够长的时间）
    try:
        while True:
            time.sleep(1)
            if not Path(profile, "DevToolsActivePort").exists() and not _port_open(_console_port()):
                break
    except KeyboardInterrupt:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="以桌面窗口方式打开控制台")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--fallback-browser", action="store_true", help="强制用浏览器 --app 模式")
    args = parser.parse_args()

    port = args.port or _console_port()
    url = f"http://127.0.0.1:{port}"

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HT0w0.L2DConsole")
        except Exception:
            pass

    server = _start_server(port)
    try:
        if not _wait_ready(port):
            print("控制台服务没能起来，请用命令行跑 python app.py 看报错。", file=sys.stderr)
            return 1

        if args.fallback_browser:
            _open_browser_app_mode(url)
        else:
            try:
                _open_native_window(url)
            except Exception as exc:  # pywebview 没装 / WebView2 不可用
                print(f"原生窗口起不来（{type(exc).__name__}: {exc}），改用浏览器窗口。", file=sys.stderr)
                _open_browser_app_mode(url)
    finally:
        if server is not None:
            if hasattr(server, "shutdown"):      # 进程内线程模式
                server.shutdown()
            elif server.poll() is None:          # 子进程模式
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
