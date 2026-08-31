#!/usr/bin/env python3
"""
Template engine — generates code scaffold from templates.
Supports Jinja2-style variable substitution and template discovery.
"""
import os
import re
import json
from datetime import datetime
from tools.registry import register_tool

# ── Template directory ──────────────────────────────
# Templates live alongside tools/ or in a dedicated templates/ dir
_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_DIR = os.path.join(os.path.dirname(_HERE), 'templates')

# ── Built-in templates ──────────────────────────────
_BUILTIN_TEMPLATES = {}

_BUILTIN_TEMPLATES['new_tool.py.j2'] = '''#!/usr/bin/env python3
"""
{{description}}
"""
import os
import traceback
from tools.registry import register_tool


@register_tool(
    name='{{tool_name}}',
    description='{{description}}',
    parameters={
        'type': 'object',
        'properties': {
            {%- for param in params %}
            '{{param.name}}': {
                'type': '{{param.type}}',
                'description': '{{param.desc}}',
            },
            {%- endfor %}
        },
        'required': [{% for p in required_params %}'{{p}}'{{ ", " if not loop.last }}{% endfor %}],
    },
)
def {{tool_name}}({{func_params}}):
    """{{description}}"""
    try:
        # TODO: implement
        return {'status': 'ok', 'message': 'implemented'}
    except Exception as e:
        return {'error': str(e), 'traceback': traceback.format_exc()}
'''

_BUILTIN_TEMPLATES['new_skill.md.j2'] = '''---
title: {{title}}
purpose: {{purpose}}
tags: {{tags}}
triggers: {{triggers}}
tools: {{tools}}
---

# {{title}}

## 用途
{{purpose}}

## 触发条件
{{triggers}}

## 步骤
1. 
2. 
3. 

## 注意事项
- 
'''

_BUILTIN_TEMPLATES['new_config.json.j2'] = '''{
    "name": "{{name}}",
    "version": "{{version}}",
    "description": "{{description}}",
    "created": "{{created}}",
    "settings": {{settings}}
}
'''


def _discover_file_templates():
    """Discover .j2 files in template directory."""
    templates = {}
    if os.path.isdir(_TEMPLATE_DIR):
        for f in os.listdir(_TEMPLATE_DIR):
            if f.endswith('.j2'):
                try:
                    with open(os.path.join(_TEMPLATE_DIR, f), 'r', encoding='utf-8') as fh:
                        templates[f] = fh.read()
                except Exception:
                    pass
    return templates


def _get_all_templates():
    """Get all available templates (built-in + file-based)."""
    templates = _BUILTIN_TEMPLATES.copy()
    templates.update(_discover_file_templates())
    return templates


def _render_template(template_text, variables):
    """Simple template renderer — supports {{var}} and {% for x in list %}...{% endfor %}"""
    result = template_text

    # Handle for loops (simplified — single-level only)
    def _render_for(match):
        full = match.group(0)
        # parse: {% for x in list %}
        for_text = full.split('%}')[0].lstrip('{% ').lstrip('-').strip()
        # Extract list name from for tag
        for_tag_match = re.match(r'for\s+(\w+)\s+in\s+(\w+)', for_text)
        if not for_tag_match:
            return full
        var_name = for_tag_match.group(1)
        list_name = for_tag_match.group(2)

        # Get body (between for and endfor)
        body_match = re.search(r'%}(.*?){%\s*endfor\s*%}', full, re.DOTALL)
        if not body_match:
            return full
        body = body_match.group(1)

        # Get list from variables
        items = variables.get(list_name, [])
        if not items:
            return ''

        rendered = ''
        for item in items:
            item_str = body
            if isinstance(item, dict):
                for k, v in item.items():
                    item_str = item_str.replace('{{' + var_name + '.' + k + '}}', str(v))
            else:
                item_str = item_str.replace('{{' + var_name + '}}', str(item))
            rendered += item_str
        return rendered

    # Process for loops first (they contain variable references inside)
    while '{%' in result and ' for ' in result:
        result = re.sub(
            r'\{%-?\s*for\s+.*?{%-?\s*endfor\s*-?%\}',
            _render_for,
            result,
            count=1,
            flags=re.DOTALL,
        )

    # Simple variable substitution
    def _replace_var(m):
        key = m.group(1).strip()
        val = variables.get(key, m.group(0))
        return str(val)

    result = re.sub(r'\{\{(.+?)\}\}', _replace_var, result)
    return result


# ── Public tools ────────────────────────────────────

@register_tool(
    name='list_templates',
    description='列出所有可用的代码模板（内建+文件系统）',
    parameters={
        'type': 'object',
        'properties': {},
        'required': [],
    },
)
def list_templates():
    """List all available templates."""
    templates = _get_all_templates()
    template_list = []
    for name, content in templates.items():
        # Extract a short description from the content
        desc = ''
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('#'):
                desc = line.lstrip('#').strip()
                break
        if not desc and 'purpose' in content:
            m = re.search(r'purpose:\s*(.*)', content)
            if m:
                desc = m.group(1)

        template_list.append({
            'name': name,
            'description': desc or '(no description)',
            'size': len(content),
            'source': 'builtin' if name in _BUILTIN_TEMPLATES else 'file',
        })

    return {
        'templates': template_list,
        'total': len(template_list),
        'template_dir': _TEMPLATE_DIR if os.path.isdir(_TEMPLATE_DIR) else None,
    }


@register_tool(
    name='create_from_template',
    description='从模板生成代码文件。支持 {{var}} 变量替换和 {% for x in list %} 循环',
    parameters={
        'type': 'object',
        'properties': {
            'template_name': {
                'type': 'string',
                'description': '模板名称，如 new_tool.py.j2。用 list_templates 查看可用模板',
            },
            'output_path': {
                'type': 'string',
                'description': '输出文件路径',
            },
            'variables': {
                'type': 'object',
                'description': '模板变量字典。例如 {"tool_name": "my_tool", "description": "..."}',
            },
        },
        'required': ['template_name', 'output_path'],
    },
)
def create_from_template(template_name: str, output_path: str, variables: dict = None):
    """Generate a file from a template."""
    if variables is None:
        variables = {}

    # Add some automatic variables
    if 'created' not in variables:
        variables['created'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    templates = _get_all_templates()
    if template_name not in templates:
        return {
            'error': f'模板 "{template_name}" 不存在',
            'available': list(templates.keys()),
        }

    try:
        template_text = templates[template_name]
        rendered = _render_template(template_text, variables)

        # Resolve output path
        out_path = output_path
        if not os.path.isabs(out_path):
            try:
                from tools.file_tools import WORKSPACE
                out_path = os.path.join(WORKSPACE, output_path)
            except Exception:
                out_path = os.path.abspath(output_path)

        # Ensure directory exists
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(rendered)

        return {
            'status': 'ok',
            'template': template_name,
            'output': out_path,
            'size': len(rendered),
            'variables_used': list(variables.keys()),
        }
    except Exception as e:
        return {'error': str(e)}


@register_tool(
    name='preview_template',
    description='预览模板渲染结果，不写入文件',
    parameters={
        'type': 'object',
        'properties': {
            'template_name': {
                'type': 'string',
                'description': '模板名称',
            },
            'variables': {
                'type': 'object',
                'description': '模板变量',
            },
        },
        'required': ['template_name'],
    },
)
def preview_template(template_name: str, variables: dict = None):
    """Preview template rendering without writing to file."""
    if variables is None:
        variables = {}

    templates = _get_all_templates()
    if template_name not in templates:
        return {
            'error': f'模板 "{template_name}" 不存在',
            'available': list(templates.keys()),
        }

    try:
        template_text = templates[template_name]
        rendered = _render_template(template_text, variables)
        return {
            'template': template_name,
            'rendered': rendered,
            'size': len(rendered),
        }
    except Exception as e:
        return {'error': str(e)}
