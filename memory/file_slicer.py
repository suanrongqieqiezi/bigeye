"""
FileSlicer — 文件切片器（CMN P1）

按文件类型选择切分策略：
- 代码文件（.py/.js/.ts/.java/.go/.rs）：按函数/类边界切
- 文档文件（.md/.txt/.rst）：按 ## 标题或空行段落切
- 其他：固定字符数 + 重叠

设计参考：[CMN实施方案.md] 第四章 P1
"""
import os
import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class Slice:
    """一个文件切片。"""
    index: int                  # 切片序号
    content: str                # 切片文本
    start: int                  # 起始字符偏移
    end: int                    # 结束字符偏移
    hash: str = ""              # 切片 SHA256（由 store 算）
    label: str = ""             # 切片标签（如函数名/章节标题）


# ── 默认参数 ───────────────────────────────────────────

DEFAULT_SLICE_SIZE = 2000       # 兜底固定切片大小
DEFAULT_OVERLAP = 200           # 重叠字符数
CODE_MAX_SLICE = 4000           # 代码单块超此阈值二次切
DOC_MIN_SLICE = 200             # 文档段落过短不单独成片


# ── 代码切分 ───────────────────────────────────────────

# 代码文件扩展名 → 语言标识
CODE_EXTS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
}

# 顶层定义正则（按语言）：class/def/function/func 等
# 简化版：匹配行首的 def/class/function/func/struct/enum/impl/public/private 等
_TOP_DEF_PATTERNS = {
    "python": re.compile(r"^(class\s+\w+|def\s+\w+|async\s+def\s+\w+)", re.MULTILINE),
    "javascript": re.compile(r"^(function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(async\s*)?\(|let\s+\w+\s*=\s*(async\s*)?\(|export\s+(default\s+)?(function|class))", re.MULTILINE),
    "typescript": re.compile(r"^(function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(async\s*)?\(|let\s+\w+\s*=\s*(async\s*)?\(|export\s+(default\s+)?(function|class|interface|type|enum))", re.MULTILINE),
    "java": re.compile(r"^(public|private|protected)?\s*(static\s+)?(class|interface|enum|void\s+\w+|\w+(<[^>]+>)?\s+\w+\s*\()", re.MULTILINE),
    "go": re.compile(r"^(func\s+\w+|type\s+\w+\s+struct|type\s+\w+\s+interface)", re.MULTILINE),
    "rust": re.compile(r"^(pub\s+)?(fn\s+\w+|struct\s+\w+|enum\s+\w+|trait\s+\w+|impl\s+)", re.MULTILINE),
    "c": re.compile(r"^(\w[\w\s\*]*\s+\w+\s*\([^;]*\)\s*\{|struct\s+\w+|typedef\s+struct)", re.MULTILINE),
    "cpp": re.compile(r"^(\w[\w\s\*:<>,]*\s+\w+\s*\([^;]*\)\s*\{|class\s+\w+|struct\s+\w+|namespace\s+\w+)", re.MULTILINE),
    "csharp": re.compile(r"^(public|private|protected)?\s*(static\s+)?(class\s+\w+|void\s+\w+|\w+\s+\w+\s*\()", re.MULTILINE),
    "ruby": re.compile(r"^(class\s+\w+|def\s+\w+|module\s+\w+)", re.MULTILINE),
    "php": re.compile(r"^(function\s+\w+|class\s+\w+)", re.MULTILINE),
    "swift": re.compile(r"^(func\s+\w+|class\s+\w+|struct\s+\w+|enum\s+\w+|protocol\s+\w+)", re.MULTILINE),
    "kotlin": re.compile(r"^(fun\s+\w+|class\s+\w+|object\s+\w+|interface\s+\w+|data\s+class\s+\w+)", re.MULTILINE),
    "scala": re.compile(r"^(def\s+\w+|class\s+\w+|object\s+\w+|trait\s+\w+|case\s+class\s+\w+)", re.MULTILINE),
    "shell": re.compile(r"^\s*(function\s+)?\w+\s*\(\)\s*\{", re.MULTILINE),
}


def _slice_code(content: str, lang: str) -> List[Slice]:
    """按顶层定义边界切分代码。"""
    pattern = _TOP_DEF_PATTERNS.get(lang)
    if not pattern:
        return _slice_generic(content)

    # 找所有顶层定义的位置
    matches = list(pattern.finditer(content))
    if not matches:
        # 无顶层定义，走兜底
        return _slice_generic(content)

    slices: List[Slice] = []
    n = len(content)

    # 文件头（imports/注释等，在第一个定义前）单独成片
    first_start = matches[0].start()
    if first_start > DOC_MIN_SLICE:
        head = content[:first_start].rstrip()
        if head.strip():
            slices.append(Slice(
                index=0, content=head, start=0, end=first_start,
                label=f"<头部 imports/注释>"
            ))

    # 每个顶层定义 → 下一个定义之前
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else n
        block = content[start:end].rstrip()
        if not block.strip():
            continue

        # 超长块二次切
        if len(block) > CODE_MAX_SLICE:
            sub_slices = _slice_generic(block, base_offset=start)
            for ss in sub_slices:
                ss.index = len(slices)
                ss.label = m.group(0).strip()
                slices.append(ss)
        else:
            # 提取函数/类名作标签
            label = m.group(0).strip().split("(")[0].split(":")[0][:60]
            slices.append(Slice(
                index=len(slices), content=block, start=start, end=end,
                label=label
            ))

    if not slices:
        return _slice_generic(content)
    return slices


# ── 文档切分 ───────────────────────────────────────────

DOC_EXTS = {".md", ".txt", ".rst", ".markdown"}

# Markdown/RST 标题正则
_HEADING_PATTERN = re.compile(r"^(#{1,6}\s+\S.+|={3,}\s*$|-{3,}\s*$|\*\s*\S)", re.MULTILINE)


def _slice_document(content: str) -> List[Slice]:
    """按 ## 标题或空行段落切分文档。"""
    # 先按标题切
    matches = list(_HEADING_PATTERN.finditer(content))
    if len(matches) >= 2:
        slices: List[Slice] = []
        # 标题前内容（如果有实质内容）
        if matches[0].start() > DOC_MIN_SLICE:
            head = content[:matches[0].start()].rstrip()
            if head.strip():
                slices.append(Slice(
                    index=0, content=head, start=0, end=matches[0].start(),
                    label="<文档头部>"
                ))
        # 每个标题 → 下一个标题
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            block = content[start:end].rstrip()
            if not block.strip():
                continue
            # 提取第一行作标签
            first_line = block.split("\n", 1)[0].strip().lstrip("#=*- ")[:60]
            slices.append(Slice(
                index=len(slices), content=block, start=start, end=end,
                label=first_line or f"<section {i}>"
            ))
        if slices:
            return slices

    # 无标题 → 按空行段落切
    paragraphs = re.split(r"\n\s*\n", content)
    if len(paragraphs) <= 1:
        return _slice_generic(content)

    slices: List[Slice] = []
    offset = 0
    for para in paragraphs:
        para_stripped = para
        # 找在原文中的位置
        idx = content.find(para, offset)
        if idx < 0:
            idx = offset
        start = idx
        end = idx + len(para)
        if len(para.strip()) < DOC_MIN_SLICE and slices:
            # 过短段落合并到上一个
            slices[-1].content += "\n\n" + para
            slices[-1].end = end
        else:
            slices.append(Slice(
                index=len(slices), content=para, start=start, end=end,
                label=para.strip().split("\n", 1)[0][:60]
            ))
        offset = end
    return slices


# ── 兜底切分 ───────────────────────────────────────────

def _slice_generic(content: str, size: int = DEFAULT_SLICE_SIZE,
                   overlap: int = DEFAULT_OVERLAP, base_offset: int = 0) -> List[Slice]:
    """固定字符数 + 重叠切分（兜底）。"""
    if not content:
        return []
    slices: List[Slice] = []
    n = len(content)
    i = 0
    idx = 0
    while i < n:
        end = min(i + size, n)
        block = content[i:end]
        slices.append(Slice(
            index=idx, content=block, start=base_offset + i, end=base_offset + end,
            label=f"<chunk {idx}>"
        ))
        idx += 1
        if end >= n:
            break
        i = end - overlap
        if i <= 0:
            i = end
    return slices


# ── 统一入口 ───────────────────────────────────────────

def get_slice_strategy(path: str) -> str:
    """根据扩展名返回切分策略名。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in CODE_EXTS:
        return "code"
    if ext in DOC_EXTS:
        return "doc"
    return "generic"


def slice_file(path: str) -> List[Slice]:
    """读取文件并切片。返回 Slice 列表。

    策略选择：
    - 代码文件（.py/.js/...）→ 按顶层定义边界
    - 文档文件（.md/.txt/...）→ 按标题/段落
    - 其他 → 固定字符数 + 重叠
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    return slice_content(content, path)


def slice_content(content: str, path: str = "") -> List[Slice]:
    """对内容字符串切片（不读文件）。"""
    if not content:
        return []

    ext = os.path.splitext(path)[1].lower() if path else ""
    if ext in CODE_EXTS:
        return _slice_code(content, CODE_EXTS[ext])
    if ext in DOC_EXTS:
        return _slice_document(content)
    return _slice_generic(content)
