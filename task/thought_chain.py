"""
思维链系统 — 独立于 DAG 的执行脚印记录
只追加不修改，单线时间线，AI 每步即时总结
"""

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── 数据结构（类型先行） ──

@dataclass
class ThoughtStep:
    """单个思维步 — 不可变，只追加"""
    step_id: int
    timestamp: float
    action_type: str          # tool_call | decision | reflection | fix | plan | checkpoint
    summary: str              # 一行摘要，热层显示
    motivation: str           # 为什么做这一步（因果链）
    result: str               # 结果简述
    next_suggestion: str      # 下一步建议（可为空）
    detail: str = ""          # 详细内容，温层翻阅

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False) + "\n"

    @classmethod
    def from_line(cls, line: str) -> "ThoughtStep":
        d = json.loads(line.strip())
        return cls(**d)


# ── 存储层 ──

class ThoughtChain:
    """思维链管理器 — 只追加不修改，单线时间线"""

    def __init__(self, task_id: str, data_dir: str = "data/thought_chains"):
        self.task_id = task_id
        self.data_dir = data_dir
        self._file_path: Optional[str] = None
        self._next_id: int = 1
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    @property
    def file_path(self) -> str:
        if self._file_path is None:
            self._file_path = os.path.join(self.data_dir, f"{self.task_id}.jsonl")
            self._sync_next_id()
        return self._file_path

    def _sync_next_id(self):
        """从已有文件恢复 next_id"""
        if os.path.exists(self.file_path):
            with open(self.file_path, "r", encoding="utf-8") as f:
                last_id = 0
                for line in f:
                    if line.strip():
                        try:
                            step = ThoughtStep.from_line(line)
                            last_id = step.step_id
                        except json.JSONDecodeError:
                            pass
                self._next_id = last_id + 1

    # ── 写（只追加） ──

    def append(self, action_type: str, summary: str, motivation: str,
               result: str, next_suggestion: str = "", detail: str = "") -> ThoughtStep:
        """追加一步。不可修改已有步。"""
        _ = self.file_path  # triggers _sync_next_id before we use _next_id
        step = ThoughtStep(
            step_id=self._next_id,
            timestamp=time.time(),
            action_type=action_type,
            summary=summary,
            motivation=motivation,
            result=result,
            next_suggestion=next_suggestion,
            detail=detail,
        )
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(step.to_line())
        self._next_id += 1
        return step

    # ── 读（热层 + 温层） ──

    def get_recent(self, n: int = 5) -> list[ThoughtStep]:
        """获取最近 N 步 — 热层显示"""
        steps = self._read_all()
        return steps[-n:] if len(steps) > n else steps

    def get_history(self, before_step_id: int = 0, limit: int = 10) -> list[ThoughtStep]:
        """获取某步之前的历史 — 温层翻阅。

        before_step_id=0（或不传）表示「从最新往回翻」：取最后 limit 步。
        这个默认值是必须的 —— 模型手里只有热层里那几个 step_id，
        让它先猜一个再往回翻，等于把「翻旧账」变成一次试错。
        """
        steps = self._read_all()
        if before_step_id:
            steps = [s for s in steps if s.step_id < before_step_id]
        return steps[-limit:] if limit and len(steps) > limit else steps

    def get_all(self) -> list[ThoughtStep]:
        return self._read_all()

    def _read_all(self) -> list[ThoughtStep]:
        if not os.path.exists(self.file_path):
            return []
        steps = []
        with open(self.file_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        steps.append(ThoughtStep.from_line(line))
                    except json.JSONDecodeError:
                        pass
        return steps

    # ── 格式化输出 ──

    def format_hot_layer(self, n: int = 6, max_chars: int = 620) -> str:
        """热层 — 每轮注入 system prompt 的「我走到哪了」。

        一条足迹 = 这一步得出的判断（summary）+ 它打算接着做什么（next_suggestion）。
        这两半合起来才叫连续：只有判断是「我想到过什么」，加上下一步才是「我正在做一件事」。

        失败单独印一行，因为它是唯一**看回来不到**的信息：5 步以前的 tool 结果
        早就滚出窗口了，而「这个坑我踩过」必须活得更久。
        成功动作不印 —— 它原样躺在上一轮的 tool 消息里，再印一遍是纯重复。
        """
        # 先多取几倍再滤掉「和机制纠缠」的步，否则 n=6 里有 4 步是辩解
        recent = [s for s in self.get_recent(n * 4)
                  if not _meta_step(s.summary)][-n:]
        if not recent:
            return "[思维链] 尚无记录"
        total = self.step_count
        fails = sum(1 for s in recent if _fail_lines(s.result))
        head = "[思维链 — 我走到哪了]"
        meta = "最近 %d 步" % len(recent)
        if total > len(recent):
            meta += " / 共 %d 步" % total
        if fails:
            meta += " · %d 步有失败" % fails
        head += " " + meta
        # 预算内取最新的几步：从最近往前收，超了就把最旧的丢掉
        rows, used = [], len(head) + 1
        for s in reversed(recent):
            r = [f"  {s.step_id}. {s.summary}"]
            if s.next_suggestion:
                r.append(f"     → {s.next_suggestion}")
            for f in _fail_lines(s.result):
                r.append(f"     ✗ {f}")
            blk = sum(len(x) + 1 for x in r)
            if rows and used + blk > max_chars:
                break
            rows.append(r)
            used += blk
        lines = [head]
        for r in reversed(rows):
            lines.extend(r)
        return "\n".join(lines)

    def format_warm_layer(self, before_step_id: int = 0, limit: int = 20,
                          max_chars: int = 3600) -> str:
        """温层 — 往回翻。热层只看得到最近几步，更早的只能从这里取回。"""
        steps = [s for s in self.get_history(before_step_id, limit) 
                 if not _meta_step(s.summary)]
        if not steps:
            return "[思维链] 无更早记录"
        lines = ["[思维链 — 往回翻 · 第 %d~%d 步 / 共 %d 步]" % (
            steps[0].step_id, steps[-1].step_id, self.step_count)]
        used = len(lines[0]) + 1
        for i, s in enumerate(steps):
            r = [f"  {s.step_id}. [{s.action_type}] {s.summary}"]
            if s.next_suggestion:
                r.append(f"     → {s.next_suggestion}")
            for f in _fail_lines(s.result):
                r.append(f"     ✗ {f}")
            if s.detail:
                r.append(f"     · {clip(s.detail, 200)}")
            blk = sum(len(x) + 1 for x in r)
            if used + blk > max_chars:
                rest = len(steps) - i
                lines.append(f"  …还有 {rest} 步没展开，用更小的 before_step_id 往前翻")
                break
            lines.extend(r)
            used += blk
        return "\n".join(lines)

    @property
    def last_step(self) -> Optional[ThoughtStep]:
        steps = self._read_all()
        return steps[-1] if steps else None

    @property
    def step_count(self) -> int:
        if not os.path.exists(self.file_path):
            return 0
        return sum(1 for _ in open(self.file_path, "r", encoding="utf-8"))

    # ── 自动沉淀：从模型原生 reasoning 蒸馏思维步（纯规则，零额外 token）──

    def append_from_reasoning(self, reasoning: str, turn: int = 0,
                              tool_names: Optional[list] = None,
                              min_chars: int = 40,
                              narrative: str = "") -> Optional[ThoughtStep]:
        """把一轮的「思考」蒸馏成一条思维步。

        两个数据源，**旁白优先、reasoning 兜底**：
          - narrative：工具轮的 content，模型自己说给用户听的那句话（已落库，白拿）
          - reasoning：原生 thinking，模型写给自己看的草稿

        为什么要有这条路：`append()` 依赖 AI **主动**调 append_thought_step 写日记，
        实测一个月只落了 20 步（全是手工测试），过程泡摘要行因此长期为空。
        而模型每轮本来就在想（同月 4836/8635 条 ai 消息带 thinking，均值 1514 字），
        这份 reasoning 想完就扔、下一轮从零开始。这里把它接住：
        不调模型、不发请求、只做字符串处理，压缩后在 1ms 内落一条。

        只追加不修改的约束不变；同一内容连续出现时直接跳过（返回 None）。
        """
        d, kind = None, "reasoning"
        if narrative and narrative.strip():
            d = distill_narrative(narrative)
            if d:
                kind = "narrative"
        if d is None:
            d = distill_reasoning(reasoning, min_chars=min_chars)
        if not d:
            return None
        # 去重：与上一步摘要相同（同一轮被重复调用 / 模型原地打转）就不再写
        try:
            last = self.last_step
        except Exception:
            last = None
        if last is not None and last.summary == d["summary"]:
            return None
        detail = ""
        if tool_names:
            detail = "本轮工具: " + ", ".join(str(t) for t in tool_names)
        if turn:
            detail = ("turn %s | " % turn) + detail
        if d.get("turns"):
            detail = (detail + "\n" if detail else "") + "转折: " + "；".join(d["turns"])
        return self.append(kind, d["summary"], d["motivation"],
                           d["result"], d.get("next", ""), detail)


# ── 蒸馏规则（模块级，便于单测与复用）──────────────────────────────
# 设计要点：
# 1) **头尾窗口**，不是纯头部 —— reasoning 的结论在尾部。踩过：只取前 4000 字，
#    一条 40 万字符的 thinking 摘要落成「注意 Windows，Git Bash 没问题」这种噪声。
# 2) 摘要优先级 结论 > 转折 > 意图 —— 过程泡那一行要的是「我现在想到哪了」。
# 3) 句子要过「信息量」筛：纯语气词（好的/嗯/让我/继续）不算一步。

_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")
MARK_INTENT = ("用户", "需要", "我得", "先", "目标是", "任务是", "需求", "要解决")
MARK_TURN = ("但是", "不过", "然而", "其实", "问题在于", "根因", "怀疑", "等等",
             "不对", "漏了", "关键是", "真正", "注意", "可是", "反而", "风险")
MARK_DONE = ("所以", "因此", "结论", "确认了", "搞定", "总结", "确定", "最终", "这样就")
NOISE = ("好的", "嗯", "让我", "继续", "现在", "ok", "okay", "那么", "接下来",
         "让我想", "想想", "先这样", "等等让我")


def _sents(text: str, head: int = 1200, tail: int = 2200,
           drop_first_if_cut: bool = False) -> list:
    """头尾窗口切句。中间段丢弃（reasoning 中段多是重复推演）。

    drop_first_if_cut=True 时，窗口截断会丢掉**第一句**：它是从半句话中间切出来的，
    现象是摘要以「），然后改任务名…」这种结尾括号开头（2026-10-28 真机实测）。
    旁白路径必须开这个 —— 一条足迹第一句就读不通，整行就废了。
    """
    t = (text or "").strip()
    if not t:
        return []
    cut = 0
    if len(t) > head + tail:
        t = t[:head] + "\n" + t[-tail:]
        cut = 1
    out = []
    for seg in _SENT_SPLIT.split(t):
        s = re.sub(r"\s+", " ", seg).strip()
        if len(s) >= 6:
            out.append(s)
    if cut and drop_first_if_cut:
        out = out[1:]
    return out


def _informative(s: str) -> bool:
    low = s.lower()
    if any(low.startswith(n) or low == n for n in NOISE):
        return False
    if len(s) < 10:
        return False
    # 以收尾符开头的句子一定是被切断的残片：「），然后改任务名…」「)。」
    if _LEAD_CLOSE.match(s):
        return False
    # 重复填充（xxxx… / ---- / 哈哈哈）不是思维，别让它在摘要里冒充结论
    if len(set(s)) <= max(3, len(s) // 6):
        return False
    # 有具体对象（数字 / 技术符号 / 驼峰 / 引号内容）才算有信息量
    return bool(re.search(r"[0-9_`/\\()\[\]<>#.]|[A-Za-z]{3,}", s)) or len(s) >= 18


# 「我要去做X」≠「我想到什么」。这类自我催促句信息量为零：
#   实测 40 万字符的 thinking，尾部窗口挑出的却是 "Time to be efficient. Let me grep."
#   又实测一轮真实任务，摘要落成「不过为了不搞复杂，我可以先执行命令…，顺手改个任务名」
#   —— 那是动作叙述，不是思维。
ACTION_CHATTER = ("let me", "let's", "time to", "i'll", "i will", "i need to", "i can",
                  "now let", "now fix", "now add", "okay", "next,", "alright",
                  "让我", "现在去", "现在写", "先去", "继续做", "接着做", "好，那", "那我去",
                  "我可以", "我来", "我打算", "我要去", "我准备")
# 纯执行动作（跑命令/改文件）也不该占据摘要行
ACTION_VERB = ("执行命令", "跑一次", "再跑", "改成", "改个", "写入", "保存", "重跑")
# 句中出现的「我要去做X」—— 不在句首也要认（"不过为了不啰嗦，我可以先执行 bash…"）
CHATTER_SUB = ("let me", "i'll ", "i will ", "i need to", "i can ",
               "我可以", "我来", "我打算", "我要去", "我准备", "顺手", "不啰嗦", "不搞复杂")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 内部记账句：模型在「还没开始干活」的那一刻，reasoning 里常是改任务名、找工具、
# 记锚点这类自我管理，不是对用户任务的判断。实测（2026-10-28 端到端）第一步因此
# 落成了 `The rule suggests "rename task". Let me discover int…` —— 走错片场。
_META = ("rename task", "rename the task", "task brief", "discover tool",
         "execute_advanced", "system prompt", "tool schema", "the rule",
         "改任务名", "更新任务锚点", "重命名任务")

# 整步过滤：这种步讲的是「我和机制的关系」，不是「任务怎么了」。
# 实测（蓝牙任务 2026-10-28）：对账告警之后，模型连着两步在解释「这轮是汇报不是执行」，
# 被蒸馏成摘要留在链上 —— 链是注回 system 的，下一个自己读到的是一段辩解。
# 写入侧（ActionChain._to_thought）已经拦了新的；这里再兜一层，老链里的也不显示。
_META_STEP = ("动作链", "对账", "账本机制", "系统自动提示", "审计", "action_chain",
              "audit", "这轮的任务是", "未验证但无需验证", "提示的来源")


def _meta_step(s: str) -> bool:
    low = (s or "").lower()
    return any(k.lower() in low for k in _META_STEP)


def _is_cjk(s: str) -> bool:
    return bool(_CJK.search(s))


def _is_meta(s: str) -> bool:
    """这句是在讲「我自己怎么记账/怎么找工具」，而不是「任务本身怎么了」。

    只罚纯内部记账：句子里同时有「根因/发现/结论」这类实内容词的不算 ——
    「更新任务锚点，因为根因已经找到」是有效判断，不是噪声。
    """
    low = (s or "").lower()
    if not any(k in low for k in _META):
        return False
    return not any(k in s for k in MARK_TURN + MARK_DONE)


def _fail_lines(result: str) -> list:
    """从 result 里挑出失败那一截。

    新步的 result 只存失败项；老步（2026-10-28 之前）存的是整串动作，
    成功项混在里面只能靠 ✗ 前缀过滤 —— 成功动作原样躺在上一轮的 tool 消息里。
    """
    out = []
    for seg in (result or "").split(" / "):
        seg = seg.strip()
        if not seg.startswith("✗"):
            continue
        seg = seg[1:].strip()
        if seg:
            out.append(seg if len(seg) <= 90 else seg[:89] + "…")
    return out


# ── 「下一步」抽取 ────────────────────────────────────────────────
# 模型一直在说「接下来我要 X」，只是这些话一直被当成噪声罚掉：
#   _score() 里 `ACTION_CHATTER` 命中 -4（"let me" "我来" "接下来"），
#   因为**当摘要**不合格。但**当下一步**恰恰合格 —— 摘要是结论，next 是打算。
# 实测 3000 条真实工具轮：47% 能直接抽出，零额外请求、零额外 token。
NEXT_LEAD = ("接下来", "下一步", "然后", "接着", "现在", "先", "再", "最后", "之后",
             "let me", "let's", "next", "then", "now", "i'll", "i will", "time to",
             "让我", "我来", "我打算", "我要", "我准备", "我这就")
# 句首动词 = 无主语的祈使句。中文里「下一步」最普遍的说法：
#   「重写：relay 记住 agent 连接」「给 relay 加异常保护，再重跑」
# 只认路 1（将来时标记）会漏掉一整类 —— 实测那正是缺口的大头。
IMPERATIVE_HEAD = ("改", "写", "加", "重写", "给", "把", "跑", "验证", "补", "删",
                   "换", "看", "查", "读", "试", "建", "创建", "生成", "修", "挪",
                   "拆", "合并", "顺手", "继续", "处理", "实现", "补上", "补个",
                   "fix", "add", "write", "check", "verify", "run", "read", "try",
                   "update", "rewrite", "make", "create")
# 结果/结论词：带这些的句子说的是「已发生」，不是「将要做」
NEXT_DONE = ("了。", "好了", "完成", "搞定", "成功", "失败", "报错", "发现", "确认了",
             "证明了", "看到了", "返回", "输出", "所以", "因此", "结论",
             "已经", "刚才", "前面", "之前", "跑了", "看了", "读到了")
# 句首有主语 ⇒ 不是祈使句。「用户确认了…」是陈述，不是下一步。
# 🔴 单字动词（用/看/查）必须靠「无主语」兜底：第一版把「用户确认了电池容量」
#    判成了「用…」开头的祈使句。
_SUBJ_HEAD = ("用户", "他", "她", "它", "这", "那", "我", "你", "我们", "他们",
              "the ", "this ", "there ", "it ", "they ", "we ", "you ")


def is_next(s: str) -> bool:
    """这句话是在说「接下来要做什么」吗？"""
    low = (s or "").lower().strip()
    if len(low) < 6:
        return False
    if _LEAD_CLOSE.match(s):
        return False
    body = low.lstrip("—·- 　")
    has_subj = body.startswith(_SUBJ_HEAD)
    lead = any(n in body[:6] for n in NEXT_LEAD) or \
        any(body.startswith(n) for n in NEXT_LEAD)
    if not lead and not has_subj:
        lead = any(body.startswith(v) for v in IMPERATIVE_HEAD)
    if not lead:
        return False
    return sum(1 for k in NEXT_DONE if k in s) < 2


def _pick_next(pool: list, summary: str = "") -> str:
    """从同一份句池里取「下一步」。

    与摘要**互补取样**，不是另开一条路：同一句既当摘要又当下一步是纯浪费。
    取最后一句命中的 —— 一段旁白/思考的收尾通常就是它的打算。
    """
    if not pool:
        return ""
    head = (summary or "").strip()[:16]
    hits = []
    for s in pool:
        if not is_next(s):
            continue
        if head and (s.strip()[:16] == head or head in s):
            continue          # 这就是摘要本身，别印两遍
        hits.append(s)
    # `check_edr_is_disconnct() 的函数名与实现相反` 这种以标识符开头的句子是
    # 「引用了一个符号」，不是打算 —— 它是靠 IMPERATIVE_HEAD 里的 check 混进来的。
    # 先过滤再挑，否则一个符号引用会把真正那句打算顶掉。
    hits = [s for s in hits
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s*\(", s)]
    if not hits:
        return ""
    zh = [s for s in hits if _is_cjk(s)]
    pick = (zh or hits)[-1]
    if not _is_cjk(pick) and len(pick.split()) < 3:
        return ""             # 纯函数名/代码片段不是「下一步」
    return _fit(pick, 60)


def _same(a: str, b: str) -> bool:
    """两句说的是不是同一件事（同一句被摘要和下一步抢）。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    return a[:16] == b[:16] or a in b or b in a


def _next_pool(good: list, ss: list) -> list:
    """下一步的候选池 = 可用句 + 短句里明确是打算的。

    `_usable_narration` 卡 8 字，「再跑一次。」这种 5 字的真打算会被丢掉；
    但放全部短句进来又会混进「好了。」，所以短句必须自己过一遍 is_next。
    """
    out = list(good)
    for s in ss:
        if s not in out and len(s) >= 6 and is_next(s):
            out.append(s)
    return out


def _split_plan(s: str):
    """把「X；下一步 Y」拆成 (判断, 打算)。拆不开返回 (s, "")。

    实测（2026-10-28 长任务）模型的旁白绝大多数是这个形状：
        「①我知道了：文件不存在（…）；下一步记下原因并做②建 mz_c.py。」
    整句当摘要是对的，但那样链上就看不到 → 行，而「下一步」正是要显示的那一半。
    同一句话里既然已经写好了，就不要指望它再写一遍。
    """
    m = _PLAN_SPLIT.search(s or "")
    if not m:
        return s, ""
    head, plan = s[:m.start()].strip(), s[m.end():].strip()
    if len(head) < 8 or len(plan) < 6:
        return s, ""      # 拆完两边都不成句，不如不拆
    return head, plan


def _score(s: str, idx: int, n: int) -> float:
    """句子像不像「一条思维」的分。idx/n 越靠后越像结论。"""
    low = s.lower()
    v = 0.0
    if any(low.startswith(a) for a in ACTION_CHATTER):
        v -= 4
    elif any(a in s for a in CHATTER_SUB):            # 句中夹的 "…我可以先执行…" 也降权
        v -= 3
    if any(a in s for a in ACTION_VERB):
        v -= 1
    if any(k in s for k in MARK_DONE):
        v += 3
    if any(k in s for k in MARK_TURN):
        v += 2
    if any(k in s for k in MARK_INTENT):
        v += 1
    if _is_cjk(s):
        v += 2          # 中文用户看中文摘要；纯英文句降权
    if 14 <= len(s) <= 60:
        v += 1
    if len(s) > 90:
        v -= 1
    if re.search(r"[：:]", s):
        v += 1          # "根因是：…" 这种判断句
    v += 1.5 * (idx / max(1, n - 1))   # 越靠后越可能是结论
    return v


def distill_reasoning(reasoning: str, min_chars: int = 40,
                      summary_max: int = 58) -> Optional[dict]:
    """reasoning → {summary, motivation, result, turns, next}；太短/无内容返回 None。"""
    t = (reasoning or "").strip()
    if len(t) < min_chars:
        return None
    ss = _sents(t)
    if not ss:
        return None
    # 一句有信息量的都没有 = 这段 reasoning 里没有可留的东西（重复填充/纯语气词），
    # 不要 fallback 回去硬挑一句 —— 那样会把 "xxxxxxxx…" 这种当结论写进链里。
    good = [s for s in ss if _informative(s)]
    good = [s for s in good if not _is_meta(s)]
    if not good:
        return None
    n = len(good)

    # 摘要句 = 打分最高的那句（同分取靠后的）
    ranked = sorted(range(n), key=lambda i: (_score(good[i], i, n), i))
    best = good[ranked[-1]] if ranked else good[-1]
    # 「下一步」先取：一句话既像结论又像打算时，**打算让给 next，结论留给 summary**。
    # 实测反例（2026-10-28）：「注册通了，是 mock relay 崩了收不到结果。重写：relay
    # 记住 agent 连接…」—— 打分把「重写：…」推上了摘要位，于是结论丢了、next 也空了。
    nxt = _pick_next(_next_pool(good, ss))
    if nxt and _same(best, nxt):
        alt = [good[i] for i in reversed(ranked) if not _same(good[i], nxt)]
        if alt:
            best = alt[0]
    head, plan = _split_plan(best)
    if plan:
        best, nxt = head, (nxt or plan)

    def first(marks, pool):
        return next((s for s in pool if any(k in s for k in marks)), None)

    def last(marks, pool):
        return next((s for s in reversed(pool) if any(k in s for k in marks)), None)

    intent = first(MARK_INTENT, good[:6]) or good[0]
    concl = last(MARK_DONE, good[-6:]) or best
    turns = [clip(s, 60) for s in [g for g in good if any(k in g for k in MARK_TURN)][-2:]]
    summ = clip(best, summary_max)
    # motivation 与摘要撞车时退回首句（两句一样等于少一个信息维度）
    motiv = clip(intent, 80)
    if motiv == summ and len(good) > 1:
        motiv = clip(good[0], 80)
    if nxt and _same(summ, nxt):
        nxt = ""            # 摘要已经说了打算，不用再印一遍

    return {
        "summary": summ,
        "motivation": motiv,
        "result": clip(concl, 80),
        "turns": turns,
        "next": nxt,
    }



# ── 旁白蒸馏：工具轮 content 是模型**写给人看的**，质量比 reasoning 高一档 ──
# 实测（Temp/_zz_narr_vs_think.py，chat.db 里 12.5 万条工具轮叙述）同一轮两个来源并排：
#   #303412  narrative: "看清楚了：miaow55 的参数名（text_lang/ref_audio_path…）就是
#                         api_v2 的 /tts 接口格式，只是 URL 指到了根路径。v1 的 api.py
#                         参数名完全不同…正确修法是把 audio_system.py 的 URL 指到 /tts"
#            distill  : "1. Edit audio_system.py line 23: SV4_API_URL = …"（执行清单，无「为什么」）
#   #303409  narrative: "注意：参数名是 v2 风格，但 URL 打在根路径 `/`。看 audio_system.py 怎么拼"
#            distill  : `" + params`，所以路径是 `/``（代码片段碎片，不可读）
#   #303400  narrative: "我在修语音 404 的路上，现在落地：换 v1 接口。"
#            distill  : "但注意：v1 api.py 需要指定模型权重吗？"（悬空疑问）
# 差别很朴素：reasoning 是写给自己看的草稿（自我催促、推演、碎片），
# content 是写给别人看的结论（发现 + 判断 + 下一步）。
# 覆盖率不是 100%（63% 的工具轮模型直接调工具、一句话不说）⇒ 只做「优先」，不做「替换」。
_MD_TOKEN = re.compile(
    r"\*\*|__|~~|`{1,3}|^\s{0,3}#{1,6}\s+|^\s{0,3}[-*+]\s+|^\s{0,3}\d+[.)]\s+", re.M)
# 「判断；下一步 Y」的接缝 —— 拆开它，摘要只说判断、打算进 next
_PLAN_SPLIT = re.compile(
    r"[；;，,。]\s*(?:下一步|接下来|然后|接着|之后就|下一步要)\s*[：:]?\s*")
# 引导句：「两个发现：」「注意：」「结论如下：」—— 有冒号没内容
_LEAD_IN = re.compile(r"^[^。！？\n]{0,12}[：:]\s*$")
# 以收尾符开头 = 被窗口切断的残片，不是句子（「），然后改任务名…」）
_LEAD_CLOSE = re.compile(r"^\s*[）)】」》\]]")
# 具体对象：路径 / 文件名 / 数字 / 标识符 —— 有它才算「说到了点上」
_CONCRETE = re.compile(r"[0-9]|\.[a-z]{2,4}\b|[A-Za-z_][A-Za-z0-9_]{2,}|[`/\\]")
# 「发现」类词：旁白里出现这些说明模型拿到了事实，而不是在描述动作
_FINDING = re.compile(r"不收|不支持|不能|无法|失败|报错|错误|404|500|卡|冲突|不一致|"
                      r"必须|其实|只是|只有|根因|问题在|说明|发现")
# 祈使动作句：「看…」「先…」「等几秒…」—— 旁白末尾多是这个，不是结论
_IMPERATIVE = re.compile(r"^(看|先|再|等|试|查|跑|改|写|接着|然后|用|把|verify|check|try|run)",
                         re.I)


def _score_narration(s: str, idx: int, n: int) -> float:
    """旁白句子打分。

    与 _score（reasoning 用）的两个关键差别，都是实测踩出来的：
    1. **旁白结论在前，reasoning 结论在后** —— 所以这里给首句加权，方向相反。
       照抄 reasoning 的尾部加权会让摘要全部落在「看 api.py 的路由」「先写启动脚本」这类
       下一步动作上（Temp/_zz_narr_check.py 第一版就是这样）。
    2. 旁白里的动作句是**有价值**的（说明往哪走），只对句首祈使句轻罚，
       不像 reasoning 那样把 "let me / 我来" 重罚 −4。
    """
    v = 0.0
    if _LEAD_IN.match(s):
        v -= 5                       # 「两个发现：」这类纯引导句
    if len(s) < 14:
        v -= 1                       # 「POST 拿到音频了」信息太薄
    if any(k in s for k in MARK_DONE):
        v += 3
    if any(k in s for k in MARK_TURN):
        v += 2
    if _FINDING.search(s):
        v += 2
    if _is_cjk(s):
        v += 1
    if _CONCRETE.search(s):
        v += 2
    if 14 <= len(s) <= 80:
        v += 1
    if len(s) > 140:
        v -= 1
    if re.search(r"[：:]", s):
        v += 1.5                     # 「看清楚了：…」「现在落地：…」= 结论+解释结构
    # 句首祈使才算「下一步动作」；带冒号的是「结论：解释」结构，不是祈使
    # （「看清楚了：miaow55 的参数名…」以「看」开头但是陈述句，罚了就丢掉整条结论）
    if _IMPERATIVE.match(s) and not re.search(r"[：:]", s):
        v -= 2
    v += 0.9 * (1.0 - idx / max(1, n - 1))   # 首句加权（与 reasoning 相反）
    return v


def _fit(s: str, maxn: int) -> str:
    """在逗号/顿号处截断，尽量保住完整语义单元。"""
    s = (s or "").strip()
    if len(s) <= maxn:
        return s.rstrip("，、；,;：: ")
    # 超限时先剔掉括号段。括号里多是补充细节（参数名清单 / 行号 / 文件名），
    # 留着它会把核心结论挤掉：「看清楚了：miaow55 发的参数名（text_lang / ref_audio_path /
    # prompt_lang / streaming_mode）就是 api_v2 的 /tts 接口格式」—— 截 72 字全是括号内容。
    t = re.sub(r"[（(][^（()）]{1,80}[）)]", " ", s)
    t = re.sub(r"\s{2,}", " ", t).replace(" ，", "，").strip()
    if len(t) >= 20:
        s = t
        if len(s) <= maxn:
            return s.rstrip("，、；,;：: ")
    out = ""
    for p in re.split(r"(?<=[，、；,;])", s):
        if len(out + p) > maxn:
            break
        out += p
    out = out.rstrip("，、；,;：: ")
    if len(out) >= 12:
        return out + "…"
    cut = s[:maxn - 1].rstrip("，、；,;：: ")
    # 硬截时若落在括号/反引号内部，退到开括号之前 —— 截一半的 "(text_lang、ref_audio" 读起来是坏的
    for o, c in (("（", "）"), ("(", ")"), ("`", "`"), ("【", "】")):
        if cut.count(o) > cut.count(c):
            p = cut.rfind(o)
            if p >= 18:
                cut = cut[:p].rstrip("，、；,;：: ")
            break
    return cut + "…"


def _usable_narration(s: str) -> bool:
    """旁白句子可用性。与 _informative 的差别：旁白可以短（"TTS 打通了"），
    但不能是重复填充 / 纯符号 —— 那两类一旦 fallback 进链，摘要行就成了 xxxxx…"""
    if not s or len(s) < 8:
        return False
    if _LEAD_CLOSE.match(s):
        return False          # 窗口截断的残片，读不起来
    if len(set(s)) <= max(3, len(s) // 6):
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", s))


def distill_narrative(narrative: str, summary_max: int = 72,
                      min_chars: int = 10) -> Optional[dict]:
    """工具轮旁白 → {summary, motivation, result, turns, next}；无可用句返回 None。

    与 distill_reasoning 的区别：narrative 本身就是人话且通常只有 1~3 句，
    不需要头尾窗口，也不要「转折」这种 reasoning 专用维度。
    """
    t = (narrative or "").strip()
    if len(t) < min_chars:
        return None
    t = _MD_TOKEN.sub(" ", t)
    # 剥掉 markdown 后中英之间会留空格："发的是 GET ，" → "发的是 GET，"
    t = re.sub(r"\s+([，。！？、；：）】」》)])", r"\1", t)
    t = re.sub(r"([（【「《(])\s+", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    # 剥掉 markdown 会在中文字之间留空格（"参数名 **就是** 格式" → "参数名 就是 格式"），
    # 中文之间不需要空格；中英之间要保留。
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    ss = [s for s in _sents(t, drop_first_if_cut=True) if len(s) >= 8]
    # 一句能用的都没有 = 这段"旁白"其实是噪声（重复填充 / 纯符号），不要 fallback 硬挑
    good = [s for s in ss if _usable_narration(s)]
    if not good:
        return None
    n = len(good)
    ranked = sorted(range(n), key=lambda i: (_score_narration(good[i], i, n), i))
    best = good[ranked[-1]]
    nxt = _pick_next(_next_pool(good, ss))
    if nxt and _same(best, nxt):
        alt = [good[i] for i in reversed(ranked) if not _same(good[i], nxt)]
        if alt:
            best = alt[0]
    head, plan = _split_plan(best)
    if plan:
        best, nxt = head, (nxt or plan)
    summ = _fit(best, summary_max)
    if nxt and _same(summ, nxt):
        nxt = ""
    return {
        "summary": summ,
        "motivation": _fit(good[0], 80),
        "result": _fit(good[-1], 80),
        "turns": [],
        "next": nxt,
    }


def clip(s: str, n: int) -> str:
    s = (s or "").strip(" ：:，,。;；-")
    return s if len(s) <= n else s[:n - 1] + "…"
