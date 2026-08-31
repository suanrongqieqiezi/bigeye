#!/usr/bin/env python3
"""
Code analysis tools — AST-based code structure analysis.
Uses Python `ast` module for Python files, regex-based structure matching for others.
"""
import ast
import re
import os
import traceback
from tools.registry import register_tool

# ── Supported file types ────────────────────────────
_LANG_MAP = {
    '.py':  'python',
    '.js':  'javascript',
    '.ts':  'typescript',
    '.rs':  'rust',
    '.go':  'go',
    '.java': 'java',
    '.c':   'c',
    '.h':   'c',
    '.cpp': 'cpp',
    '.hpp': 'cpp',
}

# ── Regex patterns for structure matching ───────────
_PATTERNS = {
    'python': {
        'function': re.compile(r'^(\s*)def\s+(\w+)\s*\('),
        'class':    re.compile(r'^(\s*)class\s+(\w+)\s*[:\(]'),
        'async_fn': re.compile(r'^(\s*)async\s+def\s+(\w+)\s*\('),
    },
    'javascript': {
        'function': re.compile(r'^(\s*)(?:function\s+)?(\w+)\s*\([^)]*\)\s*\{?'),
        'class':    re.compile(r'^(\s*)class\s+(\w+)'),
    },
    'typescript': {
        'function': re.compile(r'^(\s*)(?:function\s+)?(\w+)\s*\([^)]*\)\s*:?\s*[^\{]*\{?'),
        'class':    re.compile(r'^(\s*)class\s+(\w+)'),
        'interface': re.compile(r'^(\s*)interface\s+(\w+)'),
        'type':     re.compile(r'^(\s*)type\s+(\w+)\s*='),
    },
    'rust': {
        'function': re.compile(r'^(\s*)(?:pub\s+)?(?:unsafe\s+)?fn\s+(\w+)\s*[<(]'),
        'struct':   re.compile(r'^(\s*)(?:pub\s+)?struct\s+(\w+)'),
        'enum':     re.compile(r'^(\s*)(?:pub\s+)?enum\s+(\w+)'),
        'trait':    re.compile(r'^(\s*)(?:pub\s+)?trait\s+(\w+)'),
        'impl':     re.compile(r'^(\s*)(?:pub\s+)?impl\s+(\w+)'),
    },
    'go': {
        'function': re.compile(r'^(\s*)func\s+(?:\([^)]*\)\s+)?(\w+)\s*\('),
        'struct':   re.compile(r'^(\s*)type\s+(\w+)\s+struct'),
        'interface': re.compile(r'^(\s*)type\s+(\w+)\s+interface'),
    },
    'java': {
        'class':    re.compile(r'^(\s*)(?:public\s+)?(?:abstract\s+)?class\s+(\w+)'),
        'interface': re.compile(r'^(\s*)(?:public\s+)?interface\s+(\w+)'),
        'method':   re.compile(r'^(\s*)(?:public|private|protected|static|\s)*(?:<[^>]+>\s+)?(\w+)\s*\([^)]*\)\s*\{'),
        'enum':     re.compile(r'^(\s*)(?:public\s+)?enum\s+(\w+)'),
    },
    'c_cpp': {
        'function': re.compile(r'^(\s*)(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:\w+\s+\*?)(\w+)\s*\([^)]*\)\s*\{'),
        'struct':   re.compile(r'^(\s*)typedef\s+struct\s+(\w+)'),
        'class':    re.compile(r'^(\s*)class\s+(\w+)'),
    },
}


def _get_lang(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    return _LANG_MAP.get(ext, None)


def _parse_python_ast(filepath):
    """Parse Python file using built-in ast. Returns structured defs."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()
    tree = ast.parse(source)

    defs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = 'async_function' if isinstance(node, ast.AsyncFunctionDef) else 'function'
            defs.append({
                'kind': kind,
                'name': node.name,
                'line': node.lineno,
                'end_line': node.end_lineno,
                'params': [arg.arg for arg in node.args.args],
                'decorators': [d.id if isinstance(d, ast.Name) else repr(d) for d in node.decorator_list],
            })
        elif isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append({
                        'kind': 'async_method' if isinstance(item, ast.AsyncFunctionDef) else 'method',
                        'name': item.name,
                        'line': item.lineno,
                    })
            defs.append({
                'kind': 'class',
                'name': node.name,
                'line': node.lineno,
                'end_line': node.end_lineno,
                'bases': [b.id if isinstance(b, ast.Name) else repr(b) for b in node.bases],
                'methods': methods,
                'decorators': [d.id if isinstance(d, ast.Name) else repr(d) for d in node.decorator_list],
            })

    return defs, source


def _parse_regex(filepath, lang):
    """Parse file using regex patterns."""
    patterns = _PATTERNS.get(lang, {})
    if lang in ('c', 'cpp', 'h', 'hpp'):
        patterns = _PATTERNS['c_cpp']

    defs = []
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return defs, ''

    for i, line in enumerate(lines, 1):
        for kind, pattern in patterns.items():
            m = pattern.match(line)
            if m:
                defs.append({
                    'kind': kind,
                    'name': m.group(2),
                    'line': i,
                    'source': line.rstrip()[:120],
                })
                break

    return defs, ''.join(lines)


# ── Public tools ────────────────────────────────────

@register_tool(
    name='code_ast_parse',
    description='解析代码文件，返回结构化定义列表（函数、类、方法、结构体等）。支持 Python/JS/TS/Rust/Go/Java/C/C++',
    parameters={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '代码文件路径（相对于工作区或绝对路径）',
            },
        },
        'required': ['path'],
    },
)
def code_ast_parse(path: str):
    """Parse a code file and return structural definitions."""
    try:
        if not os.path.exists(path):
            try:
                from tools.file_tools import _safe_path
                abs_path = _safe_path(path)
                if abs_path and os.path.exists(abs_path):
                    path = abs_path
            except Exception:
                pass
        if not os.path.exists(path):
            return {'error': f'文件不存在: {path}'}

        lang = _get_lang(path)
        if not lang:
            return {'error': f'不支持的文件类型', 'file': path, 'supported': sorted(set(_LANG_MAP.values()))}

        if lang == 'python':
            defs, source = _parse_python_ast(path)
        else:
            defs, source = _parse_regex(path, lang)

        total_lines = source.count('\n') + 1
        summary = {}
        for d in defs:
            k = d['kind']
            summary[k] = summary.get(k, 0) + 1

        return {
            'file': path,
            'language': lang,
            'total_lines': total_lines,
            'definitions': defs,
            'summary': summary,
            'total_defs': len(defs),
        }
    except SyntaxError as e:
        return {'error': f'语法错误: {e}', 'file': path}
    except Exception as e:
        return {'error': str(e), 'traceback': traceback.format_exc()}


@register_tool(
    name='code_find_defs',
    description='在代码文件中按名称或类型搜索定义。name 支持模糊匹配，kind 可筛选类型',
    parameters={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '代码文件路径',
            },
            'name': {
                'type': 'string',
                'description': '搜索的名称（模糊匹配）',
            },
            'kind': {
                'type': 'string',
                'description': '筛选定义类型，如 function/class/struct/enum',
                'default': 'all',
            },
        },
        'required': ['path'],
    },
)
def code_find_defs(path: str, name: str = None, kind: str = 'all'):
    """Find definitions in code file matching name/kind."""
    result = code_ast_parse(path)
    if 'error' in result:
        return result

    defs = result.get('definitions', [])
    if kind != 'all':
        defs = [d for d in defs if d['kind'] == kind]
    if name:
        name_lower = name.lower()
        defs = [d for d in defs if name_lower in d['name'].lower()]

    result['definitions'] = defs
    result['matched'] = len(defs)
    return result


@register_tool(
    name='code_get_symbol',
    description='定位符号在文件中的位置，返回带上下文的代码片段',
    parameters={
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': '代码文件路径',
            },
            'name': {
                'type': 'string',
                'description': '要查找的符号名称',
            },
            'context_lines': {
                'type': 'integer',
                'description': '上下文行数（前后各取几行），默认5',
                'default': 5,
            },
        },
        'required': ['path', 'name'],
    },
)
def code_get_symbol(path: str, name: str, context_lines: int = 5):
    """Locate a symbol and return its context."""
    result = code_ast_parse(path)
    if 'error' in result:
        return result

    defs = result.get('definitions', [])
    matches = [d for d in defs if d['name'] == name or name.lower() in d['name'].lower()]
    if not matches:
        return {'error': f'符号 "{name}" 未找到', 'file': path}

    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
    except Exception as e:
        return {'error': f'读取失败: {e}'}

    symbols = []
    for m in matches:
        line_no = m['line']
        start = max(0, line_no - context_lines - 1)
        end = min(len(all_lines), line_no + context_lines)
        symbols.append({
            'kind': m['kind'],
            'name': m['name'],
            'line': line_no,
            'code': ''.join(all_lines[start:end]),
        })

    return {
        'file': path,
        'symbol': name,
        'matches': symbols,
        'total_matches': len(symbols),
    }
