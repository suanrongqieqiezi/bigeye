"""
思维链系统 — 独立于 DAG 的执行脚印记录
只追加不修改，单线时间线，AI 每步即时总结
"""

import json
import os
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

    def get_history(self, before_step_id: int, limit: int = 10) -> list[ThoughtStep]:
        """获取某步之前的历史 — 温层翻阅"""
        steps = self._read_all()
        result = [s for s in steps if s.step_id < before_step_id]
        return result[-limit:]

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

    def format_hot_layer(self, n: int = 5) -> str:
        """热层格式 — 注入提示词"""
        recent = self.get_recent(n)
        if not recent:
            return "[思维链] 尚无记录"
        lines = ["[思维链 — 最近执行足迹]"]
        for s in recent:
            lines.append(f"  {s.step_id}. [{s.action_type}] {s.summary}")
            if s.next_suggestion:
                lines.append(f"     → 下一步: {s.next_suggestion}")
        return "\n".join(lines)

    def format_warm_layer(self, before_step_id: int, limit: int = 20) -> str:
        """温层格式 — 可翻阅"""
        history = self.get_history(before_step_id, limit)
        if not history:
            return "[思维链] 无更早记录"
        lines = ["[思维链 — 历史足迹（折叠区域）]"]
        for s in history:
            lines.append(f"  {s.step_id}. [{s.action_type}] {s.summary} | 动机: {s.motivation}")
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
