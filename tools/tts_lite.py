# -*- coding: utf-8 -*-
"""
大眼 · 轻量朗读（Lite TTS）

为什么再做一个轻量朗读（2026-10-23）：
  用户原话「声音是坏的，有没有办法更轻量的朗读，这次这个语音留着以后实时语音交流用」。
  实测定位到 StreamVox 声音坏的真因不是模型，而是 StreamSquasher.feed() 对
  **每个返回块**各自做 _normalize_peak(target=0.95)：31 个块里 9 个被拉到贴顶，
  块间 RMS 起伏比 65.3 倍 —— 听感就是"抽吸/忽大忽小 + 静音段被放大成噪声"。
  StreamVox 保留给后续实时语音对话（低延迟、音色克隆是它的强项）。

本模块只用系统自带语音，零模型、零显存、零下载：
  · OneCore（Windows.Media.SpeechSynthesis，WinRT）—— 音质明显好于 SAPI5，
    常驻 PowerShell 5.1 worker，单次合成实测 82~91ms，318 字 374ms。
  · SAPI5（pywin32 进程内）—— OneCore 不可用时的兜底，实测 RTF≈0.005。

设计要点：
  1. OneCore 必须用 powershell.exe 5.1，PowerShell 7 不支持 WinRT 投影。
  2. worker 常驻：冷启动要 Add-Type + 反射 AsTask，每次新建进程 200~400ms，
     常驻后单次 80~140ms。进程崩了就静默重建一次。
  3. 大眼由 pythonw 启动，stderr 会被丢弃 —— 所有异常必须落文件 data/tts_lite.log。
  4. 输出统一成 24kHz PCM16 mono wav，前端不用关心引擎差异。
"""
import os
import io
import sys
import time
import base64
import wave
import struct
import logging
import tempfile
import threading
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tools", "tts_onecore_worker.ps1")
LOG_PATH = os.path.join(ROOT, "data", "tts_lite.log")
OUT_SR = 24000  # 与 StreamVox 对齐，前端 TTS_SR 不用改

_log = logging.getLogger("tts_lite")
if not _log.handlers:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        _h = logging.FileHandler(LOG_PATH, encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(_h)
        _log.setLevel(logging.INFO)
    except Exception:
        pass


class _OneCoreWorker(object):
    """常驻 powershell.exe 5.1 子进程，走 stdin/stdout 一行一答。"""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()


    def _spawn(self):
        self.close()
        if not os.path.isfile(WORKER):
            raise RuntimeError("缺少 worker 脚本: %s" % WORKER)
        flags = 0
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags, bufsize=0,
        )
        banner = self.proc.stdout.readline().decode("utf-8", "replace").strip()
        if banner != "READY":
            err = ""
            try:
                err = self.proc.stderr.read(1200).decode("utf-8", "replace")
            except Exception:
                pass
            self.close()
            raise RuntimeError("OneCore worker 未就绪 (banner=%r) %s" % (banner, err))
        _log.info("OneCore worker 就绪 pid=%s", self.proc.pid)

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self._spawn()

    def list_voices(self):
        """worker 内置的音色列表（LIST 命令，不用探针合成）"""
        with self.lock:
            self._ensure()
            self.proc.stdin.write(b"LIST\n")
            self.proc.stdin.flush()
            resp = self.proc.stdout.readline().decode("utf-8", "replace").strip()
            if not resp.startswith("VOICES|"):
                raise RuntimeError("LIST 异常返回: %s" % resp[:200])
            return [x for x in resp[7:].split(";") if x]

    def say(self, text, voice="", timeout=25):
        """返回 (wav_bytes, 引擎耗时ms, 音色名)"""
        with self.lock:
            self._ensure()
            fd, tmp = tempfile.mkstemp(prefix="_ttsoc_", suffix=".wav")
            os.close(fd)
            try:
                b = lambda s: base64.b64encode(s.encode("utf-8")).decode("ascii")
                line = "%s|%s|%s\n" % (b(text), b(tmp), b(voice or ""))
                self.proc.stdin.write(line.encode("utf-8"))
                self.proc.stdin.flush()
                resp = self.proc.stdout.readline().decode("utf-8", "replace").strip()
                if not resp:
                    raise RuntimeError("worker 无响应（进程可能已退出）")
                parts = resp.split("|")
                if parts[0] != "OK":
                    raise RuntimeError("worker 报错: %s" % (resp[:300]))
                ms = int(parts[1])
                name = parts[3] if len(parts) > 3 else voice
                with open(tmp, "rb") as f:
                    data = f.read()
                if len(data) < 100:
                    raise RuntimeError("worker 产出过小: %d 字节" % len(data))
                return data, ms, name
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    def close(self):
        p, self.proc = self.proc, None
        if not p:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


_oc = _OneCoreWorker()


# ── wav 工具 ────────────────────────────────────────────
def _read_wav(data):
    """返回 (samples:int16 list/array, sr, ch)"""
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        a = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.int16)
    else:
        raise RuntimeError("不支持的位宽 %d" % (sw * 8))
    if ch > 1:
        a = a.reshape(-1, ch)[:, 0]
    return a, sr, ch


def _to_wav(samples, sr, ch=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.astype("<i2").tobytes())
    return buf.getvalue()


def _to_24k(samples, sr):
    """OneCore 出来是 16k，线性重采样到 24k（前端 AudioContext 只认一个采样率）。"""
    import numpy as np
    if sr == OUT_SR:
        return samples
    n_out = int(round(len(samples) * OUT_SR / float(sr)))
    if n_out <= 0:
        return samples
    x_old = np.arange(len(samples), dtype=np.float64)
    x_new = np.linspace(0, len(samples) - 1, n_out)
    y = np.interp(x_new, x_old, samples.astype(np.float64))
    return np.clip(y, -32768, 32767).astype(np.int16)


def _normalize(a, target=0.92):
    """整段峰值归一。这里可以放心做：系统语音一次性算完整段，
    不存在流式逐块压峰导致的音量跳变（StreamVox 就是因为逐个 chunk 归一才坏掉的）。
    实测 OneCore/Yaoyao 原始峰值只有 0.69，听起来比其它音源小一半。"""
    import numpy as np
    if len(a) == 0:
        return a
    pk = float(np.abs(a.astype(np.float32)).max()) / 32768.0
    if pk < 0.02 or pk >= target:
        return a
    g = target / pk
    return np.clip(a.astype(np.float32) * g, -32768, 32767).astype(np.int16)


def _trim_silence(a, sr, thresh=0.006, keep_ms=40):
    """裁头尾静音——OneCore 前后各带 200~600ms 空白，听着像"点了没反应"。"""
    import numpy as np
    if len(a) == 0:
        return a
    win = max(1, int(sr * 0.02))
    n = len(a) // win
    if n == 0:
        return a
    f = a[:n * win].astype(np.float32) / 32768.0
    rms = np.sqrt((f.reshape(n, win) ** 2).mean(axis=1))
    voiced = rms > thresh
    if not voiced.any():
        return a
    keep = max(1, int(keep_ms / 20))
    i0 = max(0, int(np.argmax(voiced)) - keep)
    i1 = min(n, n - int(np.argmax(voiced[::-1])) + keep)
    return a[i0 * win: i1 * win]


def _sapi_say(text, voice_kw="Chinese", rate=0):
    """SAPI5 兜底：进程内合成，返回 wav bytes。RTF≈0.005，音质一般但绝不会失败。"""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    fd, tmp = tempfile.mkstemp(prefix="_ttssapi_", suffix=".wav")
    os.close(fd)
    try:
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        vs = sp.GetVoices()
        idx = -1
        for i in range(vs.Count):
            if voice_kw in vs.Item(i).GetDescription():
                idx = i
                break
        if idx >= 0:
            sp.Voice = vs.Item(idx)
        sp.Rate = rate
        st = win32com.client.Dispatch("SAPI.SPFileStream")
        st.Open(tmp, 3, False)
        sp.AudioOutputStream = st
        sp.Speak(text)
        st.Close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# ── 对外接口 ────────────────────────────────────────────
VOICE_MAP = {
    # 前端传短名，映射到 DisplayName 关键字
    "yaoyao": "Yaoyao", "kangkang": "Kangkang", "huihui": "Huihui",
    "xiaoxiao": "Xiaoxiao", "yunjian": "Yunjian", "yunxi": "Yunxi",
}


def list_voices():
    """列出真正可用的音色（OneCore 优先，含 SAPI 兜底名）。"""
    voices = []
    # 注意：不能套 _oc.lock —— say()/voices() 内部自己会加锁，外层再加就是死锁。
    try:
        voices = _oc.list_voices()
        _log.info("OneCore 音色 %d 个: %s", len(voices), voices)
    except Exception as e:
        _log.warning("OneCore 取音色失败: %s", e)
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        vs = sp.GetVoices()
        for i in range(vs.Count):
            d = vs.Item(i).GetDescription()
            if d not in voices:
                voices.append(d)
    except Exception:
        pass
    return voices


def synth_wav(text, voice="yaoyao", engine="onecore"):
    """合成一段 wav（24kHz PCM16 mono）。返回 (wav_bytes, headers dict)。

    headers: X-TTS-Engine / X-TTS-Voice / X-TTS-Ms / X-Audio-S / X-TTS-Rtf
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("空文本")
    t0 = time.time()
    kw = VOICE_MAP.get((voice or "").lower(), voice or "")

    data = None
    used = engine
    ms = 0
    vname = kw
    if engine != "sapi":
        try:
            data, ms, vname = _oc.say(text, kw)
            used = "onecore"
        except Exception as e:
            _log.warning("OneCore 合成失败，回落 SAPI5: %s", e)
            data = None
    if data is None:
        # 新核心不可用/报错 → 确保下次请求会重建 worker
        try:
            _oc.close()
        except Exception:
            pass
        data = _sapi_say(text, kw or "Chinese")
        used = "sapi"
        vname = kw or "Chinese"
        ms = int((time.time() - t0) * 1000)

    a, sr, ch = _read_wav(data)
    a = _trim_silence(a, sr)
    a = _to_24k(a, sr)
    a = _normalize(a)
    wav = _to_wav(a, OUT_SR, 1)
    dur = len(a) / float(OUT_SR)
    total_ms = int((time.time() - t0) * 1000)
    hd = {
        "X-TTS-Engine": used,
        "X-TTS-Voice": str(vname),
        "X-TTS-Ms": str(ms),
        "X-Audio-S": "%.2f" % dur,
        "X-TTS-Rtf": "%.3f" % (total_ms / 1000.0 / max(dur, 0.001)),
        "X-TTS-Total-Ms": str(total_ms),
    }
    _log.info("synth engine=%s voice=%s ch=%d chars=%d ms=%d aud=%.2fs total=%d",
              used, vname, len(text), len(text), ms, dur, total_ms)
    return wav, hd


def stream_wav(text, voice="yaoyao", engine="onecore", chunk_ms=200):
    """按块吐 wav 原始 PCM，给前端流式播放（跳过 44 字节头由前端处理）。

    实现上 OneCore 一次性算完（122ms 就完事），这里只做"按块发"，
    目的是让前端复用同一条 PCM 播放链路，不用区分两条代码路径。
    """
    wav, hd = synth_wav(text, voice=voice, engine=engine)
    body = wav[44:]
    step = max(2, int(OUT_SR * chunk_ms / 1000) * 2)
    for i in range(0, len(body), step):
        yield body[i:i + step]
