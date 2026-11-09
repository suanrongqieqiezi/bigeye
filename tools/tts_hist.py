# -*- coding: utf-8 -*-
"""TTS 语音库：朗读音频落盘保存 + 收藏 + 快捷键（2026-10-30）

需求（用户原话）：队列的音频保存 200 条，显示成一条条卡片；超过自动清旧；
每条音频旁一个按钮，按了加入常用列表 —— 常用的永久保存不会被清，
可设置快捷键或鼠标点击直接播放。

设计：
- 音频文件：data/tts_hist/<id>.wav（原样保存 /api/tts/pcm 转发出的字节流，
  第一块自带 44 字节 WAV 头；万一不是 RIFF 开头就当裸 PCM 补一个标准头）。
- 元数据：data/tts_hist/meta.json（items 数组，含 text/model/ts/dur/fav/hotkey）。
- 上限 200 条：入库时从最旧开始删非收藏，删到 ≤200 为止（wav 文件一起删）。
- 收藏(fav=1) 永不参与自动清理，只能手动删。
- 并发：ThreadingHTTPServer 多线程 ⇒ 所有 meta.json 读写走同一把锁。
- 🔴 本模块在 tools/ 下 ⇒ server.py 只 import 一次，改逻辑走 hot_reload 即生效，
  不用重启；但 server.py 里新加的路由本身要重启才可见。
"""

import json
import os
import re
import struct
import threading
import time

HIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tts_hist")
META_PATH = os.path.join(HIST_DIR, "meta.json")
MAX_ITEMS = 200          # 队列音频保存 200 条，超出自动清最旧（收藏除外）
MIN_KEEP_BYTES = 14400   # <0.3s（24000Hz×16bit×0.3s）的碎片不存 —— 基本都是误触/秒停
TTS_SR = 24000

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[0-9a-z_]{4,64}$")   # id 白名单：防目录穿越，拼路径前必须过这个


def _ensure_dir():
    try:
        os.makedirs(HIST_DIR, exist_ok=True)
    except Exception:
        pass


def _load():
    """读 meta（调用方必须已持锁）。文件坏/不存在 → 空库。"""
    try:
        with open(META_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            return d["items"]
    except Exception:
        pass
    return []


def _save(items):
    """写 meta（调用方必须已持锁）。原子写：先 .tmp 再替换，防写一半坏库。"""
    _ensure_dir()
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False)
    try:
        os.replace(tmp, META_PATH)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _wav_path(item_id):
    return os.path.join(HIST_DIR, item_id + ".wav")


def _pcm_bytes(raw):
    """从转发流里取出 PCM 部分：标准 RIFF 头(≥44B)时跳过头，否则当裸 PCM。"""
    if len(raw) > 44 and raw[0:4] == b"RIFF":
        return raw[44:]
    return raw


def _dur_of(pcm_len):
    return round(pcm_len / 2.0 / TTS_SR, 2)   # 16bit mono


def _fix_wav_header(raw):
    """原样保存转发流（它自带 44B 头）；头不像 RIFF 时补一个标准 24kHz mono 16bit 头。"""
    if len(raw) > 44 and raw[0:4] == b"RIFF":
        return raw
    pcm = raw
    data_len = len(pcm)
    hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, TTS_SR, TTS_SR * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", data_len)
    return hdr + pcm


def save_stream(raw, text, model):
    """入库一条语音。raw = /api/tts/pcm 转发出的完整字节流（含 WAV 头）。
    返回新条目 dict；太短/异常返回 None（绝不让历史库拖垮朗读主链路）。"""
    try:
        if not raw or len(raw) <= 44:
            return None
        pcm = _pcm_bytes(raw)
        if len(pcm) < MIN_KEEP_BYTES:
            return None
        item_id = time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(3).hex()
        wav = _fix_wav_header(raw)
        _ensure_dir()
        with open(_wav_path(item_id), "wb") as f:
            f.write(wav)
        item = {
            "id": item_id,
            "text": (text or "")[:2000],
            "model": (model or "")[:64],
            "ts": time.time(),
            "dur": _dur_of(len(pcm)),
            "bytes": len(wav),
            "fav": 0,
            "hotkey": "",
        }
        with _LOCK:
            items = _load()
            items.append(item)
            _prune(items)
            _save(items)
        return item
    except Exception as e:
        try:
            print("[tts_hist] 保存失败:", e)
        except Exception:
            pass
        return None


def _prune(items):
    """超出 MAX_ITEMS 时从最旧开始删非收藏（调用方必须已持锁）。"""
    over = len(items) - MAX_ITEMS
    if over <= 0:
        return
    kept = []
    removed = 0
    # items 按 append 序（≈时间正序）；从头部（最旧）开始摘
    for it in items:
        if over > 0 and not it.get("fav"):
            try:
                os.remove(_wav_path(it["id"]))
            except Exception:
                pass
            over -= 1
            removed += 1
        else:
            kept.append(it)
    if removed:
        try:
            print("[tts_hist] 自动清理 %d 条旧语音（收藏保留）" % removed)
        except Exception:
            pass
    items[:] = kept


def list_items():
    with _LOCK:
        items = _load()
        items.sort(key=lambda it: (-1 if it.get("fav") else 0, -it.get("ts", 0)))
        return [dict(it) for it in items]


def audio_path(item_id):
    """按 id 找 wav 路径；id 不合法或不存在返回 None。"""
    if not item_id or not _ID_RE.match(item_id):
        return None
    p = _wav_path(item_id)
    return p if os.path.isfile(p) else None


def set_fav(item_id, fav):
    with _LOCK:
        items = _load()
        for it in items:
            if it["id"] == item_id:
                it["fav"] = 1 if fav else 0
                _save(items)
                return dict(it)
    return None


def set_hotkey(item_id, key):
    key = (key or "")[:32]
    with _LOCK:
        items = _load()
        # 先清掉别的条目上同样的键 —— 一个快捷键只能绑一条
        for it in items:
            if it.get("hotkey") and it["hotkey"] == key and it["id"] != item_id:
                it["hotkey"] = ""
        for it in items:
            if it["id"] == item_id:
                it["hotkey"] = key
                _save(items)
                return dict(it)
    return None


def delete_item(item_id):
    with _LOCK:
        items = _load()
        rest = [it for it in items if it["id"] != item_id]
        if len(rest) == len(items):
            return False
        _save(rest)
    try:
        os.remove(_wav_path(item_id))
    except Exception:
        pass
    return True


def hotkey_map():
    """快捷键 → 条目，供前端拉一次做本地匹配。"""
    with _LOCK:
        items = _load()
        return {it["hotkey"]: it["id"] for it in items if it.get("hotkey")}
