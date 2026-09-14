#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地模型服务：探活 / 按需拉起 / 上下文长度管理。

背景
----
Qwen3.8 27B (Local GPU) 跑在 llama.cpp 的 llama-server 上（本机 127.0.0.1:8901）。
以前切到这个模型不做任何检查：服务没开也照样"切换成功"，真发消息时才在
~20 秒重试后甩一句 WinError 给用户，完全看不出是"服务没启动"。

本模块提供三件事
----------------
1) probe()     探活：llama.cpp 查 GET /health，Ollama 查 GET /api/tags。
               1.5s 短超时、结果短缓存，走无代理 opener（否则系统设了
               HTTP_PROXY 时对 localhost 的请求会被代理接管返回 502）。
2) start()     按需拉起：用与本机 model_config.json 的 local_llm 段一致的参数
               启动 llama-server —— 无控制台窗口、日志落盘、附带
               --alias 让 /v1/models 返回干净的模型 id。
3) 上下文长度   llama.cpp 的上下文是**启动参数 --ctx-size**，运行中无法通过
               API 修改 —— 所以存在配置里、重启生效。effective_ctx() 在服务
               运行时读 /props 拿真实 n_ctx，供 server._model_context_window()
               使用，避免应用按默认 128K 估算、实际只有 24K 导致上下文被静默截断。
4) 视觉(mmproj) Qwen3.8-27B 本身是 VLM，但视觉塔在独立的 mmproj 文件里。
               只加载主模型时 /props 报 modalities.vision=false，带图请求直接
               HTTP 500。本模块负责解析 mmproj 路径（配置优先 / 自动同目录探测）、
               拼进启动参数，并提供 supports_vision() 给上层判断"这个模型能不能看图"。

配置存放：<root>/model_config.json 的 "local_llm" 段（首次调用落默认值）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# ── 默认配置 ────────────────────────────────────────
# exe / model_path 留空：真实路径由用户在本机 model_config.json 的 "local_llm" 段配置。
_HOME = os.path.expanduser("~")
DEFAULT_CONFIG = {
    "provider": "llamacpp",
    # 前端模型列表里的 id（须与 server._build_model_list 的 llamacpp 条目一致）
    "model_id": "qwen3.8-27b-iq4xs",
    "host": "127.0.0.1",
    "port": 8901,
    # 上下文长度（llama.cpp --ctx-size）；改后需重启服务才生效
    "ctx_size": 24576,
    "exe": "",
    "model_path": "",
    # 视觉投影文件（mmproj）：Qwen3.8-27B 本身是 VLM，但视觉塔放在**独立的 mmproj 文件**里。
    # 只加载主模型时 /props 会报 modalities.vision=false，带图请求直接 HTTP 500
    # （"image input is not supported ... you may need to provide the mmproj"）。
    # 留空 = 自动在模型同目录找 mmproj*.gguf；找不到则视为不启用视觉（纯文本）。
    "mmproj": "",
    "threads": 8,
    "gpu_layers": 999,
    # 追加参数（留空用上面的标准参数即可）
    "extra_args": [],
    # 拉起后等待就绪的上限（秒）；27B 首次加载含从磁盘读 15GB+ 权重
    "start_timeout": 240,
    # Ollama（只做探活，不做拉起）
    "ollama_port": 11434,
}

_PROBE_TTL = 5.0        # 探活结果缓存（秒）：够短以支撑进度轮询，够长以合并并发请求
_HTTP_TIMEOUT = 2.0     # 单次探活超时（秒）
_LOG_NAME = "local_llm.log"

_lock = threading.RLock()
_cache: dict = {"at": 0.0, "value": None}
_spawn: dict = {}       # {"pid": int, "proc": Popen, "at": float, "cmd": [..]}
_root: str = os.path.dirname(os.path.abspath(__file__))
_config_path: str = os.path.join(_root, "model_config.json")
_data_dir: str = os.path.join(_root, "data")

# 绕过 HTTP(S)_PROXY：对 127.0.0.1 的探活绝不能走代理
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def configure(config_path: str | None = None, data_dir: str | None = None) -> None:
    """由 server.py 注入真实路径（打包成 exe 后 ROOT_DIR ≠ 源码目录）。"""
    global _config_path, _data_dir
    with _lock:
        if config_path:
            _config_path = config_path
        if data_dir:
            _data_dir = data_dir


def _is_windows() -> bool:
    return os.name == "nt"


def _no_window_kwargs() -> dict:
    """Windows 下不弹控制台（应用本身跑在 pythonw 里，弹窗会很突兀）。"""
    if not _is_windows():
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


# ── 配置读写 ────────────────────────────────────────

def get_config() -> dict:
    """返回配置 = 默认值 + model_config.json 里已存的 local_llm 段。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        saved = raw.get("local_llm") or {}
        if isinstance(saved, dict):
            for k, v in saved.items():
                if k in DEFAULT_CONFIG and v is not None and v != "":
                    cfg[k] = v
    except Exception:
        pass
    # 类型归一：JSON 里可能存成字符串
    for k in ("port", "ctx_size", "threads", "gpu_layers", "start_timeout", "ollama_port"):
        try:
            cfg[k] = int(cfg[k])
        except Exception:
            cfg[k] = int(DEFAULT_CONFIG[k])
    return cfg


def save_config(updates: dict) -> dict:
    """把 updates 合并进 model_config.json 的 local_llm 段（其它段原样保留）。"""
    raw = {}
    try:
        if os.path.exists(_config_path):
            with open(_config_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
    except Exception:
        raw = {}
    cur = dict(DEFAULT_CONFIG)
    cur.update(raw.get("local_llm") or {})
    for k, v in (updates or {}).items():
        if k in DEFAULT_CONFIG:
            cur[k] = v
    raw["local_llm"] = cur
    try:
        with open(_config_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=4)
    except Exception as e:
        return {"ok": False, "error": f"写入配置失败: {e}"}
    _invalidate()
    return {"ok": True, "config": get_config()}


def base_url(cfg: dict | None = None) -> str:
    cfg = cfg or get_config()
    return f"http://{cfg['host']}:{cfg['port']}/v1"


def is_local_model(model_id: str) -> bool:
    """该 model_id 是否指向本地 llama.cpp 服务（配置里的 id / 路径名 / 前缀）。"""
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    cfg = get_config()
    if mid == str(cfg.get("model_id", "")).lower():
        return True
    try:
        base = os.path.basename(str(cfg.get("model_path", ""))).lower()
        if base and mid == base:
            return True
    except Exception:
        pass
    return mid.startswith("qwen3.8")


# ── 视觉（mmproj）解析 ──────────────────────────────

def find_mmproj(model_path: str = "") -> str:
    """在模型同目录里找 mmproj 文件（用户没显式配置时的兜底）。

    Qwen3.8-27B 的视觉塔是独立文件，官方命名 mmproj-F16.gguf / mmproj-BF16.gguf。
    优先 F16 与 BF16（精度越高视觉理解越好），其次任意含 mmproj 的 gguf。
    """
    if not model_path:
        cfg = get_config()
        model_path = str(cfg.get("model_path") or "")
    d = os.path.dirname(model_path)
    if not d or not os.path.isdir(d):
        return ""
    try:
        names = os.listdir(d)
    except Exception:
        return ""
    cands = [n for n in names if "mmproj" in n.lower() and n.lower().endswith(".gguf")]
    if not cands:
        return ""
    def _rank(n):
        low = n.lower()
        if "f16" in low:
            return 0
        if "bf16" in low:
            return 1
        if "q8" in low:
            return 2
        return 3
    cands.sort(key=_rank)
    return os.path.join(d, cands[0])


def resolve_mmproj(cfg: dict | None = None) -> str:
    """返回实际会用的 mmproj 路径：配置里显式指定的优先，否则自动探测。"""
    cfg = cfg or get_config()
    explicit = str(cfg.get("mmproj") or "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    return find_mmproj(str(cfg.get("model_path") or ""))


def supports_vision() -> bool:
    """运行中的本地服务是否真能收图。

    以 llama.cpp 的权威字段 /props → modalities.vision 为准（b10816 已提供）。
    老 builds 没有该字段时退化为"是否配置/探测到 mmproj 文件"。
    """
    info = probe()
    if not info.get("running"):
        return False
    mod = info.get("modalities")
    if isinstance(mod, dict) and "vision" in mod:
        return bool(mod["vision"])
    return bool(info.get("mmproj_path"))


# ── HTTP 探活 ───────────────────────────────────────

def _get_json(url: str, timeout: float = _HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def _spawn_alive() -> bool:
    proc = _spawn.get("proc")
    if proc is None:
        return False
    return proc.poll() is None


def probe(force: bool = False) -> dict:
    """探活本地服务。返回 dict，字段见下方 info。结果缓存 _PROBE_TTL 秒。

    running  : /health 返回 ok（可立即用）
    starting : 返回 loading / 503，或我们刚拉起、进程还在但尚未就绪
    """
    now = time.time()
    with _lock:
        if not force and _cache["value"] is not None and (now - _cache["at"]) < _PROBE_TTL:
            return dict(_cache["value"])

    cfg = get_config()
    _mp = resolve_mmproj(cfg)
    info = {
        "provider": cfg["provider"],
        "base_url": base_url(cfg),
        "host": cfg["host"],
        "port": cfg["port"],
        "model_id": cfg["model_id"],
        "ctx_size": cfg["ctx_size"],       # 配置值（下次启动会用的）
        "live_ctx": None,                  # 运行中真实值（/props）
        "model_path": "",
        "alias": "",
        "running": False,
        "starting": False,
        "pid": None,
        "error": "",
        # 视觉：mmproj_path = 实际会加载的投影文件；vision = 服务自报能否收图
        "mmproj_path": _mp,
        "mmproj_configured": str(cfg.get("mmproj") or "").strip(),
        "modalities": None,
        "vision": False,
    }

    if cfg["provider"] == "llamacpp":
        root = f"http://{cfg['host']}:{cfg['port']}"
        try:
            h = _get_json(f"{root}/health")
            status = str((h or {}).get("status", "")).lower()
            if status == "ok":
                info["running"] = True
            elif status:
                info["starting"] = True
                info["error"] = status
        except urllib.error.HTTPError as e:
            # llama-server 加载中会返回 503 + {"status":"loading model"}
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 503:
                info["starting"] = True
                info["error"] = body[:200] or "loading model"
            else:
                info["error"] = f"HTTP {e.code}"
        except Exception as e:
            info["error"] = str(e)

        if info["running"]:
            try:
                p = _get_json(f"{root}/props", timeout=3.0) or {}
                info["live_ctx"] = int(((p.get("default_generation_settings") or {})
                                        .get("n_ctx")) or 0) or None
                info["model_path"] = p.get("model_path") or ""
                info["alias"] = p.get("model_alias") or ""
                # 权威视觉判据（llama.cpp b10816 起在 /props 暴露 modalities）
                mod = p.get("modalities")
                if isinstance(mod, dict):
                    info["modalities"] = mod
                    info["vision"] = bool(mod.get("vision"))
            except Exception:
                pass

        # 进程还活着但没就绪 → 仍在启动（供前端显示进度）
        if not info["running"] and _spawn_alive():
            info["starting"] = True
            info["pid"] = _spawn.get("pid")

    elif cfg["provider"] == "ollama":
        try:
            _get_json(f"http://{cfg['host']}:{cfg['ollama_port']}/api/tags")
            info["running"] = True
        except Exception as e:
            info["error"] = str(e)

    with _lock:
        _cache["at"] = time.time()
        _cache["value"] = dict(info)
    return dict(info)


def effective_ctx() -> int:
    """真实上下文窗口：服务在跑就用 /props 的 n_ctx，否则用配置值。

    server._model_context_window() 用它替代"猜"出来的默认 128K。
    """
    info = probe()
    if info.get("live_ctx"):
        return int(info["live_ctx"])
    cfg = get_config()
    return int(cfg["ctx_size"])


# ── 进程操作 ────────────────────────────────────────

def _port_pids(port: int) -> list:
    """查监听指定端口的 PID（用于"停止/重启服务"）。"""
    if not _is_windows():
        return []
    try:
        # 必须显式 errors="replace"：Windows 本地化 netstat 输出含非 UTF-8 字节，
        # text=True 默认按 UTF-8 严格解码会抛 UnicodeDecodeError（异常发生在
        # subprocess 的 reader 线程里，主线程只拿到空字符串 → 表现为"永远找不到
        # 占用端口的进程"，停止/重启按钮静默失效）。
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace",
                             **_no_window_kwargs()).stdout
    except Exception:
        return []
    pids = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
            if parts[4].isdigit() and parts[4] not in pids:
                pids.append(parts[4])
    return pids


def _proc_name(pid: str) -> str:
    if not _is_windows():
        return ""
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace",
                             **_no_window_kwargs()).stdout
        first = (out or "").strip().splitlines()
        if first:
            return first[0].split(",")[0].strip('"')
    except Exception:
        pass
    return ""


def _log_path() -> str:
    try:
        os.makedirs(_data_dir, exist_ok=True)
    except Exception:
        pass
    return os.path.join(_data_dir, _LOG_NAME)


def log_tail(max_chars: int = 1200) -> str:
    """启动日志末尾片段 —— 拉起失败时给前端看原因。"""
    try:
        path = _log_path()
        if not os.path.exists(path):
            return ""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_chars * 4))
            data = f.read()
        return data.decode("utf-8", "replace")[-max_chars:]
    except Exception:
        return ""


def _build_cmd(cfg: dict) -> list:
    exe = str(cfg["exe"])
    args = [
        exe,
        "-m", str(cfg["model_path"]),
        "--host", str(cfg["host"]),
        "--port", str(cfg["port"]),
        # 上下文长度：llama.cpp 只在启动时生效
        "--ctx-size", str(cfg["ctx_size"]),
        "--threads", str(cfg["threads"]),
        "--threads-batch", str(cfg["threads"]),
        "--n-gpu-layers", str(cfg["gpu_layers"]),
        # 让 /v1/models 返回干净的 id（默认返回 gguf 全路径，和前端列表对不上）
        "--alias", str(cfg["model_id"]),
        "--jinja",
    ]
    # 视觉：加载 mmproj 视觉投影器。Qwen3.8-27B 的视觉塔是独立文件，
    # 不加这一项 /props 的 modalities.vision=false，图片请求会被服务拒绝（HTTP 500）。
    mp = resolve_mmproj(cfg)
    if mp:
        args += ["--mmproj", mp]
    extra = cfg.get("extra_args") or []
    if isinstance(extra, list):
        args += [str(a) for a in extra]
    # 允许把 exe 配成 .cmd/.bat（复用用户自己的 serve 脚本）
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c"] + args
    return args


def start() -> dict:
    """按需拉起本地服务。立即返回，不等模型加载完（前端轮询 status）。"""
    cfg = get_config()
    st = probe(force=True)
    if st.get("running"):
        return {"ok": True, "already": True, "pid": st.get("pid"), "status": st}
    if st.get("starting") and _spawn_alive():
        return {"ok": True, "starting": True, "pid": _spawn.get("pid"), "status": st}

    exe, model_path = str(cfg["exe"]), str(cfg["model_path"])
    if not os.path.exists(exe):
        return {"ok": False, "error": f"找不到 llama-server 可执行文件：{exe}",
                "hint": "请在「本地模型服务」里改正路径"}
    if not os.path.exists(model_path):
        return {"ok": False, "error": f"找不到模型文件：{model_path}",
                "hint": "请在「本地模型服务」里改正路径"}

    # 显式配了 mmproj 但文件不在 → 提示（仍按纯文本启动，不阻断）
    _vision_warning = ""
    _explicit_mp = str(cfg.get("mmproj") or "").strip()
    if _explicit_mp and not os.path.exists(_explicit_mp):
        _vision_warning = f"配置的视觉投影文件不存在，将以纯文本模式启动：{_explicit_mp}"

    cmd = _build_cmd(cfg)
    log_path = _log_path()
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start =====\n")
            lf.write(" ".join(cmd) + "\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(exe) or None,
                stdout=lf, stderr=lf,
                stdin=subprocess.DEVNULL,
                **_no_window_kwargs(),
            )
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {e}"}

    with _lock:
        _spawn.clear()
        _spawn.update({"pid": proc.pid, "proc": proc, "at": time.time(), "cmd": cmd})
    _invalidate()

    # 进程秒退（参数写错等）时立刻反馈，不用等前端超时
    time.sleep(0.6)
    if proc.poll() is not None:
        return {"ok": False, "pid": proc.pid,
                "error": f"进程启动后立即退出（退出码 {proc.returncode}）",
                "log_tail": log_tail()}
    return {"ok": True, "pid": proc.pid, "started": True, "log_path": log_path,
            "cmd": " ".join(cmd)}


def stop() -> dict:
    """停止本端口的 llama-server（只杀进程名含 llama 的，避免误伤）。"""
    cfg = get_config()
    killed, skipped = [], []
    for pid in _port_pids(cfg["port"]):
        name = _proc_name(pid)
        if "llama" not in (name or "").lower():
            skipped.append({"pid": pid, "name": name})
            continue
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=15,
                           encoding="utf-8", errors="replace",
                           **_no_window_kwargs())
            killed.append({"pid": pid, "name": name})
        except Exception as e:
            skipped.append({"pid": pid, "name": name, "error": str(e)})
    with _lock:
        _spawn.clear()
    _invalidate()
    return {"ok": True, "killed": killed, "skipped": skipped}


def status(force: bool = False) -> dict:
    """给前端的完整状态：探活结果 + 进程信息 + 日志尾部。"""
    cfg = get_config()
    starting_now = _spawn_alive()
    info = probe(force=force or starting_now)
    info["configured_ctx"] = int(cfg["ctx_size"])
    info["start_timeout"] = int(cfg["start_timeout"])
    info["spawn_pid"] = _spawn.get("pid")
    if _spawn.get("at"):
        info["spawn_elapsed"] = round(time.time() - _spawn["at"], 1)
    info["exe"] = cfg["exe"]
    info["log_path"] = _log_path()
    # 视觉状态一句话说清：能不能收图 / 为什么不能
    if info.get("vision"):
        info["vision_hint"] = "已启用视觉（mmproj 已加载）"
    elif info.get("mmproj_path"):
        info["vision_hint"] = "有 mmproj 文件但当前进程未加载 —— 重启服务后生效"
    else:
        info["vision_hint"] = ("未启用视觉：缺少 mmproj 视觉投影文件。"
                               "把 mmproj-F16.gguf 放到模型同目录，或在此指定路径后重启服务")
    if not info["running"]:
        info["log_tail"] = log_tail()
    info["ok"] = True
    return info


def wait_ready(timeout: float | None = None, interval: float = 2.0) -> dict:
    """阻塞等待就绪（供脚本/自检用；HTTP 接口不调用它）。

    返回 status() 的完整结构（含 vision / vision_hint），而不仅是探活结果。
    """
    cfg = get_config()
    limit = float(timeout if timeout is not None else cfg["start_timeout"])
    t0 = time.time()
    while time.time() - t0 < limit:
        st = probe(force=True)
        if st.get("running"):
            out = status(force=True)
            out["waited"] = round(time.time() - t0, 1)
            return out
        if not _spawn_alive() and not st.get("starting"):
            # 没在启动、也没就绪 → 不必再等
            break
        time.sleep(interval)
    out = status(force=True)
    out["timed_out"] = not out.get("running")
    out["waited"] = round(time.time() - t0, 1)
    return out


# ── 自检 ────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif cmd == "start":
        print(json.dumps(start(), ensure_ascii=False, indent=2))
        print(json.dumps(wait_ready(), ensure_ascii=False, indent=2))
    elif cmd == "stop":
        print(json.dumps(stop(), ensure_ascii=False, indent=2))
    elif cmd == "config":
        print(json.dumps(get_config(), ensure_ascii=False, indent=2))
    elif cmd == "vision":
        st = status(force=True)
        print(json.dumps({
            "running": st.get("running"),
            "vision": st.get("vision"),
            "modalities": st.get("modalities"),
            "mmproj_path": st.get("mmproj_path"),
            "vision_hint": st.get("vision_hint"),
            "cmd": _build_cmd(get_config()),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"用法: {sys.argv[0]} [status|start|stop|config|vision]")
