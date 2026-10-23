# -*- coding: utf-8 -*-
"""
大眼 · 语音朗读（TTS）管理器

职责：
  1. 管理 StreamVox TTS 子进程（起/停/探活），给前端一个稳定的 /api/tts/* 入口
  2. 把前端请求代理到子进程，支持整段 wav 与逐句 NDJSON 两种模式
  3. 子进程崩溃/未启动时给出可读错误，而不是 500 堆栈

为什么单独起子进程，不在大眼进程内 import：
  - streamvox 要 onnxruntime-gpu（含 1.9GB CUDA 库）+ Python 3.12 专用 numpy；
    大眼主进程是 Python 3.13，直接 import 会因 cp312 二进制扩展直接崩。
  - 模型常驻显存 ~1.8GB，用户不朗读就不该占着 → 按需拉起、空闲可停。

子进程路径：<root>/tts/streamvox，解释器 <root>/tts/streamvox/.venv/Scripts/python.exe
"""
import os
import json
import time
import socket
import threading
import subprocess
import urllib.request
import urllib.error
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ 的上一层 = 项目根
TTS_DIR = os.path.join(ROOT, "tts", "streamvox")
TTS_PY = os.path.join(TTS_DIR, ".venv", "Scripts", "python.exe")
TTS_ENTRY = os.path.join(TTS_DIR, "run_server.py")
TTS_PORT = int(os.environ.get("DAYAN_TTS_PORT", "8765"))
TTS_HOST = "127.0.0.1"
TTS_BASE = "http://%s:%d" % (TTS_HOST, TTS_PORT)
LOG_PATH = os.path.join(ROOT, "data", "tts_serve.log")

_proc = None
_proc_lock = threading.Lock()
_last_start_err = ""


# ── 工具 ──────────────────────────────────────────────
def _port_open(host=TTS_HOST, port=TTS_PORT, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_get(path, timeout=8):
    req = urllib.request.Request(TTS_BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def available():
    """运行时是否具备（目录+解释器+入口都在）"""
    return (os.path.isfile(TTS_PY) and os.path.isfile(TTS_ENTRY)
            and os.path.isdir(TTS_DIR))


def is_alive():
    """子进程端口是否在监听（比看 _proc 可靠：进程可能被外部拉起）"""
    if not available():
        return False
    return _port_open()


def status():
    st = {
        "available": available(),
        "alive": False,
        "port": TTS_PORT,
        "dir": TTS_DIR,
        "models": None,
        "providers": None,
        "loaded": None,
        "error": None,
        "last_start_err": _last_start_err or None,
    }
    if not available():
        st["error"] = "StreamVox 运行时不存在（缺 %s）" % TTS_DIR
        return st
    if not _port_open():
        return st
    st["alive"] = True
    try:
        h = _http_get("/health", timeout=4)
        st["models"] = h.get("models_dir")
        st["providers"] = h.get("providers")
        st["loaded"] = h.get("loaded")
    except Exception as e:
        st["error"] = "健康检查失败: %s" % e
    return st


def _tail_log(n=40):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


# ── 进程控制 ──────────────────────────────────────────
def start(wait=45.0, preload="moss"):
    """拉起 TTS 子进程。
    ⚠️ 这台机器上用 nohup / start /min 起的后台进程留不住日志也容易秒退，
    必须 Popen + 重定向到文件 + DETACHED，且马上轮询端口确认真的起来了。
    """
    global _proc, _last_start_err
    if not available():
        return {"ok": False, "error": "StreamVox 运行时不存在: %s" % TTS_DIR}
    if _port_open():
        return {"ok": True, "already": True, "port": TTS_PORT}

    with _proc_lock:
        if _port_open():
            return {"ok": True, "already": True, "port": TTS_PORT}
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            env = dict(os.environ)
            env["STREAMVOX_MODELS"] = os.path.join(ROOT, "models", "streamvox", "models")
            env["STREAMVOX_HOME"] = os.path.join(ROOT, "models", "streamvox", "home")
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            flags = 0
            if os.name == "nt":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            lf = open(LOG_PATH, "ab", buffering=0)
            lf.write(("\n===== start %s port=%d =====\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), TTS_PORT)).encode("utf-8"))
            _proc = subprocess.Popen(
                [TTS_PY, TTS_ENTRY, str(TTS_PORT)],
                cwd=TTS_DIR, env=env,
                stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            print("[tts] 子进程已拉起 pid=%s port=%d" % (_proc.pid, TTS_PORT))
        except Exception as e:
            _last_start_err = "%s: %s" % (type(e).__name__, e)
            print("[tts] 启动失败:", _last_start_err)
            return {"ok": False, "error": _last_start_err}

        # 轮询端口 + 健康检查（模型加载要几秒，预热更久）
        t0 = time.time()
        while time.time() - t0 < wait:
            if _port_open():
                return {"ok": True, "pid": _proc.pid, "port": TTS_PORT,
                        "start_s": round(time.time() - t0, 2)}
            if _proc.poll() is not None:
                _last_start_err = "子进程退出 code=%s\n%s" % (_proc.returncode, _tail_log(20))
                print("[tts]", _last_start_err)
                return {"ok": False, "error": _last_start_err}
            time.sleep(0.4)
        _last_start_err = "启动超时 %.0fs\n%s" % (wait, _tail_log(20))
        return {"ok": False, "error": _last_start_err}


def stop():
    global _proc
    with _proc_lock:
        if _proc and _proc.poll() is None:
            try:
                _proc.terminate()
                _proc.wait(timeout=6)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
            print("[tts] 子进程已停止")
            _proc = None
            return {"ok": True, "stopped": True}
        _proc = None
        return {"ok": True, "stopped": False, "note": "没有本进程拉起的实例"}


# ── 代理 ─────────────────────────────────────────────
def synth_wav(text, model="moss", gap_ms=200, timeout=180):
    """整段合成，返回 (wav_bytes, headers_dict)。
    gap_ms 不进 query：POST body 更稳（文字里有 ? & # 时 query 会错）。

    model 以 "lite" 开头时走系统语音（tools/tts_lite），不进 StreamVox 子进程：
      · "lite" / "lite:yaoyao" / "lite_huihui" 等
      · 好处：零模型零显存、100~400ms 出整段，且 tts_manager 是 tools/ 下的模块，
        hot_reload 就能生效，不需要重启大眼服务。
    2026-10-23：用户反馈流式朗读“声音是坏的”，日常朗读改走这条轻量路径。
    """
    m = (model or "").strip()
    if m.lower().startswith("lite"):
        import tools.tts_lite as _lite
        voice = "yaoyao"
        if ":" in m:
            voice = m.split(":", 1)[1] or voice
        elif "_" in m:
            voice = m.split("_", 1)[1] or voice
        return _lite.synth_wav(text, voice=voice)

    if not _port_open():
        r = start()
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "TTS 服务未就绪")
    data = json.dumps({"text": text, "model": model, "gap_ms": gap_ms},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(TTS_BASE + "/tts", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        hd = {k: v for k, v in r.headers.items()}
    return body, hd


def proxy_stream(query_string, timeout=600, path="/tts/lines"):
    """把子进程的流式响应原样转发给前端。
    返回 generator（yield bytes），由调用方写 wfile。
    path: /tts/lines（逐句 NDJSON） 或 /tts/pcm（真流式 PCM）
    """
    if not _port_open():
        r = start()
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or "TTS 服务未就绪")

    def gen():
        url = TTS_BASE + path + "?" + query_string
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while True:
                chunk = r.read(2048)
                if not chunk:
                    break
                yield chunk
    return gen()


def proxy_pcm(query_string, timeout=600):
    """真流式 PCM（首字节 109ms）。小块读取，立刻转发，不攒。

    model=lite 开头时本地生成：系统语音是一次性算完整段的，没有“流式”可言，
    但仍按 200ms 一块吐，目的是让前端只维护一条 PCM 播放链路（不同引擎两条路径
    必然会有行为差异，那是 bug 温床）。
    """
    qs = urllib.parse.parse_qs(query_string or "")
    model = (qs.get("model") or [""])[0]
    if model.lower().startswith("lite"):
        import tools.tts_lite as _lite
        text = (qs.get("text") or [""])[0]
        voice = "yaoyao"
        if ":" in model:
            voice = model.split(":", 1)[1] or voice
        elif "_" in model:
            voice = model.split("_", 1)[1] or voice
        if not text.strip():
            raise RuntimeError("missing text")
        if len(text) > 2000:
            raise RuntimeError("文本过长（轻量朗读上限 2000 字）")

        # 关键：**先算完再返回生成器**。
        # 若把 synth_wav 放进生成器里，异常发生在响应头已发出之后 → 前端收到
        # 一半被截断的流，而不是一个能处理的 503。轻量朗读本身就不是真流式
        # （系统语音一次算完整段，150~350ms），提前算不影响体验。
        wav, _hd = _lite.synth_wav(text, voice=voice)

        def gen_lite():
            # 从 0 开始吐：第一块带 44 字节 wav 头，前端统一跳过。
            # （不能从 44 开始 —— 前端会无条件跳过前 44 字节，那样会吃掉 44 字节真音频）
            step = 4800 * 2  # 200ms @ 24kHz, 16bit
            for i in range(0, len(wav), step):
                yield wav[i:i + step]

        return gen_lite()

    return proxy_stream(query_string, timeout=timeout, path="/tts/pcm")
