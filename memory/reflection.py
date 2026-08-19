#!/usr/bin/env python3
"""
Memory AI — event-driven autobiographical memory agent using direct LLM calls.
取代 OMP-based reflection。

手动触发深度整合（/api/memory/reflect）。
"""
import os
import re
import sys
import threading
import time
import json
import urllib.request
import urllib.error

REFLECTION_PROMPT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reflection_prompt.txt")
CHECKPOINT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "reflection_checkpoint.json")
_DEFAULT_PROMPT = """像写日记一样，回顾刚才的对话。标记 [我] 的是我说的，[用户] 的是对方说的。

只记**[我]**做的事——我做对了什么、做错了什么、发现了什么模式、改变了什么看法。
不要把 [用户] 做的事写成我做的。对方做的是对方的事，不是我的经历。
不写用户的事（项目进度、个人信息），那些不是我的记忆，是项目笔记。
没什么新认知就跳过。

如果发现某些经历之间有因果联系——"原来那次是因为这个"、"跟上次一样又栽了"——
用 <<<因果>>>标记出来。这些会成为你记忆里的因果链。

格式：
<<<碎片记忆>>>
今天帮用户查SDK文档时我又忘了先用search，直接凭记忆回答结果错了
<<<碎片记忆>>>
<<<碎片记忆>>>
用户纠正了我三次"不是画图是聊天API"，我发现我老是把问题往画图方向引
<<<碎片记忆>>>"""

def _load_reflection_prompt():
    """Load reflection prompt from file, fall back to default."""
    try:
        if os.path.isfile(REFLECTION_PROMPT_FILE):
            with open(REFLECTION_PROMPT_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return _DEFAULT_PROMPT


def _load_checkpoint():
    """Load reflection checkpoint: topic_id → last processed message id."""
    try:
        if os.path.isfile(CHECKPOINT_FILE):
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return {}


def _save_checkpoint(data):
    """Atomically save reflection checkpoint."""
    try:
        os.makedirs(os.path.dirname(CHECKPOINT_FILE), exist_ok=True)
        tmp = CHECKPOINT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CHECKPOINT_FILE)
    except Exception as e:
        print(f"[memory] checkpoint save error: {e}")



def _llm_prompt(system_prompt, user_prompt, config=None):
    """Call LLM directly (non-streaming) and return the response text."""
    if config is None:
        from llm import LLMConfig
        config = LLMConfig.from_config(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "model_config.json")
        )

    url = f"{config.base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    body = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "max_tokens": config.max_tokens,
    }

    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        print(f"[memory] LLM error: HTTP {e.code} {err[:200]}")
        return ""
    except Exception as e:
        print(f"[memory] LLM error: {e}")
        return ""


class MemoryAI:
    """Manual-trigger memory integration agent. Uses direct LLM calls."""

    SYSTEM_TOPIC_ID = "__memory__"

    def __init__(self, db, llm_config=None):
        self.db = db
        self._llm_config = llm_config
        self._busy = False
        self._busy_lock = threading.Lock()
        self._last_run_ts = 0.0  # 上次反思回路完成时间
        self.MIN_INTERVAL = 300  # 最小间隔 5 分钟，避免每次对话都跑导致锁库

    def trigger_deep_integration(self):
        """Manual /api/memory/reflect entry point."""
        if self._busy:
            print("[memory] busy, deep integration skipped")
            return
        # 频率限制：距上次运行不足 5 分钟则跳过（手动 API 调用不受限）
        import time as _time
        elapsed = _time.time() - self._last_run_ts
        if elapsed < self.MIN_INTERVAL:
            print(f"[memory] rate-limited, last run {int(elapsed)}s ago, min {self.MIN_INTERVAL}s")
            return
        threading.Thread(target=self._run_deep_integration, daemon=True).start()

    # ── Deep integration ────────────────────────────

    def _run_deep_integration(self):
        if not self._acquire():
            return
        checkpoint = _load_checkpoint()
        updated = dict(checkpoint)
        try:
            all_parts = []
            topics = self.db.list_topics()
            for t in topics:
                if t["id"] == self.SYSTEM_TOPIC_ID:
                    continue
                msgs = self.db.get_messages(t["id"], limit=15)
                if not msgs:
                    continue
                # checkpoint 去重：该话题无新消息则跳过（纯机械判断，不改 prompt 输入 → 质量零变化）
                last_id = checkpoint.get(t["id"])
                if last_id is not None and msgs[-1]["id"] <= last_id:
                    continue
                all_parts.append(f'\n--- 话题: {t["title"]} ---')
                all_parts.append(self._format_messages(msgs, max_chars=800))
                updated[t["id"]] = msgs[-1]["id"]

            if not all_parts:
                print("[memory] deep integration: nothing new to reflect on")
                _save_checkpoint(updated)
                return

            prompt_text = _load_reflection_prompt() + "\n\n最近所有话题聊天内容：\n" + "\n".join(all_parts)
            print(f"[memory] deep integration ({len(prompt_text)} chars)")

            response = _llm_prompt("你是一个自我反思的AI助手", prompt_text, self._llm_config)
            fragments = re.findall(r'<<<碎片记忆>>>\s*(.*?)\s*<<<碎片记忆>>>', response, re.DOTALL)
            causals = re.findall(r'<<<因果>>>\s*(.*?)\s*<<<因果>>>', response, re.DOTALL)

            ts = time.strftime("%Y%m%d%H%M%S")
            count = self._store_fragments(fragments, ts=ts, source="deep")
            ccount = self._store_fragments(causals, ts=ts, source="causal")
            print(f"[memory] deep integration: {count} + {ccount} causals")

            from .fragment_store import get_store
            store = get_store()
            discarded = store.prune()
            if discarded:
                print(f"[memory] pruned {discarded} stale fragments")

            # ── CMN P4: 反思回路（建弱关联/涌现元晶/反熵/自检/提拔权威）──
            # 碎片沉淀完成后，自动跑反思回路维护网络结构
            try:
                from memory.reflection_loop import get_loop
                loop = get_loop()
                report = loop.run()
                if any(v for k, v in report.items() if k != "errors" and v):
                    print(f"[memory] reflection loop: weak={report['weak_assoc']} "
                          f"meta={report['meta_crystals']} pruned={report['pruned']} "
                          f"gaps={report['gaps']} promoted={report['promoted']}")
                if report["errors"]:
                    print(f"[memory] reflection loop errors: {report['errors']}")
            except Exception as e:
                print(f"[memory] reflection loop failed: {e}")

            # 反思成功后落盘 checkpoint（标记这些话题已处理到最新消息）
            _save_checkpoint(updated)

        except Exception as e:
            print(f"[memory] deep integration error: {e}")
        finally:
            import time as _time
            self._last_run_ts = _time.time()  # 记录完成时间，供频率限制用
            self._release()

    # ── Busy management ─────────────────────────────

    def _acquire(self):
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release(self):
        with self._busy_lock:
            self._busy = False

    # ── Utils ───────────────────────────────────────

    def _store_fragments(self, fragments, ts, source, topic_id=None):
        from .fragment_store import get_store
        store = get_store()
        count = 0
        for frag in fragments:
            frag = frag.strip()
            if 5 <= len(frag) <= 300:
                try:
                    store.add(frag, ts=ts, source=source, topic_id=topic_id)
                    count += 1
                except Exception:
                    pass
        return count

    def _format_messages(self, msgs, max_chars=800):
        parts = []
        chars = 0
        for m in msgs:
            text = m.get("text", "")
            if not text:
                continue
            snippet = text[:300]
            role = m.get("role", "")
            label = "用户" if role == "user" else "我"
            line = f"[{label}] {snippet}"
            if chars + len(line) > max_chars:
                break
            parts.append(line)
            chars += len(line)
        return "\n".join(parts)


# ── Singleton ─────────────────────────────────────

_memory_ai = None


def get_memory_ai(db, llm_config=None):
    global _memory_ai
    if _memory_ai is None:
        _memory_ai = MemoryAI(db, llm_config)
    return _memory_ai


def trigger_deep_integration_now(db, llm_config=None):
    ai = get_memory_ai(db, llm_config)
    ai.trigger_deep_integration()
