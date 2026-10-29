"""动作链 —— harness 层的「每轮动作串联 + 对账」。

为什么需要它（2026-10-28）：
    模型在**单轮内**是理性的，跨轮会漂移。因为它每一轮只看到 tool 结果，
    看不到「我走过来的路」——于是会：
      · 同一个动作失败三次，还在换姿势重试同一件事
      · 说好了「接下来验证一下」，下一句直接给出结论
      · 声称「改完了」，但整轮没有任何执行 / 读回证据
    这些都不是模型能力问题，是 harness 没给账本。账本必须是**框架维护**的：
    模型不会替自己记账（要求它调工具写日记的机制，实测一个月只落了 20 步）。

与 task/thought_chain.py 的关系（2026-10-28 修正过一版，别再搞成两条链）：
    thought_chain = **存储 + 展示**。server.py 的 Block 2.6 每轮把它的
                    format_hot_layer(5) 注进 system prompt（"[思维链 — 最近执行足迹]"），
                    所以「最近的几条动作一直在上下文里」这件事本来就已经成立。
    action_chain  = **对账 + 写步**。它不再自己存一份，每轮 record() 完直接往
                    ThoughtChain 追加一步 —— 一条链、一个文件、一处展示。
    为什么改：第一版两条链各写各的（data/action_chains/ + data/thought_chains/），
    上下文里只有后者，前者的 render_tail 从此再没被调用过 = 死代码。
    同一语义写两遍，必然有一条是白写的。

设计约束（都是踩出来的）：
  · 纯规则、零 LLM 调用。链的记录不做任何语义理解，只做结构化。
  · **链的正文不常驻上下文**（它要的信息 tool 消息里本来就有，每轮重发一遍
    纯属浪费 token 且会冲掉前缀缓存）；常驻的只有 5 行足迹，靠 ThoughtChain 那侧。
    有问题时的**告警**才直接插进 msgs。
  · 熔断：每话题告警上限 + 冷却 + 自指保护。参考 loop_detector 的事故：
    模型收到提醒后去"调查提醒机制本身"，探索类调用又触发提醒 → 真死循环。
  · 任何异常静默。账本坏了不能拖累主循环。
"""

import json
import os
import re
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── 参数 ──
MAX_TURNS = 24          # 链只留最近 N 轮（长任务也够，省内存）
MAX_INJECTS = 4         # 每话题告警上限（硬熔断）
COOLDOWN = 45.0         # 同类告警最小间隔（秒）
SAID_CLIP = 160         # 每轮「模型说的话」最多存多少字
BRIEF_CLIP = 56         # 单个动作的参数摘要长度

# ── 工具分类：动作的「类别」比工具名更重要 —— 对账比的是类别，不是具体工具 ──
READ_TOOLS = {"read_file", "grep", "file_search", "code_ast_parse",
              "code_find_defs", "code_get_symbol", "expand_compressed",
              "get_task_dag", "get_mindmap", "list_files", "read_topic_messages"}
WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "move_file", "delete_file"}
RUN_TOOLS = {"bash", "run_python", "check_python"}
SEARCH_TOOLS = {"web_search", "web_fetch", "fetch_url"}


def _cat(tool_name: str, args: dict = None) -> str:
    """工具 → 动作类别：read / write / run / search / other。"""
    inner = tool_name or ""
    if tool_name == "execute_advanced_tool" and isinstance(args, dict):
        inner = args.get("name") or ""
    if inner in READ_TOOLS:
        return "read"
    if inner in WRITE_TOOLS:
        return "write"
    if inner in RUN_TOOLS:
        return "run"
    if inner in SEARCH_TOOLS:
        return "search"
    return "other"


# ── 承诺检测：模型说的话里，「要做某事」的部分 ──
# 只认「主语省略的将来时」——「我先把 X 改了」「接下来验一下」。
# 不认「已经做了」（那是陈述），否则每一轮都会被自己判成承诺未兑现。
_INTENT_RE = re.compile(
    r"(?:我(?:先|来|去|要|这就|准备|马上)|接下来|下一步|然后|先|这就去|准备)"
    r"\s*([^。；;！!？?\n，,]{0,30})"
)
# 动作词 → 类别（同一类别多种说法）
_VERB_CAT = (
    ("write", ("改", "写", "修", "加", "删", "替换", "补", "创建", "生成文件", "edit", "write", "patch")),
    ("run", ("跑", "测", "试", "验证", "执行", "运行", "复现", "benchmark", "run", "test")),
    ("search", ("搜", "查资料", "查一下", "检索", "fetch", "search")),
    ("read", ("看", "读", "查", "找", "确认", "检查", "核对", "梳理", "review", "read", "grep")),
)

# 完成声明（声称做完）—— 与「未验证」配对使用
_DONE_RE = re.compile(
    r"(?:改好|写好了|修好|做好|搞定了|搞定|完成|实现|已改|已修|已加|已替换|应该(?:就)?(?:好|行|可以)了)"
)

# ── 自指保护：命中即静默（loop_detector 事故同款）──
_SELF_REF = ("action_chain", "动作链", "对账", "audit", "核账", "账本机制")

# 用户这一轮明确"只要答案、别动东西"。命中就不拿"没写/没跑"去告警（见 _report_only）。
_REPORT_ONLY = re.compile(
    r"别(动|改|碰|写|删|执行)|不要(动|改|碰|写|删|执行)|先别(做|动|改)|"
    r"不需要(改|动|修改|执行)|只(回答|汇报|解释|说明|复述)|"
    r"用\s*[一二三四五两\d]+\s*句(话)?(回答|说)|回答我|"
    r"停下?手(上|头)?的?活")


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else s[: n - 1] + "…"


def intent_cats(said: str) -> set:
    """从「模型说的话」里提取它承诺要做的动作类别。返回 {'write','run',...}。"""
    out = set()
    for m in _INTENT_RE.finditer(said or ""):
        seg = (m.group(1) or "").lower()
        if not seg:
            continue
        for cat, verbs in _VERB_CAT:
            if any(v in seg for v in verbs):
                out.add(cat)
                break
    return out


@dataclass
class Action:
    """一次工具调用。ok=False 表示工具层失败（顶层带 error），不代表目标没达成。"""
    tool: str
    cat: str
    brief: str
    ok: bool = True
    err: str = ""

    def line(self) -> str:
        mark = "" if self.ok else "✗ "
        s = f"{mark}{self.tool}({self.brief})"
        if not self.ok and self.err:
            s += " → " + self.err
        return s


@dataclass
class TurnNode:
    """一轮 = 模型说了什么 + 实际做了什么。"""
    n: int
    said: str = ""                     # 模型这轮的正文（裁剪）
    actions: list = field(default_factory=list)   # List[Action]
    final: bool = False                # 这一轮是不是收尾轮（无工具调用）

    @property
    def cats(self) -> set:
        return {a.cat for a in self.actions}

    @property
    def all_failed(self) -> bool:
        return bool(self.actions) and all(not a.ok for a in self.actions)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class ActionChain:
    """单话题的动作链。

    内存里是 TurnNode 列表（对账要用）；落盘统一走 ThoughtChain
    （data/thought_chains/<tid>.jsonl）—— 那是 Block 2.6 注进上下文的那一份。
    """

    def __init__(self, topic_id: str, persist: bool = True):
        self.topic_id = topic_id
        self.turns: deque = deque(maxlen=MAX_TURNS)
        self.persist = persist        # 是否往思维链写步（探针置 False，别污染真数据）
        self.goal = ""                 # 本链的任务原句（用于漂移对账）
        self.inject_count = 0
        self._last_fire = {}           # rule -> ts
        self._pending_promises = {}    # cat -> 提出承诺的轮次（跨轮未兑现的账）
        self._last_settled = set()     # 最近一轮兑现掉的类别（给 R2 用）
        # 🔴 必须是 RLock：audit() 持锁期间，各条 rule 内部还会调 recent()
        #    （recent 自己也加锁）。用普通 Lock 会当场死锁 —— 探针第一版就卡在这里，
        #    现象是进程无输出、无异常、被外部当超时杀掉。
        self._lock = threading.RLock()

    # ── 写 ──

    def set_goal(self, goal: str):
        """设置当前任务原句。每次新请求都会覆盖 —— 同一个话题里换任务很常见。"""
        if goal:
            self.goal = _clip(goal, 200)

    def begin_request(self, goal: str = ""):
        """每次新的对话请求开始时调用：刷新目标 + 重置本次请求的告警配额。

        为什么必须重置配额：MAX_INJECTS 是「每段工作」的熔断，不是话题一生的。
        不重置的话，同一话题问第二件事时账本已经哑了（实测第一版就是这样）。
        链节点**不**清空 —— 同一话题前后是连续的工作，历史步是判断漂移的依据。
        """
        self.set_goal(goal)
        self.inject_count = 0
        self._last_fire = {}
        # 跨请求的欠账不清：上一段说"要改 X"没改，这一段正好该被点出来。

    def record(self, n: int, said: str = "", tool_results=None,
               final: bool = False, reasoning: str = "") -> TurnNode:
        """记录一轮。tool_results = [(tool_name, args_dict, result_json_str), ...]

        final=True：收尾轮（没有工具调用）。
        reasoning：本轮原生 thinking，只在没有旁白时兜底当摘要（够长才用）。
        """
        acts = []
        for item in (tool_results or []):
            try:
                name, args, res_json = item
            except Exception:
                continue
            ok, err = _result_status(res_json)
            acts.append(Action(tool=name or "?", cat=_cat(name, args),
                               brief=_brief_args(args, name), ok=ok, err=err))
        node = TurnNode(n=n, said=_clip(said, SAID_CLIP), actions=acts, final=final)
        with self._lock:
            self.turns.append(node)
            self._settle_promises(node)
        self._to_thought(node, reasoning)
        return node

    # ── 落到思维链（上下文里那份「最近执行足迹」的唯一写入口）──
    def _to_thought(self, node: TurnNode, reasoning: str = "") -> None:
        """把这一轮写成一步：摘要 = 模型说了什么，结果行 = 它实际做了什么。

        🔴 别在这里另存一份。Block 2.6（server.py `_handle_chat` 的 blocks 里）
        已经把 ThoughtChain 的热层注进 system prompt，写进它就是写进上下文。
        """
        if not self.persist or not self.topic_id:
            return
        # 🔴 别把「跟审计员争辩」落成一步。
        # 实测（蓝牙任务 2026-10-28）：对账告警触发后，模型下一轮的旁白变成
        # 「这轮的任务是三句话汇报，不是执行类操作……属未验证但无需验证的情形」
        # 这种话被蒸馏成摘要写进链，而链又注回 system —— 下一个自己读到的是
        # 一段辩解，不是判断。一次告警会污染后面好几步。
        if sum(1 for k in _SELF_REF
               if k.lower() in ((node.said or "") + " " + (reasoning or "")).lower()) >= 1:
            return
        try:
            from task.thought_chain import (ThoughtChain, distill_narrative,
                                            distill_reasoning)
            summary, nxt = "", ""
            d = distill_narrative(node.said) if node.said else None
            if d:
                summary = d.get("summary") or ""
                nxt = d.get("next") or ""
            # reasoning 同时是「摘要的兜底」和「下一步的兜底」：旁白常常只说结论
            # （「注册通了，是 mock relay 崩了」），打算留在 thinking 里。
            if reasoning and (not summary or not nxt):
                dr = distill_reasoning(reasoning)
                if dr:
                    if not summary:
                        summary = dr.get("summary") or ""
                    if not nxt:
                        nxt = dr.get("next") or ""
            # 摘要退化时只用「工具(参数)」，不带错误尾巴 —— 实测带上错误尾巴会被
            # _clip 截成 "✗ execute_advanced_tool(" 这种半截括号，在足迹里没法读。
            acts_brief = " / ".join(
                ("✗ " if not a.ok else "") + _short_call(a) for a in node.actions)
            if not summary:
                # 没旁白也没够长的 thinking：至少让「做了什么」留在足迹里
                summary = _clip(acts_brief, 72)
            if not summary:
                return
            tc = ThoughtChain(self.topic_id)
            last = None
            try:
                last = tc.last_step
            except Exception:
                pass
            if last is not None and last.summary == summary \
                    and (time.time() - last.timestamp) < 90:
                return                      # 同一轮被多处调用，只写一次
            # result 只存**失败**：成功动作原样躺在上一轮的 tool 消息里，热层再印一遍
            # 是纯重复；而失败是唯一「看回来不到」的信息（5 步前的 tool 结果已滚出窗口）。
            fails = " / ".join(
                "✗ " + _clip(_short_call(a) + (" → " + a.err if a.err else ""), 90)
                for a in node.actions if not a.ok)
            detail = "turn %d" % node.n
            if node.said:
                detail += " · 说了：" + _clip(node.said, 90)
            if acts_brief:
                detail += " · 动作：" + _clip(acts_brief, 200)
            tc.append("final" if node.final else "action",
                      summary,
                      "",                           # motivation：原话已进 detail，
                                                    # 摘要/动机重复是纯浪费字段
                      fails,                        # result：只留失败项
                      nxt,                          # next_suggestion：它打算接着做什么
                      detail)                       # detail：往回翻时看这一行
        except Exception:
            pass                                # 记账失败不能拖累主循环


    # ── 跨轮承诺账本 ──
    # 只记「强承诺」（写/跑）。「我先看一下」这类轻承诺不记 —— 模型说「看看」
    # 之后去做别的事是正常的，记了就是误报。
    # 兑现窗口 2 轮：说了当轮做或下一轮做都算兑现（多轮 loop 里很常见）。
    def _settle_promises(self, node: TurnNode):
        self._last_settled = set()
        # 先兑现：本轮动作覆盖到的类别
        if node.actions:
            for cat in list(self._pending_promises):
                if cat in node.cats:
                    self._pending_promises.pop(cat, None)
                    self._last_settled.add(cat)
        for cat in intent_cats(node.said):
            if cat in ("write", "run"):
                # 本轮就已经做了 → 不算欠账
                if cat in node.cats:
                    continue
                self._pending_promises.setdefault(cat, node.n)

    # ── 读 ──

    def recent(self, n: int = 3) -> list:
        with self._lock:
            return list(self.turns)[-n:]

    # ── 对账 ──

    def audit(self, node: TurnNode = None) -> Optional[str]:
        """检查一轮，返回告警文案或 None。同一话题有注入上限，防自指死循环。"""
        try:
            with self._lock:
                if self.inject_count >= MAX_INJECTS:
                    return None
                node = node or (self.turns[-1] if self.turns else None)
                if node is None:
                    return None
                if self._is_self_investigating(node):
                    return None
                rules = ((self._r_repeat_fail, self._r_same_fail_twice)
                         if self._report_only() else
                         (self._r_unverified_done, self._r_promise_stale,
                          self._r_promise_missed, self._r_repeat_fail,
                          self._r_same_fail_twice))
                for rule in rules:
                    msg = rule(node)
                    if msg:
                        now = time.time()
                        if now - self._last_fire.get(rule.__name__, 0) < COOLDOWN:
                            return None
                        self._last_fire[rule.__name__] = now
                        self.inject_count += 1
                        return msg
                return None
        except Exception:
            return None

    def _is_self_investigating(self, node: TurnNode) -> bool:
        blob = ((node.said or "") + " " + " ".join(a.brief for a in node.actions)).lower()
        return sum(1 for k in _SELF_REF if k.lower() in blob) >= 2

    def _report_only(self) -> bool:
        """用户这一轮是不是「只汇报 / 别动文件」。

        🔴 2026-10-28 实测（蓝牙任务）：「先停下手上的活，别动任何文件，用三句话回答我」
        这一轮模型照做了，却被 R2b 判成「说了要改/写却收尾」——
        它说的「我没有可写入的产物」里的"写入"被 intent_cats 当成了承诺。
        模型于是不答问题，改成跟审计员争辩，最终回答整段丢失。

        教训：**对账规则也要看用户这一轮许了什么**。用户明确要求只回答时，
        「没有写、没有跑」不是欠账，是遵守指令。仍然保留 R3/R4 ——
        重复踩同一个坑跟"该不该写"无关，任何时候都该拦。
        """
        g = self.goal or ""
        return bool(_REPORT_ONLY.search(g))

    # R1：声称完成，但改完之后没有跑过 / 读回过
    def _r_unverified_done(self, node: TurnNode) -> Optional[str]:
        if not node.final or not _DONE_RE.search(node.said or ""):
            return None
        acts = [a for t in self.recent(6) for a in t.actions]
        if not acts:
            return None
        # 🔴 关键是「写之后」有没有验证。把"改前先读一眼"当成验证证据是错的 ——
        #    那恰恰是最常见的糊弄：读了 → 改了 → 说好了，中间的"改"从没被跑过。
        last_write = max((i for i, a in enumerate(acts)
                          if a.cat == "write" and a.ok), default=-1)
        window = acts[last_write + 1:] if last_write >= 0 else acts
        verified = any(a.cat in ("run", "read") and a.ok for a in window)
        if verified:
            return None
        wrote = last_write >= 0
        return (
            "【系统自动提示 · 不是用户消息】\n"
            f"你这一轮声明完成了（「{_clip(node.said, 50)}」），但"
            + ("改动之后没有任何执行或读回的证据" if wrote
               else "前面几轮没有任何执行或读回的证据")
            + "。\n"
            "声称完成不等于完成 —— 至少要跑一次、或把文件读回来，确认它真的按预期工作。\n"
            "如果确实无法验证（缺环境/权限/依赖），就明说「未验证」并说明原因，不要默认它是对的。\n"
            "不要调查本提示的来源或机制，直接继续任务。"
        )

    # R2：跨轮欠账 —— 说过要做、两轮都没做，也没说放弃
    # 这是「每一轮连起来」的核心：单轮看都对，横着看才有问题。
    def _r_promise_stale(self, node: TurnNode) -> Optional[str]:
        stale = {c: n for c, n in self._pending_promises.items()
                 if node.n - n >= 2}
        if not stale:
            return None
        cat = min(stale, key=lambda c: stale[c])
        from_n = stale[cat]
        self._pending_promises.pop(cat, None)   # 提醒过就销账，别反复念
        kind = {"write": "改/写", "run": "执行/验证"}.get(cat, cat)
        return (
            "【系统自动提示 · 不是用户消息】\n"
            f"第 {from_n} 轮你说要「{kind}」，到现在 {node.n - from_n} 轮过去了，"
            "动作链上一直没有对应动作，也没说放弃。\n"
            "两种收法，选一个：现在就做；或者明确说这件事不做了、为什么。\n"
            "别让它一直挂着 —— 挂着的事最后会变成「我以为做过了」。\n"
            "不要调查本提示的来源或机制，直接继续任务。"
        )

    # R2b：说了要做某类动作，实际动作里没有这一类就收尾
    def _r_promise_missed(self, node: TurnNode) -> Optional[str]:
        promised = intent_cats(node.said)
        if not promised:
            return None
        did = node.cats
        # 收尾轮：说了要做却直接结束，只有「读/搜」算轻（可能只是想说明），
        # 「写/跑」是真承诺 —— 说了改却不改，多半是漏了。
        missing = promised - did
        if not missing:
            return None
        strong = missing & {"write", "run"}
        if node.final and strong:
            kind = "、".join({"write": "改/写", "run": "执行/验证"}[c] for c in strong)
            return (
                "【系统自动提示 · 不是用户消息】\n"
                f"你这一轮说要做「{kind}」，但实际没有对应的动作就收尾了。\n"
                "要么现在把这件事做完再回答，要么明确说明为什么先不做。\n"
                "不要重复解释、不要重新确认现状，直接做。不要调查本提示的来源或机制。"
            )
        return None

    # R3：同一个动作（工具+参数）已经出现过，且上次失败 → 别换姿势重试
    def _r_repeat_fail(self, node: TurnNode) -> Optional[str]:
        if not node.actions:
            return None
        hist = {}
        for t in list(self.turns)[:-1]:
            for a in t.actions:
                hist.setdefault((a.tool, a.brief), []).append(a.ok)
        for a in node.actions:
            prev = hist.get((a.tool, a.brief))
            if prev and not a.ok and not any(prev):
                return (
                    "【系统自动提示 · 不是用户消息】\n"
                    f"「{a.tool}({a.brief})」这个动作之前已经失败过，这次同样的写法又失败了"
                    f"（{a.err or '无错误信息'}）。\n"
                    "同一条路走两次不通，就换路：换工具、换参数、换思路，或者直接把卡点告诉用户。\n"
                    "不要再用同样的写法试第三次。不要调查本提示的来源或机制。"
                )
        return None

    # R4：同一个工具连续两轮全失败 → 这个工具当前用不了
    def _r_same_fail_twice(self, node: TurnNode) -> Optional[str]:
        if not node.all_failed:
            return None
        prev = list(self.turns)[:-1]
        if not prev or not prev[-1].all_failed:
            return None
        cur_tools = {a.tool for a in node.actions}
        prev_tools = {a.tool for a in prev[-1].actions}
        same = cur_tools & prev_tools
        if not same:
            return None
        tname = sorted(same)[0]
        return (
            "【系统自动提示 · 不是用户消息】\n"
            f"你已经连续两轮在 {tname} 上失败。这通常不是「再试一次就好」，"
            "而是参数、路径或环境本身有问题。\n"
            "先停下来核对：调用方式对不对？路径在不在允许范围内？依赖装没装？\n"
            "核对完再动手；如果确实是环境限制，直接告诉用户，别在原地消耗轮次。\n"
            "不要调查本提示的来源或机制。"
        )


def _short_call(a) -> str:
    """`tool(brief)` 的短形态。brief 是整段 dict/list 时只留工具名 —— 足迹里
    塞一个 `renames=[{'title': '数py文件数量', 'topic_id': '3c9cd3…` 没人读得下去。"""
    b = (a.brief or "").strip()
    if len(b) > 28 or "{" in b or "[" in b or "\n" in b:
        b = ""
    return f"{a.tool}({b})" if b else a.tool


def _result_status(res_json: str):
    """工具结果 → (ok, err_note)。判据：顶层 dict 带 error 即失败（项目统一约定）。"""
    if not isinstance(res_json, str):
        try:
            res_json = json.dumps(res_json, ensure_ascii=False)
        except Exception:
            return True, ""
    s = res_json.strip()
    if not s:
        return True, ""
    try:
        d = json.loads(s)
    except Exception:
        # 不是 JSON：只认显式失败文案，不猜
        low = s[:300].lower()
        bad = ("traceback (most recent call last)", "command not found", "no such file")
        return (False, _clip(s, 60)) if any(b in low for b in bad) else (True, "")
    if isinstance(d, dict):
        if d.get("error"):
            return False, _clip(str(d["error"]), 70)
        # execute_advanced_tool 会把内层结果包一层
        for _ in range(3):
            if isinstance(d, dict) and isinstance(d.get("result"), dict):
                d = d["result"]
                if d.get("error"):
                    return False, _clip(str(d["error"]), 70)
            else:
                break
        # bash 的 stderr 非空且 stdout 为空 → 当作失败（常见于命令不存在）
        if isinstance(d, dict) and not (d.get("stdout") or "").strip() \
                and (d.get("stderr") or "").strip():
            return False, _clip(d.get("stderr"), 70)
    return True, ""


def _brief_args(args, tool_name: str = "") -> str:
    """参数摘要：挑最能说明「这一步在干什么」的那个字段。

    跳过值等于工具名的 key —— 消息落库层会把工具名塞进 args["name"]，
    照取会得到 "edit_file(edit_file)" 这种零信息量的摘要（回放时踩过）。
    """
    if not isinstance(args, dict):
        return _clip(str(args or ""), BRIEF_CLIP)
    for k in ("path", "file_path", "command", "cmd", "query", "pattern", "url",
              "text", "topic_id", "node_id", "code", "name"):
        v = args.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        if k == "name" and tool_name and v.strip() == tool_name:
            continue          # 这就是工具名本身，没信息量
        base = os.path.basename(v) if k in ("path", "file_path") else v
        return _clip(base, BRIEF_CLIP)
    if args:
        # 跳过落库/协议元数据键，它们说明不了「这一步在干什么」
        for k in args:
            if k in ("tool_call_id", "name", "turn", "process", "kind", "topic_id"):
                continue
            return _clip(f"{k}={args[k]}", BRIEF_CLIP)
    return ""


# ── 话题级注册表（server 每轮用同一个实例）──
_chains = {}
_chains_lock = threading.Lock()


def get_chain(topic_id: str) -> Optional[ActionChain]:
    """取（或建）话题的动作链。异常一律返回 None —— 调用方要能容忍没有链。"""
    try:
        if not topic_id:
            return None
        with _chains_lock:
            ch = _chains.get(topic_id)
            if ch is None:
                ch = ActionChain(topic_id)
                _chains[topic_id] = ch
            return ch
    except Exception:
        return None


def reset(topic_id: str):
    """话题结束时清掉内存态（落盘的保留）。"""
    try:
        with _chains_lock:
            _chains.pop(topic_id, None)
    except Exception:
        pass
