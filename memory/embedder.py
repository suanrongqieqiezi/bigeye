#!/usr/bin/env python3
"""
Embedding API wrapper.

Priority chain:
  0. Hashing TF-IDF (pure Python, zero deps, always available)
  1. sentence-transformers (local, needs torch)
  2. Ollama (local service)
  3. OpenAI-compatible API (DeepSeek, etc.)

Backend 0 guarantees memory recall works without any external dependencies.
"""
import json
import math
import os
import re
import threading
import time
import urllib.request
import urllib.error

EMBEDDING_DIM = 512  # default; ONNX BGE: 512, st: 384, hashing: 256

# Cache to avoid re-embedding identical text
_cache = {}
_cache_hits = 0
_cache_misses = 0

# One-time zero-vector warning
_warned_zero_vector = False


# ── Backend 0: ONNX Runtime + BGE-small-zh (best quality, no torch) ──

_onnx_session = None
_onnx_tokenizer = None
_onnx_lock = threading.Lock()
_onnx_infer_lock = threading.Lock()  # 保护 tokenizer.encode + session.run（都不是线程安全的）
ONNX_DIM = 512

def _get_onnx_model_dir():
    """Find ONNX model directory (works in dev and PyInstaller bundle)."""
    import sys
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Try project root first (dev), then _MEIPASS (PyInstaller)
    for root in [base, getattr(sys, '_MEIPASS', ''), os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else '']:
        if not root:
            continue
        path = os.path.join(root, 'models', 'bge_small_zh')
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, 'model.onnx')):
            return path
    return None

def _load_onnx_model():
    """Lazy-load ONNX model + tokenizer."""
    global _onnx_session, _onnx_tokenizer
    if _onnx_session is not None:
        return _onnx_session
    with _onnx_lock:
        if _onnx_session is not None:
            return _onnx_session
        model_dir = _get_onnx_model_dir()
        if not model_dir:
            print("[embedder] ONNX model not found — run _export_onnx.py first")
            return None
        try:
            # vendor 目录注入：onnxruntime 装在项目 vendor/（系统 site-packages 权限受限装不了）
            import sys as _sys
            _vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vendor')
            if os.path.isdir(_vendor) and _vendor not in _sys.path:
                _sys.path.insert(0, _vendor)
            import onnxruntime as ort
            from tokenizers import Tokenizer
            _onnx_tokenizer = Tokenizer.from_file(os.path.join(model_dir, 'tokenizer.json'))
            # BGE-small-zh position embedding 上限 512，超长文本必须截断，否则 ONNX 推理报错
            # 静默 fallback 到 TFIDF(256) 会导致维度割裂（历史根因之一）
            _onnx_tokenizer.enable_truncation(max_length=ONNX_DIM)
            _onnx_session = ort.InferenceSession(
                os.path.join(model_dir, 'model.onnx'),
                providers=['CPUExecutionProvider']
            )
            print(f"[embedder] ONNX BGE-small-zh loaded (dim={ONNX_DIM})")
            return _onnx_session
        except Exception as e:
            print(f"[embedder] ONNX load failed: {e}")
            return None

def _onnx_embed(text: str) -> list[float] | None:
    """Embed via ONNX BGE-small-zh.

    线程安全：HuggingFace tokenizers 的 encode() 不是线程安全的，
    用 _onnx_infer_lock 串行化推理部分。cache 命中时不加锁。
    """
    session = _load_onnx_model()
    if session is None or _onnx_tokenizer is None:
        return None
    try:
        import numpy as np
        with _onnx_infer_lock:
            encoded = _onnx_tokenizer.encode(text)
            ids = np.array([encoded.ids], dtype=np.int64)
            mask = np.array([encoded.attention_mask], dtype=np.int64)
            ttype = np.array([encoded.type_ids], dtype=np.int64)
            outputs = session.run(None, {
                'input_ids': ids,
                'attention_mask': mask,
                'token_type_ids': ttype,
            })
        vec = outputs[0][0, 0].tolist()
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec
    except Exception as e:
        print(f"[embedder] ONNX inference failed: {e}")
        return None


# ── Backend 1: Hashing TF-IDF (zero dependencies, always available) ──

_tfidf_vocab = {}           # word → {df: doc freq, idf: precomputed}
_tfidf_doc_count = 0
_tfidf_dim = 256            # fixed output dimension
_tfidf_lock = threading.Lock()
def _tokenize(text: str) -> list[str]:
    """Simple Chinese/English tokenizer — bigrams for CJK, whitespace for ASCII."""
    tokens = []
    # Split on non-alphanumeric, keep CJK chars
    # Chinese: character bigrams
    cjk = re.findall(r'[\u4e00-\u9fff]+', text)
    for seg in cjk:
        for i in range(len(seg)):
            tokens.append(seg[i])           # unigram
            if i < len(seg) - 1:
                tokens.append(seg[i:i+2])    # bigram
    # English/numbers: lowercase words
    en = re.findall(r'[a-zA-Z0-9]+', text)
    for w in en:
        tokens.append(w.lower())
    return tokens

def _hash_to_dim(token: str) -> int:
    """Hash a token to a fixed dimension index."""
    h = 0
    for c in token:
        h = ((h << 5) - h) + ord(c)
        h = h & 0x7FFFFFFF
    return h % _tfidf_dim

def _tfidf_embed(text: str) -> list[float]:
    """Embed using hashing TF-IDF. No model, no deps, <1ms per call."""
    tokens = _tokenize(text)
    if not tokens:
        return None

    # Count term frequencies
    tf = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1

    # Build vector using hashing trick + TF weighting
    vec = [0.0] * _tfidf_dim
    max_tf = max(tf.values()) if tf else 1

    with _tfidf_lock:
        for token, count in tf.items():
            idx = _hash_to_dim(token)
            # TF normalization + IDF bonus for rare terms
            tf_norm = count / max_tf
            idf = 1.0  # uniform without document stats
            if token in _tfidf_vocab:
                idf = math.log((_tfidf_doc_count + 1) / (df + 1)) + 1
            vec[idx] += tf_norm * idf

    # L2 normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]

    return vec

def _tfidf_add_document(text: str):
    """Register a document to improve IDF statistics."""
    tokens = set(_tokenize(text))
    with _tfidf_lock:
        global _tfidf_doc_count
        _tfidf_doc_count += 1
        for t in tokens:
            if t not in _tfidf_vocab:
                _tfidf_vocab[t] = {"df": 0}
            _tfidf_vocab[t]["df"] += 1


# ── Backend 1: sentence-transformers (in-process, no service needed) ──
_st_model = None
_st_loaded = False

def _load_st_model():
    """Lazy-load sentence-transformers model (thread-safe)."""
    global _st_model, _st_loaded
    if _st_loaded:
        return _st_model
    _st_loaded = True
    try:
        # Use HF mirror for China accessibility
        if "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        # 离线模式：直接用本地缓存加载，不做 HEAD 网络检查。
        # 网络差时这些检查每次超时 20s×重试，会阻塞服务启动数分钟。
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import sentence_transformers
        _st_model = sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2")
        dim = _st_model.get_sentence_embedding_dimension()
        print(f"[embedder] sentence-transformers loaded (dim={dim})")
        return _st_model
    except ImportError:
        print("[embedder] sentence-transformers not installed — run: pip install sentence-transformers")
        return None
    except Exception as e:
        print(f"[embedder] sentence-transformers load failed: {e}")
        return None


def _st_embed(text: str) -> list[float] | None:
    """Embed with sentence-transformers."""
    model = _load_st_model()
    if model is None:
        return None
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception:
        return None


# ── Backend 2: Ollama ──────────────────────────────

def _ollama_embed(text: str, model: str = "nomic-embed-text", base_url: str = "http://localhost:11434") -> list[float] | None:
    """Call Ollama embeddings API."""
    url = f"{base_url}/api/embeddings"
    body = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body)
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        emb = data.get("embedding", [])
        if emb and len(emb) > 0:
            return emb
    except (urllib.error.URLError, ConnectionRefusedError):
        pass
    except Exception:
        pass
    return None


# ── Backend 3: OpenAI-compatible API ───────────────

def _openai_embed(text: str, api_key: str = "", base_url: str = "https://api.deepseek.com") -> list[float] | None:
    """Call OpenAI-compatible embeddings API."""
    if not api_key:
        return None
    url = f"{base_url}/v1/embeddings"
    body = json.dumps({
        "model": "text-embedding-3-small",
        "input": text,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body)
        req.method = "POST"
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        emb = data.get("data", [{}])[0].get("embedding", [])
        if emb and len(emb) > 0:
            return emb
    except Exception:
        pass
    return None


# ── Public API ────────────────────────────────────

def embed(text: str) -> list[float]:
    """
    Embed text into a vector. Tries backends in priority order.
    Caches results by text hash. Returns zero vector on complete failure.
    """
    global _cache_hits, _cache_misses
    key = hash(text)
    if key in _cache:
        _cache_hits += 1
        return _cache[key]

    _cache_misses += 1
    # Priority 0: ONNX BGE-small-zh (best quality, no torch needed)
    emb = _onnx_embed(text)
    if emb is not None:
        _cache[key] = emb
        return emb

    # Priority 1: Hashing TF-IDF (always available, zero deps)
    emb = _tfidf_embed(text)
    if emb is not None:
        _cache[key] = emb
        return emb

    # Priority 2: Ollama (local service)
    emb = _ollama_embed(text)
    if emb is not None:
        _cache[key] = emb
        return emb

    # Priority 3: DeepSeek API (key from env or model_config.json)
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        try:
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "model_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                api_key = cfg.get("api_key", "")
        except Exception:
            pass
    if api_key:
        emb = _openai_embed(text, api_key=api_key)
        if emb is not None:
            _cache[key] = emb
            return emb

    # Fallback: zero vector → memory system degrades to anchor-only
    global _warned_zero_vector
    if not _warned_zero_vector:
        print("[memory] WARNING: No embedding backend available — memory recall degraded to anchor-only")
        _warned_zero_vector = True

    zero = [0.0] * EMBEDDING_DIM
    _cache[key] = zero
    return zero


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity (pure Python, no numpy dependency)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = (sum(x * x for x in a)) ** 0.5
    norm_b = (sum(x * x for x in b)) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cache_stats() -> dict:
    return {"size": len(_cache), "hits": _cache_hits, "misses": _cache_misses}


def clear_cache():
    global _cache, _cache_hits, _cache_misses
    _cache = {}
    _cache_hits = 0
    _cache_misses = 0
