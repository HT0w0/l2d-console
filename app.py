#!/usr/bin/env python3
"""L2D 直播 / 陪看 控制台。

一个不依赖任何第三方库的本地小应用：把散落在几个脚本里的功能
（工作台、语音引擎、弹幕桥、自动回复、直播值守、陪看浏览器）
收成一块面板，点按钮就能开关，并实时显示状态。

只做编排：真正的渲染、合成、识别仍然由被启动的那些程序负责。

用法：
    python app.py            # 打开 http://127.0.0.1:61950
    python app.py --port 62000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if getattr(sys, "frozen", False):
    # 打包成 exe 后：可写的东西（config.json / logs）放在 exe 旁边，
    # 只读的资源（ui.html）在 PyInstaller 的解包目录里。
    BASE = Path(sys.executable).resolve().parent
    ASSETS = Path(getattr(sys, "_MEIPASS", BASE))
else:
    BASE = ASSETS = Path(__file__).resolve().parent

ROOT = BASE
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# --------------------------------------------------------------------------- 配置

def load_config() -> dict:
    candidates = [ROOT / "config.json", ASSETS / "config.json",
                  ROOT / "config.example.json", ASSETS / "config.example.json"]
    for path in candidates:
        name = path.name
        if path.exists():
            try:
                cfg = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                print(f"[config] {name} 解析失败: {exc}, 跳过", file=sys.stderr)
                continue
            cfg["_source"] = name
            return cfg
    raise SystemExit("找不到 config.json（可复制 config.example.json 一份再改）")


CFG = load_config()
PY = CFG.get("python") or sys.executable
WORKBENCH = Path(CFG.get("workbench_dir", ""))
VOICE = Path(CFG.get("voice_dir", ""))
WORKBENCH_PORT = int(CFG.get("workbench_port", 61942))
DEBUG_PORT = int(CFG.get("browser_debug_port", 9333))


# --------------------------------------------------------------------------- 工具

def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None


def http_post(url: str, payload: dict | None = None, timeout: float = 8.0):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {"raw": body[:400]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:  # 连不上 / 超时
        return {"error": str(exc)}


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def tail(path: Path, lines: int = 12) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-lines:]]
    except FileNotFoundError:
        return []


def _ps_query(fragment: str, names: list[str], kill: bool) -> list[int]:
    """在指定进程名里按命令行片段查找（必要时结束）。只匹配目标进程名，
    避免把命令行里恰好含这个字符串的 powershell / 终端本身也算进去。"""
    if not fragment:
        return []
    name_filter = " OR ".join(f"Name='{n}'" for n in names)
    action = "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; " if kill else ""
    script = (
        f"Get-CimInstance Win32_Process -Filter \"{name_filter}\" | "
        f"Where-Object {{ $_.CommandLine -like '*{fragment}*' -and $_.ProcessId -ne {os.getpid()} }} | "
        f"ForEach-Object {{ {action}$_.ProcessId }}"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25, creationflags=CREATE_NO_WINDOW,
        )
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def ps_kill_matching(fragment: str, names: list[str] | None = None) -> int:
    return len(_ps_query(fragment, names or ["python.exe", "pythonw.exe"], kill=True))


def ps_find_matching(fragment: str, names: list[str] | None = None) -> list[int]:
    return _ps_query(fragment, names or ["python.exe", "pythonw.exe"], kill=False)


def first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return p
    return None


# --------------------------------------------------------------------------- 进程管理

class Jobs:
    """自己启动的子进程 + 按命令行识别的外部进程。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}
        self.started: dict[str, float] = {}

    def log_path(self, name: str) -> Path:
        return LOG_DIR / f"{name}.log"

    def start(self, name: str, args: list[str], cwd: Path, marker: str) -> dict:
        with self.lock:
            proc = self.procs.get(name)
            if proc and proc.poll() is None:
                return {"ok": True, "note": "已经在运行"}
            cmd = [str(a) for a in args]
            log = self.log_path(name).open("a", encoding="utf-8")
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
            log.flush()
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:
                log.write(f"[start failed] {exc}\n")
                log.close()
                return {"ok": False, "error": str(exc)}
            self.procs[name] = proc
            self.started[name] = time.time()
            return {"ok": True, "pid": proc.pid, "marker": marker}

    def stop(self, name: str, marker: str) -> dict:
        killed = 0
        with self.lock:
            proc = self.procs.pop(name, None)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=6)
                    killed += 1
                except Exception:
                    try:
                        proc.kill()
                        killed += 1
                    except Exception:
                        pass
        if marker:
            killed += ps_kill_matching(marker)
        self.started.pop(name, None)
        return {"ok": True, "killed": killed}

    def running(self, name: str, marker: str) -> bool:
        proc = self.procs.get(name)
        if proc and proc.poll() is None:
            return True
        return bool(ps_find_matching(marker)) if marker else False


JOBS = Jobs()

MARKERS = {
    "workbench": "launch.py",
    "bridge": "bili_live_bridge.py",
    "reply": "auto_reply.py",
    "host": "live_host.py",
    "watch_browser": "dsh-edge-watch",
}

LOGS = ("workbench", "bridge", "reply", "host")


# --------------------------------------------------------------------------- 状态

def workbench_state() -> dict:
    if not port_open(WORKBENCH_PORT):
        return {"alive": False}
    data = http_json(f"http://127.0.0.1:{WORKBENCH_PORT}/api/studio/voice", timeout=2.5)
    if not isinstance(data, dict):
        return {"alive": True, "voice": "unknown"}
    engine = data.get("engineState") or {}
    return {
        "alive": True,
        "ready": bool(data.get("ready")),
        "engine": engine.get("status"),
        "device": engine.get("device"),
        "sampleRate": data.get("sampleRate"),
        "loadSeconds": data.get("loadSeconds"),
    }


def browser_state() -> dict:
    if not port_open(DEBUG_PORT):
        return {"alive": False}
    pages = http_json(f"http://127.0.0.1:{DEBUG_PORT}/json/list", timeout=1.5) or []
    vids = [p for p in pages if p.get("type") == "page"]
    best = vids[0] if vids else None
    return {
        "alive": True,
        "port": DEBUG_PORT,
        "pages": len(vids),
        "title": (best or {}).get("title"),
        "url": (best or {}).get("url"),
    }


def room_state() -> dict:
    room = CFG.get("room_id")
    if not room:
        return {"configured": False}
    data = http_json(
        f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room}", timeout=4.0
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return {"configured": True, "room": room, "error": "接口没返回"}
    info = data.get("data") or {}
    return {
        "configured": True,
        "room": room,
        "title": info.get("title"),
        "live": info.get("live_status") == 1,
        "online": info.get("online"),
        "uid": info.get("uid"),
    }


def collect_status() -> dict:
    host_running = JOBS.running("host", MARKERS["host"])
    return {
        "time": time.strftime("%H:%M:%S"),
        "config": {"source": CFG.get("_source"), "room": CFG.get("room_id")},
        "workbench": workbench_state(),
        "browser": browser_state(),
        "room": room_state(),
        "jobs": {
            "host": host_running,
            "bridge": JOBS.running("bridge", MARKERS["bridge"]),
            "reply": JOBS.running("reply", MARKERS["reply"]),
        },
        "logs": {name: tail(JOBS.log_path(name), 6) for name in LOGS},
    }


# --------------------------------------------------------------------------- 动作

def tail_until_ready(name: str, url: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(int(CFG.get("workbench_port", 61942))):
            time.sleep(1.0)
            continue
        data = http_json(url, timeout=2.5) or {}
        if data.get("ready") or (data.get("engineState") or {}).get("status") == "ready":
            return {"ok": True, "state": data}
        time.sleep(1.5)
    return {"ok": False, "note": "超时（引擎可能还在加载）", "log": tail(JOBS.log_path(name), 12)}


def do_action(action: str, params: dict) -> dict:
    room = str(params.get("room") or CFG.get("room_id") or "").strip()

    if action == "start_workbench":
        if port_open(WORKBENCH_PORT):
            return {"ok": True, "note": "工作台已经在跑"}
        res = JOBS.start("workbench", [PY, "launch.py"], WORKBENCH, MARKERS["workbench"])
        return res

    if action == "stop_workbench":
        return JOBS.stop("workbench", MARKERS["workbench"])

    if action == "warmup_voice":
        return http_post(f"http://127.0.0.1:{WORKBENCH_PORT}/api/studio/voice/warmup", {}, timeout=10)

    if action == "start_bridge":
        if not room:
            return {"ok": False, "error": "没填房间号"}
        return JOBS.start(
            "bridge",
            [PY, "bili_live_bridge.py", "--room", room, "--seconds", "0"],
            VOICE, MARKERS["bridge"],
        )

    if action == "stop_bridge":
        return JOBS.stop("bridge", MARKERS["bridge"])

    if action == "start_reply":
        return JOBS.start("reply", [PY, "auto_reply.py", "--gap", "4", "--max-age", "20"], VOICE, MARKERS["reply"])

    if action == "stop_reply":
        return JOBS.stop("reply", MARKERS["reply"])

    if action == "start_host":
        if not room:
            return {"ok": False, "error": "没填房间号"}
        return JOBS.start("host", [PY, "live_host.py", "--room", room], VOICE, MARKERS["host"])

    if action == "stop_host":
        res = JOBS.stop("host", MARKERS["host"])
        JOBS.stop("bridge", MARKERS["bridge"])
        JOBS.stop("reply", MARKERS["reply"])
        return res

    if action == "open_watch_browser":
        exe = first_existing(CFG.get("browser_candidates") or [])
        if not exe:
            return {"ok": False, "error": "找不到 Edge/Chrome"}
        profile = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "dsh-edge-watch"
        url = params.get("url") or CFG.get("watch_url") or "https://www.bilibili.com"
        subprocess.Popen(
            [exe, f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             "--new-window", url],
            creationflags=CREATE_NO_WINDOW,
        )
        return {"ok": True, "note": f"已开浏览器（调试端口 {DEBUG_PORT}）"}

    if action == "close_watch_browser":
        return {"ok": True, "killed": ps_kill_matching(MARKERS["watch_browser"], ["msedge.exe", "chrome.exe"])}

    if action == "open_livehime":
        exe = first_existing(CFG.get("livehime_candidates") or [])
        if not exe:
            return {"ok": False, "error": "找不到直播姬"}
        subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
        return {"ok": True, "note": "已启动直播姬"}

    if action == "stop_all":
        killed = 0
        for name in ("host", "bridge", "reply"):
            killed += JOBS.stop(name, MARKERS[name]).get("killed", 0)
        return {"ok": True, "killed": killed}

    if action == "open_logs":
        if os.name == "nt":
            os.startfile(LOG_DIR)  # noqa: S606
        return {"ok": True}

    return {"ok": False, "error": f"不认识的动作: {action}"}


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "L2DConsole/1.0"

    def log_message(self, fmt, *args):  # 静音：控制台日志已经够多了
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = (ASSETS / "ui.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(collect_status())
            return
        if path == "/api/config":
            self._json({"room": CFG.get("room_id"), "source": CFG.get("_source"),
                        "workbench": str(WORKBENCH), "voice": str(VOICE)})
            return
        if path.startswith("/api/log/"):
            name = re.sub(r"[^a-z_]", "", path.rsplit("/", 1)[-1])
            self._json({"name": name, "lines": tail(JOBS.log_path(name), 80)})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path != "/api/action":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        action = payload.get("action", "")
        params = payload.get("params") or {}
        try:
            result = do_action(action, params)
        except Exception as exc:  # 别让一个动作把服务打挂
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["action"] = action
        self._json(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="L2D 直播 / 陪看 控制台")
    parser.add_argument("--port", type=int, default=int(CFG.get("console_port", 61950)))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"控制台: {url}   (配置来源 {CFG.get('_source')})")
    if args.open:
        threading.Timer(0.8, lambda: subprocess.Popen(
            ["cmd", "/c", "start", "", url], creationflags=CREATE_NO_WINDOW)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")
    return 0


if __name__ == "__main__":
    sys.exit(main())
