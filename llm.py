#!/usr/bin/env python3
"""
LLM API client — direct OpenAI-compatible calls, streaming, function calling.
完全取代 ompQ.exe 的角色。
"""
import json
import os
import ssl
import base64
import urllib.request
import time

# 部分环境（企业 VPN / 抓包软件 / 杀软）会向 HTTPS 链路注入自签名证书，
# 导致 urllib 默认证书校验失败，DeepSeek 等 API 调用全部报
# SSL: CERTIFICATE_VERIFY_FAILED。本服务为本地部署，禁用证书校验可接受。
_UNVERIFIED_SSL_CTX = ssl.create_default_context()
_UNVERIFIED_SSL_CTX.check_hostname = False
_UNVERIFIED_SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Event types (compatible with 大眼 frontend) ────
EVENT_THINKING_DELTA = "thinking_delta"
EVENT_TEXT_DELTA = "text_delta"
EVENT_TOOL_CALL = "tool_call"
EVENT_DONE = "done"
EVENT_ERROR = "error"

# ── 连接层瞬时故障识别（网关断连/超时等，可安全重试）──
TRANSIENT_MARKERS = ("remote end closed", "timed out", "timeout",
                     "connection reset", "connection aborted",
                     "connection refused", "eof occurred",
                     "temporarily unavailable", "no route to host",
                     "network is unreachable", "name or service not known",
                     # Windows 连接层瞬时故障文案（urllib 抛 WinError）
                     "winerror 10054", "winerror 10053", "winerror 10060",
                     "forcibly closed", "existing connection was aborted")

def is_transient_conn_error(e):
    s = str(e).lower()
    return any(m in s for m in TRANSIENT_MARKERS)


class LLMError(Exception):
    pass


class LLMConfig:
    """Per-provider config, loaded from model_config.json."""
    def __init__(self, provider="deepseek", model="deepseek-chat",
                 base_url="https://api.deepseek.com", api_key="",
                 max_tokens=8192, fallback_chain=None):
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.max_tokens = max_tokens
        self.fallback_chain = fallback_chain or []

    @classmethod
    def from_config(cls, path="model_config.json"):
        cfg = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        provider = cfg.get("provider", "deepseek")
        model = cfg.get("model", "deepseek-chat")
        base_url = cfg.get("base_url", "https://api.deepseek.com")
        api_key = cfg.get("api_key", "")
        max_tokens = cfg.get("max_tokens", 8192)
        fallback = cfg.get("fallback_chain", [])
        return cls(provider=provider, model=model, base_url=base_url,
                   api_key=api_key, max_tokens=max_tokens,
                   fallback_chain=fallback)

    def to_dict(self):
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
        }

    def clone_with(self, **kwargs):
        d = self.__dict__.copy()
        for k, v in kwargs.items():
            if k in d:
                d[k] = v
        return LLMConfig(**{k: d[k] for k in LLMConfig.__init__.__code__.co_varnames
                           if k in d and k != 'self'})


def _build_messages(system_msg=None, history=None, new_msg=None):
    """Build messages array for the LLM API."""
    msgs = []
    if system_msg:
        msgs.append({"role": "system", "content": system_msg})
    if history:
        msgs.extend(history)
    if new_msg:
        msgs.append({"role": "user", "content": new_msg})
    return msgs


# ── 多模态（vision）支持 ──
# DeepSeek vision 模型（deepseek-v4-flash-vision-exp 等）通过 OpenAI 兼容的
# content 数组接收图片：{"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}。
# 本地图片走 base64 data URL；非 vision 模型不认 image 块，需在发送前剥离。

_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# 显式声明支持图片的模型（模型名不含 "vision" 但原生多模态）。
# GLM-5.3-Flash 是智谱原生多模态模型，通过 OpenAI 兼容接口收图。
_VISION_MODEL_PREFIXES = ("glm-5.3-flash",)


def image_mime_type(path):
    """返回图片路径对应的 MIME，非图片返回 None。"""
    return _IMAGE_MIME.get(os.path.splitext(str(path))[1].lower())


def is_vision_model(model):
    """支持图片的模型：显式前缀集合（glm-5.3-flash 等原生多模态）或模型名含 vision。"""
    if not model:
        return False
    mid = str(model).lower()
    for prefix in _VISION_MODEL_PREFIXES:
        if mid == prefix or mid.startswith(prefix + "-") or mid.startswith(prefix + ":"):
            return True
    return "vision" in mid


def build_multimodal_content(text, image_paths):
    """把文本 + 本地图片路径列表编码成 OpenAI 多模态 content 数组。

    返回 None 表示没有任何图片成功编码（调用方应回退到纯文本）。
    """
    content = []
    if text:
        content.append({"type": "text", "text": text})
    encoded = 0
    for p in image_paths:
        mime = image_mime_type(p)
        if not mime:
            continue
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            print(f"[llm] 图片读取失败 {p}: {e}")
            continue
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        encoded += 1
    if encoded == 0:
        return None
    return content


def strip_image_content(messages, model):
    """非 vision 模型：把 messages 里 content 为数组的图片消息降级为纯文本。

    - vision 模型：原样返回（保留 image 块）。
    - 非 vision 模型：删掉 image_url 块，text 块拼回字符串，避免 API 报错。
    返回新列表（不修改入参）。
    """
    if is_vision_model(model):
        return messages
    out = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            m2 = dict(m)
            m2["content"] = "\n".join(t for t in texts if t)
            out.append(m2)
        else:
            out.append(m)
    return out


# ── Tool schema compaction
# 单工具 schema 超过预算时，按损失从小到大依次压缩：
#   1. 删 properties/items 上的 description
#   2. 删 $defs/definitions，$ref → {}
#   3. 深度≥3 的复杂对象塌缩成 {}
#   4. 删 anyOf/oneOf/allOf
# 顶层参数 surface（参数名 + type + required + enum）始终保留，模型仍能正确传参。
# 参考: codex-rs/tools/src/json_schema.rs (compact_large_tool_schema)
_TOOL_SCHEMA_BUDGET_BYTES = 5_000  # ≈ 1k token，与 Codex 一致
_TOOL_SCHEMA_MAX_DEPTH = 3


def _schema_size_bytes(node):
    """廉价体积估计：序列化后的 UTF-8 字节数。"""
    try:
        return len(json.dumps(node, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _strip_inner_descriptions(node):
    """删 properties/items 上的 description，但保留顶层 tool description。"""
    if isinstance(node, dict):
        for key in ("properties", "items"):
            child = node.get(key)
            if isinstance(child, dict):
                for prop_schema in child.values():
                    if isinstance(prop_schema, dict) and "description" in prop_schema:
                        prop_schema.pop("description", None)
                        _strip_inner_descriptions(prop_schema)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, dict) and "description" in item:
                        item.pop("description", None)
                        _strip_inner_descriptions(item)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _strip_inner_descriptions(v)
    elif isinstance(node, list):
        for item in node:
            _strip_inner_descriptions(item)


def _drop_definitions(node):
    """删 $defs/definitions，把 $ref 改成 {} 避免悬空引用。"""
    if isinstance(node, dict):
        if "$ref" in node:
            node.clear()
            node["type"] = "object"
            return
        node.pop("$defs", None)
        node.pop("definitions", None)
        for v in node.values():
            _drop_definitions(v)
    elif isinstance(node, list):
        for item in node:
            _drop_definitions(item)


def _collapse_deep_objects(node, depth=0):
    """深度≥3 的复杂对象塌缩成 {}（保留顶层参数 surface）。"""
    if isinstance(node, dict):
        # 顶层 properties 不塌缩（保留参数名）
        if depth >= _TOOL_SCHEMA_MAX_DEPTH and "properties" in node:
            node.clear()
            node["type"] = "object"
            return
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, dict):
                _collapse_deep_objects(v, depth + 1)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _collapse_deep_objects(item, depth + 1)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                _collapse_deep_objects(item, depth)


def _prune_compositions(node):
    """删 anyOf/oneOf/allOf（最后手段，损失最大）。"""
    if isinstance(node, dict):
        for key in ("anyOf", "oneOf", "allOf"):
            node.pop(key, None)
        for v in node.values():
            _prune_compositions(v)
    elif isinstance(node, list):
        for item in node:
            _prune_compositions(item)


_TOOL_SCHEMA_COMPACTION_PASSES = [
    _strip_inner_descriptions,
    _drop_definitions,
    _collapse_deep_objects,
    _prune_compositions,
]


def compact_tool_schema(parameters):
    """对单个工具的 parameters schema 做多遍有损压缩，直到满足预算。

    输入应为可变 dict（会被就地修改）。返回压缩后的 schema。
    顶层参数 surface（properties 的 key 名、type、required、enum）始终保留。
    """
    if not isinstance(parameters, dict):
        return parameters
    import copy
    schema = copy.deepcopy(parameters)
    if _schema_size_bytes(schema) <= _TOOL_SCHEMA_BUDGET_BYTES:
        return schema
    for pass_fn in _TOOL_SCHEMA_COMPACTION_PASSES:
        if _schema_size_bytes(schema) <= _TOOL_SCHEMA_BUDGET_BYTES:
            break
        try:
            pass_fn(schema)
        except Exception:
            # 任意一遍失败都继续，最坏情况是 schema 体积超标
            continue
    return schema


def _build_tools(tool_defs):
    """Build tools array for OpenAI function calling API, with schema compaction.

    对每个工具的 parameters schema 应用多遍有损压缩（借鉴 Codex），
    在不影响模型选参/传参能力的前提下显著降低 token 占用。
    """
    if not tool_defs:
        return None
    built = []
    for t in tool_defs:
        params = t.get("parameters", {"type": "object", "properties": {}})
        compacted = compact_tool_schema(params)
        built.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": compacted,
            }
        })
    return built


def _log_raw_request(url, body, config):
    """Save raw API request. Returns log_group pair."""
    try:
        from api_logger import save_raw_request
        return save_raw_request("", url, body, config.model)
    except Exception:
        return None


def _log_raw_response(log_group, raw_sse_text, duration_ms, status_code=200,
                       input_tokens=0, output_tokens=0):
    """Save raw API response."""
    if log_group is None:
        return
    try:
        from api_logger import save_raw_response
        save_raw_response(log_group, raw_sse_text, status_code, duration_ms,
                           input_tokens, output_tokens)
    except Exception:
        pass


def chat_stream(config, messages, tools=None, cancel_event=None, verify_ssl=True):
    """
    Stream a chat completion from the LLM API.
    Yields dicts: {"type": EVENT_THINKING_DELTA|TEXT_DELTA|TOOL_CALL|DONE|ERROR, ...}
    verify_ssl: True=校验证书(默认安全), False=跳过校验(用于 VPN/抓包等证书注入环境)
    """
    url = f"{config.base_url}/chat/completions"
    # 防御：api_key 含非 ASCII 字符时清空，避免 latin-1 编码崩溃
    safe_key = config.api_key
    try:
        safe_key.encode("latin-1")
    except UnicodeEncodeError:
        print(f"[llm] WARNING: api_key 含非 ASCII 字符，已忽略 (provider={config.provider})")
        safe_key = ""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {safe_key}",
    }
    # Strip internal-only fields from messages before sending to API
    clean_messages = []
    for m in messages:
        clean = {k: v for k, v in m.items() if not k.startswith("_")}
        clean_messages.append(clean)

    body = {
        "model": config.model,
        "messages": clean_messages,
        "stream": True,
        "max_tokens": config.max_tokens,
    }
    tool_spec = _build_tools(tools)
    if tool_spec:
        body["tools"] = tool_spec
        body["tool_choice"] = "auto"
    body["stream_options"] = {"include_usage": True}

    # ── Log raw request before sending ──
    log_group = _log_raw_request(url, body, config)
    t0 = time.time()
    raw_sse_chunks = []

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    ssl_ctx = None if verify_ssl else _UNVERIFIED_SSL_CTX
    # ── 连接层瞬时故障自动重试 ──
    # DeepSeek 网关偶发在 ~15s 无任何响应直接断连（Remote end closed connection
    # without response），属服务端/链路瞬时故障。此处对连接阶段错误自动重试，
    # 避免单次断连直接暴露给用户。
    resp = None
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = urllib.request.urlopen(req, timeout=120, context=ssl_ctx)
            break
        except urllib.error.HTTPError as e:
            # HTTP 层错误（4xx/5xx）不重试，直接上报
            error_body = e.read().decode("utf-8", errors="replace")
            _log_raw_response(log_group, f"HTTP {e.code}: {error_body}",
                              duration_ms=(time.time() - t0) * 1000,
                              status_code=e.code)
            yield {"type": EVENT_ERROR, "error": f"HTTP {e.code}: {error_body}"}
            return
        except Exception as e:
            if attempt < max_attempts - 1 and is_transient_conn_error(e):
                wait = 1.5 * (attempt + 1)
                print(f"[llm] transient connection error (attempt {attempt + 1}/{max_attempts}), retry in {wait}s: {e}")
                time.sleep(wait)
                continue
            _log_raw_response(log_group, str(e),
                               duration_ms=(time.time() - t0) * 1000,
                               status_code=0)
            yield {"type": EVENT_ERROR, "error": str(e)}
            return

    tool_calls_buffer = {}
    full_text = ""
    full_thinking = ""
    last_usage = {}
    done_yielded = False

    try:
        for raw_line in resp:
            if cancel_event and cancel_event.is_set():
                resp.close()
                _log_raw_response(log_group, "cancelled",
                                   duration_ms=(time.time() - t0) * 1000,
                                   status_code=0)
                yield {"type": EVENT_ERROR, "error": "cancelled"}
                return
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data: "):
                continue
            data = line[6:]
            raw_sse_chunks.append(data)  # capture raw data for logging
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            # Capture usage from final chunk
            if "usage" in obj and obj["usage"] is not None:
                last_usage = obj["usage"]

            choices = obj.get("choices", [])

            if not choices:
                continue
            delta = choices[0].get("delta", {})

            finish_reason = choices[0].get("finish_reason")

            # Thinking content (DeepSeek reasoning_content / Ollama reasoning)
            thinking = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or ""
            if thinking:
                full_thinking += thinking
                yield {"type": EVENT_THINKING_DELTA, "delta": thinking}

            # Text content
            content = delta.get("content", "")
            if content:
                full_text += content
                yield {"type": EVENT_TEXT_DELTA, "delta": content}

            # Tool calls (accumulate across chunks)
            tool_calls = delta.get("tool_calls", [])
            for tc in tool_calls:
                idx = tc.get("index", 0)
                if idx not in tool_calls_buffer:
                    tool_calls_buffer[idx] = {
                        "name": "",
                        "args": "",
                        "id": tc.get("id", f"call_{idx}"),
                    }
                func = tc.get("function", {})
                if func.get("name"):
                    tool_calls_buffer[idx]["name"] += func["name"]
                if func.get("arguments"):
                    tool_calls_buffer[idx]["args"] += func["arguments"]

            # Tool calls complete — emit them
            if finish_reason == "tool_calls":
                for idx in sorted(tool_calls_buffer.keys()):
                    tc = tool_calls_buffer[idx]
                    try:
                        args = json.loads(tc["args"]) if tc["args"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield {
                        "type": EVENT_TOOL_CALL,
                        "tool_call_id": tc["id"],
                        "tool_name": tc["name"],
                        "arguments": args,
                    }
                tool_calls_buffer.clear()

            # Normal stop
            if finish_reason in ("stop", "length"):
                done_yielded = True
                yield {"type": EVENT_DONE, "finish_reason": finish_reason,
                       "full_text": full_text, "full_thinking": full_thinking,
                       "usage": last_usage}
    except Exception as e:
        _log_raw_response(log_group,
                           f"STREAM ERROR: {str(e)}\nRAW: {raw_sse_chunks[-1] if raw_sse_chunks else ''}",
                           duration_ms=(time.time() - t0) * 1000,
                           status_code=200)
        yield {"type": EVENT_ERROR, "error": str(e)}
        return

    if not done_yielded:
        yield {"type": EVENT_DONE, "finish_reason": "stop",
               "full_text": full_text, "full_thinking": full_thinking,
               "usage": last_usage or {}}

    # ── Log raw response after stream completes ──
    duration_ms = (time.time() - t0) * 1000
    raw_body = "\n".join(raw_sse_chunks) if raw_sse_chunks else ""
    usage = last_usage or {}
    _log_raw_response(log_group, raw_body,
                       duration_ms=duration_ms, status_code=200,
                       input_tokens=usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0),
                       output_tokens=usage.get("completion_tokens", 0) or usage.get("output_tokens", 0))


# ── 全链路本地缓存 ──────────────────────────────────────
# 默认关闭，由 server.py 根据 meta.llm_cache_enabled 切换。
# 开启后：所有 chat_stream 调用先查本地缓存，命中即整段重放（秒回），
# 未命中走原逻辑；纯文本回复（无 tool_calls）落库供下次命中。
# 带 tool_calls / ERROR 的响应不缓存（工具必须真执行）。
_LLM_CACHE_ENABLED = False


def set_cache_enabled(on):
    """开关本地缓存（由 server 根据用户设置调用）。"""
    global _LLM_CACHE_ENABLED
    _LLM_CACHE_ENABLED = bool(on)


def is_cache_enabled():
    return _LLM_CACHE_ENABLED


def chat_stream_cached(config, messages, tools=None, cancel_event=None, verify_ssl=True):
    """带本地缓存层的 chat_stream 包装。

    命中：一次性重放 text/thinking + DONE，不走网络。
    未命中：透传原 chat_stream 事件，并在纯文本回复结束时落库。
    带 tool_calls / ERROR 的响应不缓存。
    """
    if not _LLM_CACHE_ENABLED:
        # 开关关闭：完全透传，零开销
        yield from chat_stream(config, messages, tools=tools,
                               cancel_event=cancel_event, verify_ssl=verify_ssl)
        return

    try:
        import llm_cache
        cache = llm_cache.get_cache()
        key = llm_cache.compute_key(config, messages, tools, config.max_tokens)
        hit = cache.get(key)
    except Exception:
        # 缓存层任何异常都不影响主流程，直接透传
        yield from chat_stream(config, messages, tools=tools,
                               cancel_event=cancel_event, verify_ssl=verify_ssl)
        return

    if hit:
        # ── 命中：重放 ──
        # usage 归零：命中没走 API、没花钱，重放旧 usage 会让上层重复计费
        thinking = hit.get("full_thinking") or ""
        text = hit.get("full_text") or ""
        if thinking:
            yield {"type": EVENT_THINKING_DELTA, "delta": thinking}
        if text:
            yield {"type": EVENT_TEXT_DELTA, "delta": text}
        yield {"type": EVENT_DONE, "finish_reason": "stop",
               "full_text": text, "full_thinking": thinking,
               "usage": {}, "_cache_hit": True}
        return

    # ── 未命中：透传，并收集结果用于落库 ──
    collected_text = ""
    collected_thinking = ""
    collected_usage = {}
    had_tool_calls = False
    had_error = False
    done_emitted = False

    for event in chat_stream(config, messages, tools=tools,
                             cancel_event=cancel_event, verify_ssl=verify_ssl):
        et = event.get("type")
        if et == EVENT_TEXT_DELTA:
            collected_text += event.get("delta", "")
        elif et == EVENT_THINKING_DELTA:
            collected_thinking += event.get("delta", "")
        elif et == EVENT_TOOL_CALL:
            had_tool_calls = True
        elif et == EVENT_ERROR:
            had_error = True
        elif et == EVENT_DONE:
            done_emitted = True
            collected_text = event.get("full_text", collected_text)
            collected_thinking = event.get("full_thinking", collected_thinking)
            collected_usage = event.get("usage", {}) or {}
        yield event

    # ── 落库条件：正常结束 + 无 tool_calls + 无错误 + 有内容 ──
    if (done_emitted and not had_tool_calls and not had_error
            and (collected_text or collected_thinking)):
        try:
            cache.put(key, config.model, collected_text, collected_thinking,
                      collected_usage, has_tool_calls=0)
        except Exception:
            pass


def chat_once_cached(config, messages, tools=None, verify_ssl=True):
    """非流式入口：返回完整文本。带本地缓存层，命中即秒回。

    供 memory/reflection 等非流式调用方使用，确保全链路覆盖。
    返回 dict: {"text": str, "thinking": str, "usage": dict, "cache_hit": bool}
    """
    text = ""
    thinking = ""
    usage = {}
    cache_hit = False
    for event in chat_stream_cached(config, messages, tools=tools,
                                    cancel_event=None, verify_ssl=verify_ssl):
        et = event.get("type")
        if et == EVENT_TEXT_DELTA:
            text += event.get("delta", "")
        elif et == EVENT_THINKING_DELTA:
            thinking += event.get("delta", "")
        elif et == EVENT_DONE:
            text = event.get("full_text", text)
            thinking = event.get("full_thinking", thinking)
            usage = event.get("usage", {}) or {}
            cache_hit = bool(event.get("_cache_hit", False))
        elif et == EVENT_ERROR:
            return {"text": "", "thinking": "", "usage": {},
                    "cache_hit": False, "error": event.get("error", "")}
    return {"text": text, "thinking": thinking, "usage": usage,
            "cache_hit": cache_hit}
